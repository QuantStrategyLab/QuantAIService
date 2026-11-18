"""Abstract scenario routing registry and complexity tiers.

Scenarios and categories are configuration data. This module never hard-codes
business scenario ids. Downstream services (including QuantAIService) plug in
their own registry through the same `task.routing.v1` shape.

Callers may hint `scenario` and `complexity`. They cannot choose a provider.
Unready providers are skipped; if none remain, routing fails closed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import compact_json

SCHEMA_VERSION = 'task.routing.v1'
Tiers = frozenset({'simple', 'standard', 'complex'})
Modes = frozenset({'api', 'agent', 'auto'})
ConcreteModes = frozenset({'api', 'agent'})
DEFAULT_MODE_BY_TIER = {'simple': 'api', 'standard': 'api', 'complex': 'agent'}
_API_PROVIDER_TYPES = frozenset({'gemini_api', 'openai_api', 'anthropic_api', 'chatgpt_plan_oauth'})
_AGENT_PROVIDER_TYPES = frozenset({'muse', 'dot_events', 'grok_webhook', 'claude_routine', 'claudeduet'})
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
TIER_RANK = {'simple': 0, 'standard': 1, 'complex': 2}


def configured_provider_mode(item: Any) -> str | None:
    """Return concrete execution mode ('api' or 'agent') for a configured provider."""
    if not isinstance(item, dict):
        return None
    kind = item.get('type')
    if kind in _API_PROVIDER_TYPES:
        return 'api'
    if kind in _AGENT_PROVIDER_TYPES:
        return 'agent'
    return None


def resolve_scenario_mode(scenario: dict[str, Any], tier: str) -> str:
    """Resolve concrete execution mode ('api' or 'agent') for a scenario and tier."""
    mode = scenario['mode']
    if mode != 'auto':
        return mode
    by_tier = scenario.get('mode_by_tier') or {}
    return by_tier.get(tier, DEFAULT_MODE_BY_TIER[tier])


class RoutingError(ValueError):
    """Invalid routing configuration or an unsatisfiable routing decision."""


@dataclass(frozen=True)
class RoutingDecision:
    provider: str
    scenario: str
    category: str | None
    tier: str
    model: str
    reasoning_effort: str
    mode: str
    reason: str


def mask_project(project: str) -> str:
    """Mask owner/repo identities for diagnostics. Not a cryptographic hash."""
    if not isinstance(project, str) or '/' not in project:
        return '[redacted]'
    parts = []
    for segment in project.split('/'):
        if not segment:
            parts.append('***')
        elif len(segment) <= 2:
            parts.append('*' * len(segment))
        else:
            parts.append(segment[0] + ('*' * (len(segment) - 2)) + segment[-1])
    return '/'.join(parts)


def _require_pattern(value: Any, pattern: re.Pattern[str], message: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RoutingError(message)
    return value


def _require_id(value: Any, label: str) -> str:
    return _require_pattern(value, RegistryId, f'invalid {label}')


def _require_enum(value: Any, allowed: frozenset[str] | set[str], message: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise RoutingError(message)
    return value


def _require_dict(
    value: Any,
    type_message: str,
    *,
    allowed: set[str] | frozenset[str] | None = None,
    required: set[str] | frozenset[str] | None = None,
    unknown_message: str | None = None,
    required_message: str | None = None,
    non_empty: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutingError(type_message)
    keys = set(value)
    if allowed is not None and keys - allowed:
        raise RoutingError(unknown_message or type_message)
    if required is not None and not required <= keys:
        raise RoutingError(required_message or unknown_message or type_message)
    if non_empty and not value:
        raise RoutingError(type_message)
    return value


def _require_positive_int_map(mapping: dict[str, Any], message: str) -> dict[str, int]:
    for value in mapping.values():
        if type(value) is not int or value < 1:
            raise RoutingError(message)
    return mapping


def _validate_tier_mapping(tier_name: str, mapping: Any) -> None:
    if tier_name not in Tiers:
        raise RoutingError('invalid complexity tier mapping')
    entries = _require_dict(mapping, 'invalid complexity tier mapping', non_empty=True)
    for provider, entry in entries.items():
        _require_pattern(provider, ProviderName, 'invalid tier provider')
        item = _require_dict(
            entry,
            'tier entry requires model and reasoning_effort',
            allowed={'model', 'reasoning_effort'},
            required={'model', 'reasoning_effort'},
        )
        if not isinstance(item['model'], str) or not item['model']:
            raise RoutingError('tier entry requires model and reasoning_effort')
        _require_enum(item['reasoning_effort'], Efforts, 'tier entry requires model and reasoning_effort')


def _validate_matching(matching: Any) -> dict:
    if matching is None:
        return {}
    obj = _require_dict(
        matching,
        'matching must be an object',
        allowed={'hints', 'default_complexity', 'min_with_image', 'size_thresholds'},
        unknown_message='unknown matching fields',
    )
    hints = obj.get('hints', [])
    if not isinstance(hints, list) or any(
        not isinstance(item, str) or not item or len(item) > 64 for item in hints
    ):
        raise RoutingError('matching.hints must be short strings')
    for key in ('default_complexity', 'min_with_image'):
        if key in obj:
            _require_enum(obj[key], Tiers, f'{key} must be a complexity tier')
    thresholds = _require_dict(
        obj.get('size_thresholds', {}),
        'size_thresholds may only set standard and complex',
        allowed={'standard', 'complex'},
    )
    _require_positive_int_map(thresholds, 'size_thresholds values must be positive integers')
    return obj


def _validate_heuristics(heuristics: Any) -> dict:
    if heuristics is None:
        return dict(DEFAULT_HEURISTICS)
    raw = _require_dict(heuristics, 'unknown heuristics fields', allowed=set(DEFAULT_HEURISTICS))
    _require_positive_int_map(raw, 'heuristics values must be positive integers')
    merged = {**DEFAULT_HEURISTICS, **raw}
    if not (merged['size_simple_max'] < merged['size_standard'] < merged['size_complex']):
        raise RoutingError('heuristics size thresholds must increase')
    return merged


def _validate_categories(categories: dict[str, Any]) -> None:
    for name, raw in categories.items():
        _require_id(name, 'category id')
        category = _require_dict(
            raw,
            'category must be an object',
            allowed={'parent', 'default_complexity'},
            unknown_message='unknown category fields',
        )
        parent = category.get('parent')
        if parent is not None:
            _require_id(parent, 'category parent')
            if parent == name:
                raise RoutingError('category cannot parent itself')
        _require_enum(
            category.get('default_complexity', 'standard'),
            Tiers,
            'category default_complexity must be a complexity tier',
        )
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


def _validate_scenarios(scenarios: dict[str, Any], categories: dict[str, Any]) -> None:
    for name, raw in scenarios.items():
        _require_id(name, 'scenario id')
        scenario = _require_dict(
            raw,
            'scenario must be an object',
            allowed={'category', 'mode', 'mode_by_tier', 'providers', 'matching', 'tier_overrides'},
            required={'mode', 'providers'},
            unknown_message='scenario requires mode and providers',
            required_message='scenario requires mode and providers',
        )
        category = scenario.get('category')
        if category is not None:
            _require_id(category, 'scenario category')
            if category not in categories:
                raise RoutingError('scenario category is not registered')
        _require_enum(scenario['mode'], Modes, 'scenario mode must be api, agent or auto')
        if 'mode_by_tier' in scenario:
            if scenario['mode'] != 'auto':
                raise RoutingError('mode_by_tier requires scenario mode auto')
            mode_by_tier = _require_dict(
                scenario['mode_by_tier'],
                'mode_by_tier must be a nonempty tier mapping',
                allowed=Tiers,
                unknown_message='mode_by_tier keys must be simple, standard or complex',
                non_empty=True,
            )
            for target_mode in mode_by_tier.values():
                _require_enum(target_mode, ConcreteModes, 'mode_by_tier values must be api or agent')
        providers = scenario['providers']
        if not isinstance(providers, list) or not providers:
            raise RoutingError('scenario providers must be a nonempty ordered list')
        for provider in providers:
            _require_pattern(provider, ProviderName, 'invalid provider preference')
        if len(set(providers)) != len(providers):
            raise RoutingError('scenario providers must be unique')
        _validate_matching(scenario.get('matching'))
        overrides = _require_dict(
            scenario.get('tier_overrides', {}),
            'tier_overrides may only override known tiers',
            allowed=Tiers,
        )
        for tier_name, mapping in overrides.items():
            _validate_tier_mapping(tier_name, mapping)


def _validate_provider_modes(scenarios: dict[str, Any], provider_modes: dict[str, str | None] | None) -> None:
    if not provider_modes:
        return
    for scenario in scenarios.values():
        providers = scenario['providers']
        if scenario['mode'] != 'auto':
            for provider in providers:
                pm = provider_modes.get(provider)
                if pm is not None and pm != scenario['mode']:
                    raise RoutingError('scenario provider mode does not match scenario mode')
        elif all(provider_modes.get(provider) is not None for provider in providers):
            for tier_name in Tiers:
                target_mode = resolve_scenario_mode(scenario, tier_name)
                if not any(provider_modes.get(provider) == target_mode for provider in providers):
                    raise RoutingError('auto scenario has no provider for resolved tier mode')


def _validate_admitted_models(
    scenarios: dict[str, Any], tiers: dict[str, Any], models: dict | None,
) -> None:
    if models is None:
        return
    for scenario in scenarios.values():
        for tier_name, mapping in tiers.items():
            overrides = scenario.get('tier_overrides', {}).get(tier_name, {})
            for provider in scenario['providers']:
                entry = overrides.get(provider, mapping.get(provider))
                if entry is not None and entry['model'] not in models.get(provider, ()):
                    raise RoutingError('scenario tier model is not admitted')


def validate_routing_config(routing: Any, models: dict | None = None, *,
                            provider_modes: dict[str, str | None] | None = None,
                            project_providers: set[str] | None = None) -> dict:
    """Validate and normalize a `task.routing.v1` registry object."""
    if routing is None:
        return {}
    obj = _require_dict(
        routing,
        'routing must be an object',
        allowed={'schema_version', 'categories', 'scenarios', 'complexity_tiers', 'heuristics'},
        unknown_message='unknown routing fields',
    )
    if obj.get('schema_version', SCHEMA_VERSION) != SCHEMA_VERSION:
        raise RoutingError('unsupported routing schema_version')
    categories = obj.get('categories', {})
    scenarios = obj.get('scenarios', {})
    tiers = obj.get('complexity_tiers', {})
    if not isinstance(categories, dict) or not isinstance(scenarios, dict) or not isinstance(tiers, dict):
        raise RoutingError('routing maps must be objects')
    if not scenarios:
        raise RoutingError('routing.scenarios is required when routing is set')
    if set(tiers) != Tiers:
        raise RoutingError('complexity_tiers must define simple, standard and complex')
    _validate_categories(categories)
    _validate_scenarios(scenarios, categories)
    for tier_name, mapping in tiers.items():
        _validate_tier_mapping(tier_name, mapping)
    _validate_admitted_models(scenarios, tiers, models)
    _validate_provider_modes(scenarios, provider_modes)
    if project_providers is not None:
        for scenario in scenarios.values():
            for provider in scenario['providers']:
                if provider not in project_providers:
                    raise RoutingError('scenario provider is not admitted')
    return {
        'schema_version': SCHEMA_VERSION,
        'categories': categories,
        'scenarios': scenarios,
        'complexity_tiers': tiers,
        'heuristics': _validate_heuristics(obj.get('heuristics')),
    }


def _category_default(routing: dict, category_id: str | None) -> str:
    current = category_id
    while current is not None:
        category = routing['categories'].get(current)
        if category is None:
            break
        if 'default_complexity' in category:
            return category['default_complexity']
        current = category.get('parent')
    return 'standard'


def _raise_tier(current: str, floor: str | None) -> str:
    if floor is None:
        return current
    return floor if TIER_RANK[floor] > TIER_RANK[current] else current


def resolve_complexity(request: dict, scenario_id: str, routing: dict | None = None) -> str:
    """Resolve complexity from an explicit hint, then registry matching data.

    Defaults come only from scenario/category registry fields. Heuristics may
    raise the tier; they never invent scenario-specific branches in code.
    """
    explicit = request.get('complexity')
    if explicit is not None:
        _require_enum(explicit, Tiers, 'complexity must be simple, standard or complex')
    routing = validate_routing_config(routing) if routing else {}
    scenario = (routing.get('scenarios') or {}).get(scenario_id, {})
    matching = scenario.get('matching') or {}
    heuristics = routing.get('heuristics') or dict(DEFAULT_HEURISTICS)
    materials = request.get('materials', [])
    if not isinstance(materials, list):
        materials = []
    size = len(compact_json({'objective': request.get('objective', ''), 'materials': materials}).encode('utf-8'))
    has_image = any(isinstance(item, dict) and (
        item.get('type') == 'image'
        or isinstance(item.get('images'), list)
        or (isinstance(item.get('mime_type'), str) and item['mime_type'].startswith('image/'))
    ) for item in materials)
    thresholds = matching.get('size_thresholds') or {}
    size_standard = thresholds.get('standard', heuristics['size_standard'])
    size_complex = thresholds.get('complex', heuristics['size_complex'])
    tier = explicit if explicit is not None else (
        matching.get('default_complexity') or _category_default(routing, scenario.get('category'))
    )
    if has_image:
        floor = matching.get('min_with_image')
        if floor is None and size >= heuristics['image_complex_size']:
            floor = 'complex'
        if floor is not None:
            tier = _raise_tier(tier, floor)
    if len(materials) >= heuristics['materials_complex_count'] or size >= size_complex:
        return 'complex'
    if size >= size_standard:
        tier = _raise_tier(tier, 'standard')
    return tier


def _tier_entry(routing: dict, scenario_id: str, tier: str, provider: str) -> dict:
    scenario = routing['scenarios'][scenario_id]
    override = ((scenario.get('tier_overrides') or {}).get(tier) or {}).get(provider)
    if override is not None:
        return override
    entry = ((routing.get('complexity_tiers') or {}).get(tier) or {}).get(provider)
    if entry is None:
        raise RoutingError('no tier mapping for provider')
    return entry


def decide_route(routing: dict, request: dict, *, project: str,
                 provider_ready: Callable[[str], tuple[bool, str]],
                 provider_mode: Callable[[str], str | None] | None = None,
                 allows_tier_model: Callable[[str, str, str], bool] | None = None,
                 any_tier_model: Callable[[str, str, str], bool] | None = None,
                 select_api_model: Callable[[str, str, str], str] | None = None,
                 log: logging.Logger | None = None) -> RoutingDecision:
    routing = validate_routing_config(routing)
    scenario_id = request.get('scenario')
    if not isinstance(scenario_id, str) or scenario_id not in routing['scenarios']:
        raise RoutingError('scenario is not configured for this project')
    configured = routing['scenarios'][scenario_id]
    tier = resolve_complexity(request, scenario_id, routing)
    effective_mode = resolve_scenario_mode(configured, tier)
    req_mode = request.get('mode')
    if req_mode not in (None, 'auto') and req_mode != effective_mode:
        raise RoutingError('request mode does not match scenario mode')
    category = configured.get('category')
    skips: list[str] = []
    model_mismatch = False
    for provider in configured['providers']:
        if provider_mode is not None:
            pm = provider_mode(provider)
            if pm is not None and pm != effective_mode:
                skips.append(f'skip:{provider}:mode_mismatch')
                continue
        ready, detail = provider_ready(provider)
        if not ready:
            skips.append(f'skip:{provider}:{detail}')
            continue
        try:
            entry = _tier_entry(routing, scenario_id, tier, provider)
        except RoutingError as exc:
            skips.append(f'skip:{provider}:{exc}')
            continue
        requested_model = request.get('model')
        if ('model' in request and requested_model != entry['model']
                and not (effective_mode == 'api' and allows_tier_model and allows_tier_model(provider, tier, requested_model))):
            model_mismatch = True
            skips.append(f'skip:{provider}:model_mismatch')
            continue
        model = requested_model if 'model' in request else entry['model']
        if effective_mode == 'api' and 'model' not in request:
            checker = any_tier_model if any_tier_model is not None else allows_tier_model
            if checker is not None and not checker(provider, tier, model):
                skips.append(f'skip:{provider}:model_mismatch')
                continue
        if effective_mode == 'api' and select_api_model is not None:
            try:
                model = select_api_model(provider, tier, model)
            except LookupError:
                skips.append(f'skip:{provider}:model_unavailable')
                continue
        reason = f'scenario:{scenario_id};tier:{tier};mode:{effective_mode}'
        if category:
            reason = f'category:{category};' + reason
        if skips:
            reason += ';' + ';'.join(skips)
        decision = RoutingDecision(
            provider=provider, scenario=scenario_id, category=category, tier=tier, model=model,
            reasoning_effort=entry['reasoning_effort'], mode=effective_mode, reason=reason,
        )
        _emit(log, project, decision)
        return decision
    if model_mismatch:
        raise RoutingError('model does not match complexity tier')
    raise RoutingError('no ready provider for scenario')


def _emit(log: logging.Logger | None, project: str, decision: RoutingDecision) -> None:
    logger = log or logging.getLogger('ai_service.routing')
    payload = {
        'event': 'task_routing',
        'project_masked': mask_project(project),
        'provider': decision.provider,
        'mode': decision.mode,
        'category': decision.category,
        'tier': decision.tier,
        'model': decision.model,
        'effort': decision.reasoning_effort,
        'reason': decision.reason,
        'scenario': decision.scenario,
    }
    logger.info('%s', compact_json(payload, sort_keys=True))


def provider_readiness(name: str, *, env: dict[str, str] | None,
                       configured_providers: dict[str, Any],
                       has_dot_subscription: bool,
                       has_muse_subscription: bool = False) -> tuple[bool, str]:
    """Fail-closed readiness probe used by submit-time routing."""
    item = configured_providers.get(name)
    if item is None:
        return False, 'not_configured'
    kind = item.get('type') if isinstance(item, dict) else None
    environment = env or {}
    if kind == 'dot_events':
        if item.get('enabled') is not True:
            return False, 'disabled'
        return (True, 'ready') if has_dot_subscription else (False, 'no_subscription')
    if kind == 'muse':
        # Cloudflare event subscription wake; never Cursor Cloud Agents.
        if item.get('enabled') is not True:
            return False, 'disabled'
        return (True, 'ready') if has_muse_subscription else (False, 'no_subscription')
    if kind in {'grok_webhook', 'claude_routine'}:
        if item.get('enabled') is not True:
            return False, 'disabled'
        if name == 'grokbot' and not environment.get('AI_SERVICE_GROK_READY'):
            return False, 'grok_not_ready'
        key_env = item.get('key_env')
        if not isinstance(key_env, str) or not environment.get(key_env):
            return False, 'missing_credential'
        return True, 'ready'
    if kind in {'gemini_api', 'openai_api', 'anthropic_api'}:
        if item.get('enabled') is not True or item.get('api_usage_authorized') is not True:
            return False, 'disabled'
        key_env = item.get('key_env')
        if not isinstance(key_env, str) or not environment.get(key_env):
            return False, 'missing_credential'
        return True, 'ready'
    if kind == 'chatgpt_plan_oauth':
        if item.get('plan_usage_authorized') is not True:
            return False, 'disabled'
        token_env = item.get('token_env')
        if not isinstance(token_env, str) or not environment.get(token_env):
            return False, 'missing_credential'
        return True, 'ready'
    if kind == 'claudeduet':
        return False, 'unavailable'
    return False, 'unsupported'
