"""Offline validation for PersonalAIService ``task.routing.v1`` registry data.

Quant categories and scenarios are configuration under
``projects[<id>].routing`` / ``PROJECT_POLICIES[project].routing``. This module
does not hard-code business scenario ids, submit tasks, skip unreadiness, read
secrets or authorize spend. Fail-closed provider readiness belongs to the
shared PersonalAIService runtime (see Pigbibi/PersonalAIService#88).
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, ValidationError


SCHEMA_VERSION = 'task.routing.v1'
COMPLEXITY_TIERS = ('simple', 'standard', 'complex')
COMPLEXITY_RANK = {name: index for index, name in enumerate(COMPLEXITY_TIERS)}
DEFAULT_HEURISTICS = {
    'size_simple_max': 1500,
    'size_standard': 4000,
    'size_complex': 50000,
    'image_complex_size': 20000,
    'materials_complex_count': 8,
}

_ACCOUNT_PATTERNS = (
    re.compile(r'\bacct_[A-Za-z0-9_-]+\b'),
    re.compile(r'\b(?:ak_|pk_|sk-)[A-Za-z0-9_-]{8,}\b'),
    re.compile(r'\b\d{12}\b'),
)


class RoutingError(ValueError):
    """Registry document failed the task.routing.v1 contract."""


def mask_account_ids(text: str) -> str:
    """Replace account-like tokens for diagnostics (PAS fixed log fields)."""
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    masked = text
    for pattern in _ACCOUNT_PATTERNS:
        masked = pattern.sub('[redacted-account]', masked)
    return masked


def routing_schema() -> dict[str, Any]:
    return json.loads(
        files(__package__).joinpath('task_routing.v1.schema.json').read_text(encoding='utf-8')
    )


def load_routing(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, (str, Path)):
        raw = json.loads(Path(source).read_text(encoding='utf-8'))
    elif isinstance(source, Mapping):
        raw = deepcopy(dict(source))
    else:
        raise TypeError('routing source must be a path or mapping')
    return validate_routing(raw)


def validate_routing(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate JSON Schema plus category/scenario referential integrity."""
    if not isinstance(document, dict):
        raise RoutingError('routing document must be an object')
    data = deepcopy(document)
    try:
        Draft202012Validator(routing_schema()).validate(data)
    except ValidationError as exc:
        raise RoutingError('routing document does not match task.routing.v1') from exc
    if data.get('schema_version', SCHEMA_VERSION) != SCHEMA_VERSION:
        raise RoutingError('unsupported task routing schema')

    categories = data.get('categories') or {}
    for name, category in categories.items():
        parent = category.get('parent')
        if parent is not None and parent not in categories:
            raise RoutingError(f'category {name} parent is unknown')
    for name in categories:
        _assert_acyclic(name, categories)

    for name, scenario in data['scenarios'].items():
        category = scenario.get('category')
        if category is not None and category not in categories:
            raise RoutingError(f'scenario {name} category is unknown')
        for provider in scenario['providers']:
            for tier_name in COMPLEXITY_TIERS:
                selection = _tier_selection(data, scenario, tier_name).get(provider)
                if selection is None:
                    raise RoutingError(
                        f'scenario {name} provider {provider} missing from tier {tier_name}'
                    )
    heuristics = data.get('heuristics')
    if heuristics is None:
        data['heuristics'] = dict(DEFAULT_HEURISTICS)
    else:
        merged = dict(DEFAULT_HEURISTICS)
        merged.update(heuristics)
        data['heuristics'] = merged
    return data


def validate_project_routing(routing: Mapping[str, Any], project_policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate routing and that preferred providers are admitted on the project."""
    data = validate_routing(routing)
    providers = project_policy.get('providers')
    models = project_policy.get('models')
    if not isinstance(providers, dict) or not isinstance(models, dict):
        raise RoutingError('project policy must include providers and models maps')
    admitted = set(providers.values())
    for scenario_id, scenario in data['scenarios'].items():
        for provider in scenario['providers']:
            if provider not in admitted:
                raise RoutingError(
                    f'scenario {scenario_id} provider {provider} is not in project providers'
                )
            allowlist = models.get(provider)
            if not isinstance(allowlist, (list, set, tuple)):
                raise RoutingError(f'project models missing allowlist for {provider}')
            for tier_name in COMPLEXITY_TIERS:
                model = _tier_selection(data, scenario, tier_name)[provider]['model']
                if model not in allowlist:
                    raise RoutingError(
                        f'scenario {scenario_id} model {model} is not admitted for {provider}'
                    )
    return data


def _assert_acyclic(start: str, categories: Mapping[str, Any]) -> None:
    seen: list[str] = []
    current: str | None = start
    while current is not None:
        if current in seen:
            raise RoutingError(f'category cycle involving {start}')
        seen.append(current)
        current = categories[current]['parent']


def _category_default(category_name: str | None, categories: Mapping[str, Any]) -> str:
    current = category_name
    while current is not None:
        category = categories[current]
        default = category.get('default_complexity')
        if default in COMPLEXITY_RANK:
            return default
        current = category.get('parent')
    return 'standard'


def _tier_selection(registry: Mapping[str, Any], scenario: Mapping[str, Any], tier: str) -> dict[str, Any]:
    base = dict(registry['complexity_tiers'][tier])
    overrides = (scenario.get('tier_overrides') or {}).get(tier) or {}
    merged = dict(base)
    for provider, selection in overrides.items():
        merged[provider] = dict(selection)
    return merged


def resolve_complexity(
    registry: Mapping[str, Any],
    scenario_id: str,
    *,
    requested: str | None = None,
    material_bytes: int = 0,
    material_count: int = 0,
    has_image: bool = False,
) -> str:
    """Resolve complexity using the PAS raise-only order (offline helper)."""
    data = validate_routing(registry)
    scenario = data['scenarios'].get(scenario_id)
    if scenario is None:
        raise RoutingError('unknown scenario')
    if requested is not None:
        if requested not in COMPLEXITY_RANK:
            raise RoutingError('unknown complexity')
        tier = requested
    else:
        matching = scenario.get('matching') or {}
        default = matching.get('default_complexity')
        if default is None:
            default = _category_default(scenario.get('category'), data.get('categories') or {})
        tier = default if default in COMPLEXITY_RANK else 'standard'

    matching = scenario.get('matching') or {}
    heuristics = data['heuristics']
    thresholds = matching.get('size_thresholds') or {}
    standard_size = thresholds.get('standard', heuristics['size_standard'])
    complex_size = thresholds.get('complex', heuristics['size_complex'])

    raised = tier
    if has_image:
        floor = matching.get('min_with_image') or (
            'complex' if material_bytes >= heuristics['image_complex_size'] else None
        )
        if floor is not None and COMPLEXITY_RANK[floor] > COMPLEXITY_RANK[raised]:
            raised = floor
    if material_count >= heuristics['materials_complex_count']:
        raised = 'complex' if COMPLEXITY_RANK['complex'] > COMPLEXITY_RANK[raised] else raised
    if material_bytes >= complex_size:
        raised = 'complex'
    elif material_bytes >= standard_size and COMPLEXITY_RANK['standard'] > COMPLEXITY_RANK[raised]:
        raised = 'standard'
    return raised


def effective_route(
    registry: Mapping[str, Any],
    scenario_id: str,
    *,
    complexity: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Materialize inherited tier selections for one scenario (data only)."""
    data = validate_routing(registry)
    scenario = data['scenarios'].get(scenario_id)
    if scenario is None:
        raise RoutingError('unknown scenario')
    tier = resolve_complexity(data, scenario_id, requested=complexity)
    selections = _tier_selection(data, scenario, tier)
    route = {
        'schema_version': SCHEMA_VERSION,
        'scenario': scenario_id,
        'category': scenario.get('category'),
        'mode': scenario['mode'],
        'providers': list(scenario['providers']),
        'tier': tier,
        'selections': {
            provider: dict(selections[provider]) for provider in scenario['providers']
        },
        'matching_hints': list((scenario.get('matching') or {}).get('hints') or []),
        'result_kind': 'advisory',
    }
    if account_id is not None:
        route['account_id'] = mask_account_ids(str(account_id))
    return route
