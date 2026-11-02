"""Test-only PAS configuration validator; no runtime routing or provider access."""
# Source: Pigbibi/PersonalAIService src/ai_service/routing.py
# SHA: 0145c0c02ac24da2edfa02d52f0490880379f02f (PR #88).
# Only validation definitions copied verbatim; see pas88-LICENSE.
from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = 'task.routing.v1'


Tiers = frozenset({'simple', 'standard', 'complex'})


Efforts = frozenset({'low', 'medium', 'high'})


ProviderName = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


RegistryId = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')


DEFAULT_HEURISTICS = {
    'size_simple_max': 1500,
    'size_standard': 4000,
    'size_complex': 50000,
    'image_complex_size': 20000,
    'materials_complex_count': 8,
}


class RoutingError(ValueError):
    """Invalid routing configuration or an unsatisfiable routing decision."""


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or RegistryId.fullmatch(value) is None:
        raise RoutingError(f'invalid {label}')
    return value


def _validate_tier_mapping(tier_name: str, mapping: Any) -> None:
    if tier_name not in Tiers or not isinstance(mapping, dict) or not mapping:
        raise RoutingError('invalid complexity tier mapping')
    for provider, entry in mapping.items():
        if not isinstance(provider, str) or ProviderName.fullmatch(provider) is None:
            raise RoutingError('invalid tier provider')
        if (not isinstance(entry, dict) or set(entry) != {'model', 'reasoning_effort'}
                or not isinstance(entry['model'], str) or not entry['model']
                or entry['reasoning_effort'] not in Efforts):
            raise RoutingError('tier entry requires model and reasoning_effort')


def _validate_matching(matching: Any) -> dict:
    if matching is None:
        return {}
    if not isinstance(matching, dict):
        raise RoutingError('matching must be an object')
    allowed = {'hints', 'default_complexity', 'min_with_image', 'size_thresholds'}
    if set(matching) - allowed:
        raise RoutingError('unknown matching fields')
    hints = matching.get('hints', [])
    if not isinstance(hints, list) or any(not isinstance(item, str) or not item or len(item) > 64
                                          for item in hints):
        raise RoutingError('matching.hints must be short strings')
    for key in ('default_complexity', 'min_with_image'):
        if key in matching and matching[key] not in Tiers:
            raise RoutingError(f'{key} must be a complexity tier')
    thresholds = matching.get('size_thresholds', {})
    if not isinstance(thresholds, dict) or set(thresholds) - {'standard', 'complex'}:
        raise RoutingError('size_thresholds may only set standard and complex')
    for name, value in thresholds.items():
        if type(value) is not int or value < 1:
            raise RoutingError('size_thresholds values must be positive integers')
    return matching


def _validate_heuristics(heuristics: Any) -> dict:
    if heuristics is None:
        return dict(DEFAULT_HEURISTICS)
    if not isinstance(heuristics, dict) or set(heuristics) - set(DEFAULT_HEURISTICS):
        raise RoutingError('unknown heuristics fields')
    merged = dict(DEFAULT_HEURISTICS)
    for key, value in heuristics.items():
        if type(value) is not int or value < 1:
            raise RoutingError('heuristics values must be positive integers')
        merged[key] = value
    if not (merged['size_simple_max'] < merged['size_standard'] < merged['size_complex']):
        raise RoutingError('heuristics size thresholds must increase')
    return merged


def validate_routing_config(routing: Any) -> dict:
    """Validate and normalize a `task.routing.v1` registry object."""
    if routing is None:
        return {}
    if not isinstance(routing, dict):
        raise RoutingError('routing must be an object')
    allowed = {'schema_version', 'categories', 'scenarios', 'complexity_tiers', 'heuristics'}
    if set(routing) - allowed:
        raise RoutingError('unknown routing fields')
    version = routing.get('schema_version', SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise RoutingError('unsupported routing schema_version')
    categories = routing.get('categories', {})
    scenarios = routing.get('scenarios', {})
    tiers = routing.get('complexity_tiers', {})
    if not isinstance(categories, dict) or not isinstance(scenarios, dict) or not isinstance(tiers, dict):
        raise RoutingError('routing maps must be objects')
    if not scenarios:
        raise RoutingError('routing.scenarios is required when routing is set')
    if set(tiers) != Tiers:
        raise RoutingError('complexity_tiers must define simple, standard and complex')
    for name, category in categories.items():
        _require_id(name, 'category id')
        if not isinstance(category, dict):
            raise RoutingError('category must be an object')
        allowed_cat = {'parent', 'default_complexity'}
        if set(category) - allowed_cat:
            raise RoutingError('unknown category fields')
        parent = category.get('parent')
        if parent is not None:
            _require_id(parent, 'category parent')
            if parent == name:
                raise RoutingError('category cannot parent itself')
        default = category.get('default_complexity', 'standard')
        if default not in Tiers:
            raise RoutingError('category default_complexity must be a complexity tier')
    for name, category in categories.items():
        parent = category.get('parent')
        seen = {name}
        while parent is not None:
            if parent not in categories:
                raise RoutingError('category parent is not registered')
            if parent in seen:
                raise RoutingError('category parent cycle')
            seen.add(parent)
            parent = categories[parent].get('parent')
    for name, scenario in scenarios.items():
        _require_id(name, 'scenario id')
        if not isinstance(scenario, dict):
            raise RoutingError('scenario must be an object')
        allowed_scn = {'category', 'mode', 'providers', 'matching', 'tier_overrides'}
        if set(scenario) - allowed_scn or not {'mode', 'providers'} <= set(scenario):
            raise RoutingError('scenario requires mode and providers')
        category = scenario.get('category')
        if category is not None:
            _require_id(category, 'scenario category')
            if category not in categories:
                raise RoutingError('scenario category is not registered')
        if scenario['mode'] not in {'api', 'agent'}:
            raise RoutingError('scenario mode must be api or agent')
        providers = scenario['providers']
        if not isinstance(providers, list) or not providers:
            raise RoutingError('scenario providers must be a nonempty ordered list')
        if len(set(providers)) != len(providers):
            raise RoutingError('scenario providers must be unique')
        for provider in providers:
            if not isinstance(provider, str) or ProviderName.fullmatch(provider) is None:
                raise RoutingError('invalid provider preference')
        _validate_matching(scenario.get('matching'))
        overrides = scenario.get('tier_overrides', {})
        if not isinstance(overrides, dict) or set(overrides) - Tiers:
            raise RoutingError('tier_overrides may only override known tiers')
        for tier_name, mapping in overrides.items():
            _validate_tier_mapping(tier_name, mapping)
    for tier_name, mapping in tiers.items():
        _validate_tier_mapping(tier_name, mapping)
    return {
        'schema_version': SCHEMA_VERSION,
        'categories': categories,
        'scenarios': scenarios,
        'complexity_tiers': tiers,
        'heuristics': _validate_heuristics(routing.get('heuristics')),
    }
