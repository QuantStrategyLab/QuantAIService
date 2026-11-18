"""Refresh API tier model rosters from official provider pricing documents."""
from __future__ import annotations

import http.client
import os
import re
from decimal import Decimal
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .gemini import MODEL_PATTERN, TIER_KEYS

PRICE_CONFIG = {
    'openai': ('https://developers.openai.com', '/api/docs/pricing.md'),
    'anthropic': ('https://platform.claude.com', '/docs/en/about-claude/pricing.md'),
    'gemini': ('https://ai.google.dev', '/gemini-api/docs/pricing.md'),
}
_DATED_SUFFIX = re.compile(r'-(?:\d{4}-\d{2}-\d{2}|\d{8})$')
_DOLLAR = re.compile(r'\$(\d+(?:\.\d+)?)(?:\s*/\s*(?:1M(?:\s*tokens)?|MTok))?(?:<sup>\d+</sup>)?')
_ANTHROPIC_ROW = re.compile(r'Claude (Haiku|Sonnet|Opus) (\d+(?:\.\d+)*)')
_GEMINI_ROW = re.compile(r'(gemini-\d+(?:\.\d+)*-[a-z0-9.-]+)', re.IGNORECASE)
_GEMINI_HEADING = re.compile(r'Gemini\s+(\d+(?:\.\d+)*)\s+(Flash-Lite|Flash\s+Lite|Flash|Pro)(?:\s+(Preview))?', re.IGNORECASE)
MAX_BYTES = 512_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_pricing(url: str) -> str:
    """Fetch one official HTTPS pricing document without following redirects."""
    request = Request(
        url,
        headers={
            'Accept': 'text/markdown, text/html;q=0.9',
            'User-Agent': 'Mozilla/5.0 (compatible; AIServiceModelPolicy/1.0)',
        },
    )
    with build_opener(_NoRedirect()).open(request, timeout=12) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('pricing document too large')
    return body.decode('utf-8')


def pricing_url(provider: str, environ: dict[str, str] | None = None) -> str:
    """Build a validated HTTPS pricing URL for a supported API provider."""
    if provider not in PRICE_CONFIG:
        raise ValueError(f'unsupported pricing provider: {provider}')
    default_origin, path = PRICE_CONFIG[provider]
    source = os.environ if environ is None else environ
    override = source.get(provider.upper() + '_PRICING_ORIGIN', '').strip()
    origin_text = override or default_origin
    origin = urlsplit(origin_text)
    if (origin.scheme != 'https' or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.port not in (None, 443)
            or origin.path not in ('', '/') or origin.query or origin.fragment):
        raise ValueError('configure a trusted HTTPS pricing origin')
    return 'https://' + origin.netloc + path


def model_version(family_regex: re.Pattern[str], model: str) -> tuple[int, ...]:
    """Extract comparable integer version tuples for a configured model family regex."""
    base = _DATED_SUFFIX.sub('', model)
    match = family_regex.fullmatch(base)
    if not match or match.lastindex is None or match.lastindex < 1:
        return ()
    return tuple(int(part) for part in re.split(r'[.-]', match[1]))


def _parse_cell_price(cell: str) -> Decimal | None:
    match = _DOLLAR.search(cell.strip())
    return Decimal(match[1]) if match else None


def _parse_openai_rates(document: str) -> dict[str, tuple[Decimal, Decimal]]:
    lines = document.splitlines()
    try:
        start = lines.index('### Standard pricing data') + 1
    except ValueError:
        return {}
    rates: dict[str, tuple[Decimal, Decimal]] = {}
    columns: list[str] = []
    for line in lines[start:]:
        if line.startswith('#'):
            break
        if not line.startswith('|'):
            columns = []
            continue
        fields = [field.strip() for field in line.strip('|').split('|')]
        if fields[0] == 'Model':
            columns = fields
            continue
        if len(fields) != len(columns) or not columns:
            continue
        row = dict(zip(columns, fields))
        name = fields[0].strip('`')
        if not MODEL_PATTERN.fullmatch(name):
            continue
        pairs = [('Input', 'Output')] if 'Input' in row else [
            ('Short context input', 'Short context output'),
            ('Long context input', 'Long context output'),
        ]
        in_prices = [_parse_cell_price(row.get(inp, '')) for inp, _ in pairs]
        out_prices = [_parse_cell_price(row.get(out, '')) for _, out in pairs]
        prev = rates.get(name, (Decimal(0), Decimal(0)))
        in_val = max([prev[0], *(p for p in in_prices if p is not None)]) if all(p is not None for p in in_prices) else Decimal('Infinity')
        out_val = max([prev[1], *(p for p in out_prices if p is not None)]) if all(p is not None for p in out_prices) else Decimal('Infinity')
        rates[name] = (in_val, out_val)
    return rates


def _parse_anthropic_rates(document: str) -> dict[str, tuple[Decimal, Decimal]]:
    lines = document.splitlines()
    try:
        start = lines.index('## Model pricing') + 1
    except ValueError:
        return {}
    rates: dict[str, tuple[Decimal, Decimal]] = {}
    columns: list[str] = []
    for line in lines[start:]:
        if line.startswith('#') and not line.startswith('###'):
            break
        if not line.startswith('|'):
            columns = []
            continue
        fields = [field.strip() for field in line.strip('|').split('|')]
        if fields[0] == 'Model':
            columns = fields
            continue
        if len(fields) != len(columns) or not columns:
            continue
        row = dict(zip(columns, fields))
        match = _ANTHROPIC_ROW.fullmatch(fields[0])
        if not match:
            continue
        name = f'claude-{match[1].lower()}-{match[2].replace(".", "-")}'
        in_price = _parse_cell_price(row.get('Base input tokens', ''))
        out_price = _parse_cell_price(row.get('Output tokens', ''))
        prev = rates.get(name, (Decimal(0), Decimal(0)))
        in_val = max(prev[0], in_price) if in_price is not None else Decimal('Infinity')
        out_val = max(prev[1], out_price) if out_price is not None else Decimal('Infinity')
        rates[name] = (in_val, out_val)
    return rates


def _normalize_gemini_name(text: str) -> str | None:
    hyphenated = _GEMINI_ROW.search(text)
    if hyphenated:
        return hyphenated[1].lower()
    heading = _GEMINI_HEADING.search(text)
    if not heading:
        return None
    variant = re.sub(r'\s+', '-', heading[2].strip().lower())
    suffix = '-preview' if heading[3] else ''
    return f'gemini-{heading[1]}-{variant}{suffix}'


def _parse_gemini_rates(document: str) -> dict[str, tuple[Decimal, Decimal]]:
    rates: dict[str, tuple[Decimal, Decimal]] = {}
    current_model: str | None = None
    in_ignored_section = False
    columns: list[str] = []
    for raw_line in document.splitlines():
        line = raw_line.strip()
        if line.startswith('#'):
            lower = line.lower()
            in_ignored_section = any(word in lower for word in ('batch', 'caching', 'cached', 'live api'))
            parsed_name = _normalize_gemini_name(line)
            if parsed_name:
                current_model = parsed_name
            columns = []
            continue
        if in_ignored_section or not line.startswith('|'):
            if not line.startswith('|'):
                columns = []
            continue
        fields = [field.strip() for field in line.strip('|').split('|')]
        if fields and any(col in fields for col in ('Input', 'Input price', 'Model')):
            columns = fields
            continue
        if not columns or len(fields) != len(columns):
            continue
        row = dict(zip(columns, fields))
        name = _normalize_gemini_name(fields[0]) or current_model
        if not name or not MODEL_PATTERN.fullmatch(name):
            continue
        in_cell = row.get('Input') or row.get('Input price') or ''
        out_cell = row.get('Output') or row.get('Output price') or ''
        in_price = _parse_cell_price(in_cell)
        out_price = _parse_cell_price(out_cell)
        if in_price is None or out_price is None:
            continue
        prev = rates.get(name, (Decimal(0), Decimal(0)))
        rates[name] = (max(prev[0], in_price), max(prev[1], out_price))
    return rates


def standard_rates(provider: str, document: str) -> dict[str, tuple[Decimal, Decimal]]:
    """Parse standard token rates (USD per 1M tokens) from a provider pricing document."""
    if provider == 'openai':
        return _parse_openai_rates(document)
    if provider == 'anthropic':
        return _parse_anthropic_rates(document)
    if provider == 'gemini':
        return _parse_gemini_rates(document)
    raise ValueError(f'unsupported pricing provider: {provider}')


def refresh_tier_models(
    provider: str,
    tier: str,
    policy: dict,
    *,
    fetch: Callable[[str], str] | None = None,
    document: str | None = None,
) -> tuple[list[str], str]:
    """Return updated approved model candidates and decision source for one tier."""
    if tier not in TIER_KEYS:
        raise ValueError(f'unsupported complexity tier: {tier}')
    baseline = list(policy['tier_models'][provider][tier])
    family_pattern = policy['tier_families'][provider][tier]
    family_regex = re.compile(family_pattern)
    raw_caps = policy['rate_caps'][provider][tier]
    caps = (Decimal(str(raw_caps[0])), Decimal(str(raw_caps[1])))

    if document is not None:
        rates = standard_rates(provider, document)
    else:
        try:
            url = pricing_url(provider)
            rates = standard_rates(provider, (fetch or fetch_pricing)(url))
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            rates = {}
    if not rates:
        return baseline, 'approved_baseline'

    affordable = {
        model: price for model, price in rates.items()
        if price[0] <= caps[0] and price[1] <= caps[1] and MODEL_PATTERN.fullmatch(model)
    }
    matching = {model for model in affordable if model_version(family_regex, model)}
    newest_baseline = max((model_version(family_regex, model) for model in baseline), default=())
    updates = sorted(
        (model for model in matching if model_version(family_regex, model) > newest_baseline),
        key=lambda model: model_version(family_regex, model),
        reverse=True,
    )
    approved = [
        model for model in baseline
        if _DATED_SUFFIX.sub('', model) in affordable
    ]
    combined = list(dict.fromkeys(updates + approved))[:8]
    return combined, 'official_pricing'


def refresh_policy(
    policy: dict,
    *,
    fetch: Callable[[str], str] | None = None,
) -> tuple[dict, dict[str, dict[str, dict[str, object]]]]:
    """Refresh all configured API provider tiers and return updated policy + audit report."""
    docs: dict[str, str | None] = {}
    for provider in policy.get('tier_models', {}):
        try:
            docs[provider] = (fetch or fetch_pricing)(pricing_url(provider))
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            docs[provider] = None

    updated_tiers: dict[str, dict[str, list[str]]] = {}
    report: dict[str, dict[str, dict[str, object]]] = {}
    for provider, tiers in policy.get('tier_models', {}).items():
        updated_tiers[provider] = {}
        report[provider] = {}
        doc = docs[provider]
        for tier in tiers:
            if doc is None:
                candidates = list(policy['tier_models'][provider][tier])
                source = 'approved_baseline'
            else:
                candidates, source = refresh_tier_models(provider, tier, policy, document=doc)
            updated_tiers[provider][tier] = candidates
            report[provider][tier] = {
                'source': source,
                'changed': candidates != list(policy['tier_models'][provider][tier]),
                'candidates': candidates,
            }
    return {**policy, 'tier_models': updated_tiers}, report
