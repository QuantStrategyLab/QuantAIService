"""Refresh the audit roster from the providers' public standard token prices."""
import http.client
import os
import re
from decimal import Decimal
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PRICE_PATH = {
    'openai': '/api/docs/pricing.md',
    'anthropic': '/docs/en/about-claude/pricing.md',
}
FAMILY = {
    'openai': re.compile(r'gpt-(\d+(?:\.\d+)*)-mini'),
    'anthropic': re.compile(r'claude-sonnet-(\d+(?:-\d+)*)'),
}
HEADINGS = {'openai': '### Standard pricing data', 'anthropic': '## Model pricing'}
MAX_BYTES = 512_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_pricing(url):
    request = Request(url, headers={'Accept': 'text/markdown, text/html;q=0.9',
                                    'User-Agent': 'Mozilla/5.0 (compatible; MonthlyAuditPolicy/1.0)'})
    with build_opener(_NoRedirect()).open(request, timeout=12) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('pricing document too large')
    return body.decode('utf-8')


def version(provider, model):
    match = FAMILY[provider].fullmatch(model)
    if match:
        return tuple(int(part) for part in re.split(r'[.-]', match[1]))
    return ()


def standard_rates(provider, document):
    """Parse only the base/standard table, ignoring cached, batch and premium sections."""
    lines = document.splitlines()
    try:
        start = lines.index(HEADINGS[provider]) + 1
    except ValueError:
        return {}
    rates = {}
    columns = []
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
        name = fields[0]
        if provider == 'anthropic':
            match = re.fullmatch(r'Claude Sonnet (\d+(?:\.\d+)*)', name)
            if not match:
                continue
            name = 'claude-sonnet-' + match[1].replace('.', '-')
            pairs = [('Base input tokens', 'Output tokens')]
        else:
            if not version(provider, name):
                continue
            pairs = [('Input', 'Output')] if 'Input' in row else [
                ('Short context input', 'Short context output'), ('Long context input', 'Long context output')]
        amounts = [[], []]
        for pair in pairs:
            for i, column in enumerate(pair):
                match = re.fullmatch(r'\$(\d+(?:\.\d+)?)(?: / MTok)?(?:<sup>\d+</sup>)?', row.get(column, ''))
                if match:
                    amounts[i].append(Decimal(match[1]))
        # Ambiguous/missing prices never authorize a new model. Duplicate rows use the higher rate.
        previous = rates.get(name, (Decimal(0), Decimal(0)))
        rates[name] = tuple(max([previous[i], *prices]) if len(prices) == len(pairs)
                            else Decimal('Infinity') for i, prices in enumerate(amounts))
    return rates


def refresh_candidates(provider, tier, policy, fetch=None):
    """Return approved model IDs in preference order and the source of the decision."""
    baseline = policy[tier][provider]
    caps = [Decimal(str(value)) for value in policy['rate_caps'][tier][provider]]
    try:
        origin = urlsplit(os.environ.get(provider.upper() + '_PRICING_ORIGIN', ''))
        if (origin.scheme != 'https' or not origin.hostname or origin.username is not None or
                origin.password is not None or origin.port not in (None, 443) or
                origin.path not in ('', '/') or origin.query or origin.fragment):
            raise ValueError('configure a trusted HTTPS pricing origin')
        url = 'https://' + origin.netloc + PRICE_PATH[provider]
        rates = standard_rates(provider, (fetch or fetch_pricing)(url))
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        rates = {}
    if not rates:
        return list(baseline), 'approved_baseline'

    affordable = {model for model, price in rates.items()
                  if price[0] <= caps[0] and price[1] <= caps[1]}
    newest_approved = max(version(provider, model) for model in baseline)
    updates = sorted((model for model in affordable if version(provider, model) > newest_approved),
                     key=lambda model: version(provider, model), reverse=True)
    # Dated GPT snapshots share the base alias price. Unknown or over-cap rows are excluded.
    approved = [model for model in baseline
                if re.sub(r'-\d{4}-\d{2}-\d{2}$', '', model) in affordable]
    return updates + approved, 'official_pricing'
