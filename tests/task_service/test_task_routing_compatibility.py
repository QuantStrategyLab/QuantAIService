"""Check QAS registries against an independent routing contract fixture."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from quant_ai_service.task_routing import RoutingError, validate_routing
from tests.fixtures.routing_contract import ContractError, check_routing_contract


class RoutingCompatibilityTests(unittest.TestCase):
    def setUp(self):
        example = Path(__file__).resolve().parents[2] / 'config' / 'quant.example.json'
        self.projects = json.loads(example.read_text(encoding='utf-8'))['projects']
        self.routing = next(iter(self.projects.values()))['routing']

    def test_example_passes_validator_and_contract(self):
        example = Path(__file__).resolve().parents[2] / 'config' / 'quant.example.json'
        projects = json.loads(example.read_text(encoding='utf-8'))['projects']
        for project in projects.values():
            with self.subTest(project='synthetic example'):
                validate_routing(project['routing'])
                check_routing_contract(project['routing'])

    def test_review_incompatibilities_are_rejected_by_both(self):
        cases = []

        def case(label, path, value):
            bad = copy.deepcopy(self.routing)
            target = bad
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            cases.append((label, bad))

        for path in [
            ('complexity_tiers', 'complex', 'dot', 'reasoning_effort'),
            ('scenarios', 'pr_code_review', 'tier_overrides', 'complex', 'dot', 'reasoning_effort'),
        ]:
            case('xhigh', path, 'xhigh')
        for key in ('standard', 'complex'):
            for value in (0, -1, 1.5, True, 4000.0):
                case(f'threshold {key} {value!r}',
                     ('scenarios', 'pr_code_review', 'matching', 'size_thresholds', key), value)
        for key in self.routing['heuristics']:
            for value in (0, -1, 1.5, True, 4000.0):
                case(f'heuristic {key} {value!r}', ('heuristics', key), value)
        for key, value in [('size_simple_max', 4000), ('size_simple_max', 5000),
                           ('size_standard', 50000), ('size_standard', 60000),
                           ('size_complex', 4000), ('size_complex', 3000)]:
            case(f'non-increasing {key} {value}', ('heuristics', key), value)
        case('partial heuristics use defaults', ('heuristics',), {'size_standard': 1500})
        for name in ('1x', 'bad key!', 'a' * 65):
            case('category name', ('categories', name),
                 {'parent': None, 'default_complexity': 'standard'})
            case('scenario name', ('scenarios', name),
                 copy.deepcopy(self.routing['scenarios']['pr_code_review']))
        for name in ('bad key!', '-provider', 'p' * 65):
            case('tier provider name', ('complexity_tiers', 'simple', name),
                 {'model': 'synthetic-model', 'reasoning_effort': 'low'})
            case('override provider name',
                 ('scenarios', 'pr_code_review', 'tier_overrides', 'complex', name),
                 {'model': 'synthetic-model', 'reasoning_effort': 'low'})
        case('null scenario default',
             ('scenarios', 'pr_code_review', 'matching', 'default_complexity'), None)
        case('null category default', ('categories', 'quant', 'default_complexity'), None)
        for label, bad in cases:
            with self.subTest(case=label):
                with self.assertRaises(ContractError):
                    check_routing_contract(bad)
                with self.assertRaises(RoutingError):
                    validate_routing(bad)

    def test_effective_threshold_order_including_defaults(self):
        for thresholds in ({'standard': 50000}, {'complex': 4000},
                           {'standard': 6000, 'complex': 6000},
                           {'standard': 7000, 'complex': 6000}):
            bad = copy.deepcopy(self.routing)
            bad['scenarios']['pr_code_review']['matching']['size_thresholds'] = thresholds
            with self.subTest(thresholds=thresholds):
                with self.assertRaises(ContractError):
                    check_routing_contract(bad)
                with self.assertRaises(RoutingError):
                    validate_routing(bad)

    def test_positive_boundaries_and_omitted_matching_default(self):
        good = copy.deepcopy(self.routing)
        good['heuristics'] = {'size_simple_max': 1, 'size_standard': 2, 'size_complex': 3,
                              'image_complex_size': 1, 'materials_complex_count': 1}
        good['scenarios']['pr_code_review']['matching']['size_thresholds'] = {
            'standard': 1, 'complex': 2,
        }
        good['scenarios']['pr_code_review']['matching'].pop('default_complexity', None)
        validate_routing(good)
        check_routing_contract(good)
