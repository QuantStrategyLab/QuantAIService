"""Configuration validation and runtime assembly (composition root).

Builds ``Application`` for the HTTP transport. Keep request handlers in
``application/`` and socket framing in ``server.py``.
"""
from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator

from .auth import JWTAuthorizer, Principal, grant_projects, project_belongs_to, valid_owner
from .events import DotEventProvider, EventSubscriptions
from .gemini import GeminiProvider
from .github_apps import GitHubApps, GitHubComments, validate_apps
from .inference import AnthropicAPIProvider, ChatGPTPlanProvider, OpenAIAPIProvider, select_api_model
from .mcp_logging import log_level
from .providers import ClaudeRoutineProvider, GrokWebhookProvider
from .routing import configured_provider_mode, provider_readiness, validate_routing_config
from .runtime import Policy
from .worker_tools import tools_schema_sha256, worker_tools
from .application import Application

_API_PROVIDERS = {
    'gemini_api': (GeminiProvider, 1024, 60),
    'openai_api': (OpenAIAPIProvider, 4096, 90),
    'anthropic_api': (AnthropicAPIProvider, 4096, 90),
}


def validate_configuration(config):
    """Validate startup configuration without state, network or credential reads."""
    if config.get('enabled') is not True:
        raise ValueError('service is disabled; configure identity and native assistant before enabling')
    log_level(config.get('mcp_log_level', 'INFO'))
    auth = JWTAuthorizer(config['resource'], config['issuers'], config['grants'])
    provider_modes = {
        name: configured_provider_mode(item)
        for name, item in config['providers'].items()
    }
    policies = {}
    for owner, item in config['projects'].items():
        routing = item.get('routing')
        if routing is not None:
            validate_routing_config(
                routing, item['models'], provider_modes=provider_modes,
                project_providers=set(item['providers'].values()),
            )
        policies[owner] = Policy(
            providers=item['providers'],
            models={key: set(models) for key, models in item['models'].items()},
            max_active=item.get('max_active', 4),
            max_timeout_seconds=item.get('max_timeout_seconds', 3600),
            routing=routing,
        )
    owner = config.get('owner')
    if owner is not None and (not valid_owner(owner)
            or any(not project_belongs_to(project, owner) for project in policies)):
        raise ValueError('service projects must belong to its configured owner')
    for grant in config['grants']:
        if owner is not None and (grant.get('owner', owner) != owner
                or any(not project_belongs_to(project, owner) for project in grant_projects(grant))):
            raise ValueError('grant is outside the configured service owner')
        if 'owner' in grant and any(not project_belongs_to(project, grant['owner']) for project in policies):
            raise ValueError('owner worker cannot span service owners')
        if 'projects' in grant and not grant_projects(grant) <= policies.keys():
            raise ValueError('worker project list must be explicitly admitted')
    Path(config['state_path'])
    limit = config.get('max_request_bytes', 2_000_000)
    if type(limit) is not int or not 1 <= limit <= 16_000_000:
        raise ValueError('invalid request size limit')
    dot_count = 0
    for item in config['providers'].values():
        kind = item['type']
        if kind == 'dot_events':
            dot_count += 1
        elif kind in {'grok_webhook', 'claude_routine'}:
            if item.get('enabled') is not True:
                continue
            item['key_env']
            # A synthetic key validates static constructor arguments only.
            provider_class = ClaudeRoutineProvider if kind == 'claude_routine' else GrokWebhookProvider
            provider_class(item['url'], 'configuration-check', config['resource'])
        elif kind == 'claudeduet':
            # Duet is not Claude Code Routines; no verified invocation protocol yet.
            if item.get('enabled') is True:
                raise ValueError('Claude Duet invocation is not configured')
        elif kind == 'muse':
            # Muse Wonder wakes through Cloudflare event subscriptions; never
            # Cursor Cloud Agents.
            continue
        elif kind == 'chatgpt_plan_oauth':
            # Unauthorized entries stay as routing placeholders (same as disabled gemini).
            item['token_env']
            if item.get('plan_usage_authorized') is not True:
                continue
            ChatGPTPlanProvider(lambda: None, plan_usage_authorized=True)
        elif kind in _API_PROVIDERS:
            if item.get('enabled') is not True:
                continue
            item['key_env']
            provider_cls, default_max, default_timeout = _API_PROVIDERS[kind]
            provider_cls(lambda: None, model=item['model'],
                api_usage_authorized=item.get('api_usage_authorized', False),
                max_output_tokens=item.get('max_output_tokens', default_max),
                timeout_seconds=item.get('timeout_seconds', default_timeout),
                tier_models=item.get('tier_models'))
        else:
            raise ValueError('unsupported native provider')
    if dot_count > 1:
        raise ValueError('one Dot event provider per service deployment')
    validate_apps(config)
    return auth, policies


def check_worker_tools(config, identity, expected_projects):
    """Check owner-worker schemas without creating runtime, subscriptions or a JWT."""
    _, policies = validate_configuration(config)
    grants = [grant for grant in config['grants'] if grant.get('identity') == identity]
    if len(grants) != 1:
        raise ValueError('worker selector must match exactly once')
    grant = grants[0]
    scopes = frozenset(grant['scopes'])
    if scopes != {'tasks:read', 'tasks:work', 'events:subscribe'}:
        raise ValueError('expected a read/work/events worker without submit scope')
    if not grant.get('owner') or grant['owner'] != config.get('owner'):
        raise ValueError('worker must match the configured service owner')
    provider = grant.get('provider')
    provider_cfg = config['providers'].get(provider, {})
    if (provider_cfg.get('type') not in {'dot_events', 'muse'}
            or provider_cfg.get('enabled') is not True):
        raise ValueError('worker must use an enabled event provider')
    expected = sorted(set(expected_projects))
    if not expected or len(expected) != len(expected_projects):
        raise ValueError('expected projects must be nonempty and unique')
    principal = Principal(identity, None, scopes, provider, owner=grant['owner'])
    tools = worker_tools(principal, policies, provider)
    if {tool['name'] for tool in tools} != {'get_task', 'claim_task', 'report_task_result',
                                          'get_event_subscription', 'delete_event_subscription'} or len(tools) != 5:
        raise ValueError('expected five worker tools')
    for tool in tools:
        schema = tool['inputSchema']
        Draft202012Validator.check_schema(schema)
        if (schema['required'].count('project') != 1
                or schema['properties']['project']['enum'] != expected):
            raise ValueError('required project enum must match expected projects exactly')
    return {'valid': True, 'worker_match_count': 1, 'owner_matches': True,
            'provider_matches': True, 'worker_has_submit': False,
            'tool_count': len(tools), 'project_required_in_all': True,
            'project_enum_exact_match': True, 'tools_schema_sha256': tools_schema_sha256(tools),
            'preflight_kind': 'config_derived_worker_tools', 'real_oauth_verified': False,
            'running_process_verified': False}


def build_application(config, *, github_key_loader=None, github_transport=None):
    # Route runtime, env, and validation through server so unittest patches on
    # ``ai_service.server`` stay effective (historical monolith import surface).
    from . import server as srv
    auth, policies = srv.validate_configuration(config)
    providers = {}
    events_holder = {'buses': {}}

    def provider_ready(name, owner):
        buses = events_holder['buses']
        item = config['providers'].get(name) if isinstance(config.get('providers'), dict) else None
        kind = item.get('type') if isinstance(item, dict) else None
        bus = buses.get(name)
        active = bus.has_active(owner) if bus is not None else False
        return provider_readiness(
            name, env=dict(srv.os.environ), configured_providers=config['providers'],
            has_dot_subscription=active if kind == 'dot_events' else False,
            has_muse_subscription=active if kind == 'muse' else False,
        )

    def provider_mode(name):
        item = config['providers'].get(name) if isinstance(config.get('providers'), dict) else None
        mode = configured_provider_mode(item)
        if mode is not None:
            return mode
        provider = providers.get(name)
        return getattr(provider, 'mode', None)

    def allows_tier_model(name, owner, tier, model):
        item = config['providers'].get(name) if isinstance(config.get('providers'), dict) else None
        if not isinstance(item, dict) or model not in policies[owner].models.get(name, set()):
            return False
        tier_models = item.get('tier_models')
        if isinstance(tier_models, dict):
            candidates = tier_models.get(tier)
            return isinstance(candidates, list) and model in candidates
        fixed_model = item.get('model')
        return fixed_model == model if isinstance(fixed_model, str) else False

    def any_tier_model(name, owner, tier, fallback):
        item = config['providers'].get(name) if isinstance(config.get('providers'), dict) else None
        admitted = policies[owner].models.get(name, set())
        if not isinstance(item, dict):
            return False
        if item.get('type') == 'chatgpt_plan_oauth':
            return fallback in admitted
        tier_models = item.get('tier_models')
        if isinstance(tier_models, dict):
            candidates = tier_models.get(tier)
            return isinstance(candidates, list) and any(m in admitted for m in candidates)
        fixed_model = item.get('model')
        return fixed_model == fallback and fixed_model in admitted if isinstance(fixed_model, str) else False

    def select_api_model_fn(name, req, decision, admitted):
        item = config['providers'].get(name) if isinstance(config.get('providers'), dict) else None
        tier_models = item.get('tier_models') if isinstance(item, dict) else None
        if not isinstance(item, dict) or tier_models is None:
            return decision.model
        provider_obj = providers.get(name)
        env_key = item.get('key_env', '')
        key_client = getattr(provider_obj, '_key_client', lambda env_key=env_key: srv.os.environ.get(env_key))
        return select_api_model(
            item.get('type'), key_client,
            tier_models=tier_models, tier=decision.tier, decision_model=decision.model,
            request=req, admitted=admitted, transport=getattr(provider_obj, '_transport', None),
        )

    runtime = srv.TaskRuntime(
        Path(config['state_path']), providers, policies,
        provider_ready=provider_ready, provider_mode=provider_mode,
        allows_tier_model=allows_tier_model, any_tier_model=any_tier_model,
        select_api_model=select_api_model_fn,
    )
    events = None
    for name, item in config['providers'].items():
        if item['type'] in {'grok_webhook', 'claude_routine'}:
            if item.get('enabled') is not True:
                continue
            key = srv.os.environ.get(item['key_env'])
            provider_class = ClaudeRoutineProvider if item['type'] == 'claude_routine' else GrokWebhookProvider
            providers[name] = provider_class(item['url'], key, config['resource'])
        elif item['type'] == 'claudeduet':
            # validate_configuration rejects enablement before reading credentials.
            continue
        elif item['type'] == 'muse':
            if item.get('enabled') is not True:
                continue
            if name in events_holder['buses']:
                raise ValueError('one Muse event provider per service deployment')
            muse_events = EventSubscriptions(runtime, provider_name=name, can_access=auth.can_access_events)
            events_holder['buses'][name] = muse_events
            providers[name] = DotEventProvider(muse_events)
        elif item['type'] == 'dot_events':
            if item.get('enabled') is not True:
                continue
            if name in events_holder['buses'] or events is not None:
                raise ValueError('one Dot event provider per service deployment')
            events = EventSubscriptions(runtime, provider_name=name, can_access=auth.can_access_events)
            if name != 'dot':
                # Legacy rows without a side-table entry are Dot; claim them once
                # under the configured Dot provider key (never from Muse).
                with runtime.connection() as db:
                    db.execute(
                        'INSERT OR IGNORE INTO subscription_providers(subscription, provider) '
                        'SELECT id, ? FROM subscriptions '
                        'WHERE id NOT IN (SELECT subscription FROM subscription_providers)',
                        (name,))
            events_holder['buses'][name] = events
            providers[name] = DotEventProvider(events)
        elif item['type'] == 'chatgpt_plan_oauth':
            if item.get('plan_usage_authorized') is not True:
                continue
            environment_name = item['token_env']
            providers[name] = ChatGPTPlanProvider(lambda name=environment_name: srv.os.environ.get(name),
                                                  plan_usage_authorized=True)
        elif item['type'] in _API_PROVIDERS:
            if item.get('enabled') is not True:
                continue
            environment_name = item['key_env']
            provider_cls, default_max, default_timeout = _API_PROVIDERS[item['type']]
            providers[name] = provider_cls(lambda name=environment_name: srv.os.environ.get(name),
                model=item['model'], api_usage_authorized=item.get('api_usage_authorized', False),
                max_output_tokens=item.get('max_output_tokens', default_max),
                timeout_seconds=item.get('timeout_seconds', default_timeout),
                tier_models=item.get('tier_models'))
        else:
            raise ValueError('unsupported native provider')
    if config.get('github_apps') is not None:
        GitHubComments(runtime, GitHubApps(config, key_loader=github_key_loader, transport=github_transport))
    return Application(runtime, auth, events, event_buses=events_holder['buses'])
