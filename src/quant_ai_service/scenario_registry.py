"""Load quant scenario registry data for the shared PersonalAIService extension.

This module validates and inherits config. It does not select providers at
runtime, skip unreadiness, submit tasks, read secrets or authorize spend.
Quant scenarios and categories live only in JSON; add new ones by editing
config, not by changing Python tables.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, ValidationError


EXTENSION_SCHEMA_VERSION = 'personal-ai.scenario-registry.extension.v1'
COMPLEXITY_TIERS = frozenset({'simple', 'standard', 'complex'})

_ACCOUNT_PATTERNS = (
    re.compile(r'\bacct_[A-Za-z0-9_-]+\b'),
    re.compile(r'\b(?:ak_|pk_|sk-)[A-Za-z0-9_-]{8,}\b'),
    re.compile(r'\b\d{12}\b'),
)


class RegistryError(ValueError):
    """Registry document failed the extension contract or referential checks."""


def mask_account_ids(text: str) -> str:
    """Replace account-like tokens before writing logs or artifacts."""
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    masked = text
    for pattern in _ACCOUNT_PATTERNS:
        masked = pattern.sub('[redacted-account]', masked)
    return masked


def extension_schema() -> dict[str, Any]:
    return json.loads(
        files(__package__).joinpath('scenario_registry.extension.v1.schema.json').read_text(encoding='utf-8')
    )


def load_registry(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, (str, Path)):
        raw = json.loads(Path(source).read_text(encoding='utf-8'))
    elif isinstance(source, Mapping):
        raw = deepcopy(dict(source))
    else:
        raise TypeError('registry source must be a path or mapping')
    return validate_registry_document(raw)


def validate_registry_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate JSON Schema then referential integrity for the extension contract."""
    if not isinstance(document, dict):
        raise RegistryError('registry document must be an object')
    data = deepcopy(document)
    try:
        Draft202012Validator(extension_schema()).validate(data)
    except ValidationError as exc:
        raise RegistryError('registry document does not match extension schema') from exc
    if data.get('schema_version') != EXTENSION_SCHEMA_VERSION:
        raise RegistryError('unsupported scenario registry schema')
    if not isinstance(data.get('assumption'), str) or not data['assumption'].strip():
        raise RegistryError('assumption must document PersonalAIService alignment')

    bindings = data['provider_bindings']
    profiles = data['model_profiles']
    tiers = data['complexity_tiers']
    categories = data['categories']
    scenarios = data['scenarios']

    for tier_name, tier in tiers.items():
        if tier_name not in COMPLEXITY_TIERS:
            raise RegistryError('unknown complexity tier')
        if tier['model_profile'] not in profiles:
            raise RegistryError(f'model_profile missing for tier {tier_name}')

    for profile_name, models in profiles.items():
        unknown = set(models) - set(bindings)
        if unknown:
            raise RegistryError(f'model profile {profile_name} references unknown providers')

    for name, category in categories.items():
        parent = category['parent']
        if parent is not None and parent not in categories:
            raise RegistryError(f'category {name} parent is unknown')
        _check_preference(category['provider_preference'], bindings, allow_empty=True)
        _check_tier_overrides(category['tier_overrides'], profiles)

    for name, category in categories.items():
        _assert_acyclic(name, categories)

    for name, scenario in scenarios.items():
        if scenario['category'] not in categories:
            raise RegistryError(f'scenario {name} category is unknown')
        _check_preference(scenario['provider_preference'], bindings, allow_empty=True)
        _check_tier_overrides(scenario['tier_overrides'], profiles)
        preference = _inherited_preference(scenario, categories)
        if not preference:
            raise RegistryError(f'scenario {name} has no provider_preference after inheritance')
        complexity = _inherited_default_complexity(scenario, categories)
        if complexity not in COMPLEXITY_TIERS:
            raise RegistryError(f'scenario {name} default_complexity is invalid')

    return data


def _check_preference(preference: list[str], bindings: Mapping[str, Any], *, allow_empty: bool) -> None:
    if not isinstance(preference, list):
        raise RegistryError('provider_preference must be a list')
    if not preference and not allow_empty:
        raise RegistryError('provider_preference must be nonempty')
    if len(set(preference)) != len(preference):
        raise RegistryError('provider_preference must not contain duplicates')
    for logical in preference:
        if logical not in bindings:
            raise RegistryError(f'unknown logical provider {logical}')


def _check_tier_overrides(overrides: Mapping[str, Any], profiles: Mapping[str, Any]) -> None:
    if not isinstance(overrides, dict):
        raise RegistryError('tier_overrides must be an object')
    default = overrides.get('default_complexity')
    if default is not None and default not in COMPLEXITY_TIERS:
        raise RegistryError('invalid default_complexity override')
    for tier_name in COMPLEXITY_TIERS:
        partial = overrides.get(tier_name)
        if partial is None:
            continue
        profile = partial.get('model_profile')
        if profile is not None and profile not in profiles:
            raise RegistryError(f'tier override references unknown model_profile {profile}')


def _assert_acyclic(start: str, categories: Mapping[str, Any]) -> None:
    seen: list[str] = []
    current: str | None = start
    while current is not None:
        if current in seen:
            raise RegistryError(f'category cycle involving {start}')
        seen.append(current)
        current = categories[current]['parent']


def _category_chain(category_name: str, categories: Mapping[str, Any]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current: str | None = category_name
    while current is not None:
        category = categories[current]
        chain.append(category)
        current = category['parent']
    return chain


def _inherited_preference(scenario: Mapping[str, Any], categories: Mapping[str, Any]) -> list[str]:
    if scenario['provider_preference']:
        return list(scenario['provider_preference'])
    for category in _category_chain(scenario['category'], categories):
        if category['provider_preference']:
            return list(category['provider_preference'])
    return []


def _merge_tier_overrides(scenario: Mapping[str, Any], categories: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    layers = list(reversed(_category_chain(scenario['category'], categories)))
    layers.append(scenario)
    for layer in layers:
        overrides = layer.get('tier_overrides') or {}
        if 'default_complexity' in overrides:
            merged['default_complexity'] = overrides['default_complexity']
        for tier_name in COMPLEXITY_TIERS:
            partial = overrides.get(tier_name)
            if partial is None:
                continue
            base = dict(merged.get(tier_name) or {})
            base.update(partial)
            merged[tier_name] = base
    return merged


def _inherited_default_complexity(scenario: Mapping[str, Any], categories: Mapping[str, Any]) -> str:
    overrides = _merge_tier_overrides(scenario, categories)
    return overrides.get('default_complexity', 'standard')


def effective_scenario(registry: Mapping[str, Any], scenario_id: str) -> dict[str, Any]:
    """Resolve inherited category fields for one scenario id from registry data.

    Generic inheritance only. Fail-closed provider readiness checks belong to the
    PersonalAIService registry runtime, not this data package.
    """
    data = validate_registry_document(registry)
    scenario = data['scenarios'].get(scenario_id)
    if scenario is None:
        raise RegistryError('unknown scenario')
    overrides = _merge_tier_overrides(scenario, data['categories'])
    complexity = overrides.get('default_complexity', 'standard')
    tier = dict(data['complexity_tiers'][complexity])
    partial = overrides.get(complexity) or {}
    tier.update(partial)
    profile_name = tier['model_profile']
    models = dict(data['model_profiles'][profile_name])
    category_name = scenario['category']
    hints: list[str] = []
    for item in reversed(_category_chain(category_name, data['categories'])):
        for hint in item['matching_hints']:
            if hint not in hints:
                hints.append(hint)
    for hint in scenario['matching_hints']:
        if hint not in hints:
            hints.append(hint)
    return {
        'schema_version': EXTENSION_SCHEMA_VERSION,
        'registry_id': data['registry_id'],
        'scenario': scenario_id,
        'label': scenario['label'],
        'category': category_name,
        'matching_hints': hints,
        'provider_preference': _inherited_preference(scenario, data['categories']),
        'provider_bindings': {
            name: dict(data['provider_bindings'][name])
            for name in _inherited_preference(scenario, data['categories'])
        },
        'default_complexity': complexity,
        'reasoning_effort': tier['reasoning_effort'],
        'model_profile': profile_name,
        'models': models,
        'result_kind': 'advisory',
    }
