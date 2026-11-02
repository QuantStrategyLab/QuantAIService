"""Independent test double for the routing compatibility subset.

This checks only effort, identifiers, complexity defaults and numeric bounds.
It is not a runtime validator and does not assert full service compatibility.
"""
import re


class ContractError(ValueError):
    """A value violates the compatibility subset."""


def _require(condition):
    if not condition:
        raise ContractError('routing compatibility contract violated')


def _identifier(value, *, provider=False):
    pattern = r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}' if provider else r'[A-Za-z][A-Za-z0-9_-]{0,63}'
    _require(isinstance(value, str) and re.fullmatch(pattern, value) is not None)


def _positive(values):
    _require(all(type(value) is int and value >= 1 for value in values))


def _default(mapping):
    if 'default_complexity' in mapping:
        _require(mapping['default_complexity'] in ('simple', 'standard', 'complex'))


def _selections(tiers):
    for selections in tiers.values():
        for provider, selection in selections.items():
            _identifier(provider, provider=True)
            _require(selection['reasoning_effort'] in ('low', 'medium', 'high'))


def check_routing_contract(routing):
    """Reject incompatible values without importing production validation code."""
    bounds = {'size_simple_max': 1500, 'size_standard': 4000, 'size_complex': 50000,
              'image_complex_size': 20000, 'materials_complex_count': 8}
    bounds.update(routing.get('heuristics', {}))
    _positive(bounds.values())
    _require(bounds['size_simple_max'] < bounds['size_standard'] < bounds['size_complex'])
    _selections(routing['complexity_tiers'])
    for name, category in routing.get('categories', {}).items():
        _identifier(name)
        if category.get('parent') is not None:
            _identifier(category['parent'])
        _default(category)
    for name, scenario in routing['scenarios'].items():
        _identifier(name)
        if 'category' in scenario:
            _identifier(scenario['category'])
        for provider in scenario['providers']:
            _identifier(provider, provider=True)
        _selections(scenario.get('tier_overrides', {}))
        matching = scenario.get('matching', {})
        _default(matching)
        thresholds = matching.get('size_thresholds', {})
        _positive(thresholds.values())
        _require(thresholds.get('standard', bounds['size_standard'])
                 < thresholds.get('complex', bounds['size_complex']))
