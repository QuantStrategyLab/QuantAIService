"""Synthetic conversion checks; no archive, network or production writes."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from binance_report_input import prepare_archive_records, report_to_live_record
from sync_binance_live_runs import BinanceLiveRunsError


def report():
    return {
        "schema_version": "runtime_report.v1", "platform": "binance", "strategy_profile": "crypto_live_pool_rotation",
        "strategy_domain": "crypto", "dry_run": False, "status": "ok",
        "started_at": "2026-10-01T00:00:00Z",
        "external_cash_flow_interval": {
            "account_scope_sha256": "a" * 64, "start_at": "2026-09-30T23:00:00Z",
            "end_at": "2026-10-01T00:00:00Z", "end_equity_usdt": "100",
            "net_external_cash_flow": "0", "currency": "USDT",
            "valuation_basis": "checkpoint_quantities_sampled_prices",
        },
    }


def convert(value):
    return report_to_live_record(value, stream_id="synthetic", account_scope_sha256="a" * 64,
                                 source_sha256="b" * 64)


def test_valid_report_projects_only_qualified_fields():
    value = report()
    value["private_extra"] = "not copied"
    record = convert(value)
    assert record["execution_result"]["external_cash_flow_interval"] == value["external_cash_flow_interval"]
    assert record["recorded_at"] == "2026-10-01T00:00:00+00:00"
    assert "private_extra" not in record
    assert record["_source"]["archive_report_sha256"] == "b" * 64


def test_missing_interval_is_unknown_not_zero():
    value = report()
    value.pop("external_cash_flow_interval")
    assert convert(value)["execution_result"]["external_cash_flow_interval"] is None


@pytest.mark.parametrize("field,bad", [("dry_run", True), ("platform", "other"),
    ("strategy_profile", "other"), ("strategy_domain", "us_equity"), ("status", "unexpected")])
def test_untrusted_report_rejected(field, bad):
    value = report()
    value[field] = bad
    with pytest.raises(BinanceLiveRunsError):
        convert(value)


def test_wrong_scope_and_invalid_interval_are_rejected():
    value = report()
    value["external_cash_flow_interval"]["account_scope_sha256"] = "c" * 64
    with pytest.raises(BinanceLiveRunsError, match="report_account_scope_mismatch"):
        convert(value)
    value = report()
    value["external_cash_flow_interval"]["net_external_cash_flow"] = None
    with pytest.raises(BinanceLiveRunsError, match="external_cash_flow_interval_invalid"):
        convert(value)


def prepare(pages, bodies, existing=None):
    return prepare_archive_records(lambda token: pages[token], lambda name, generation: bodies[(name, generation)],
        prefix="protected/profile/", stream_id="synthetic",
        account_scope_sha256="a" * 64, existing_records=existing or [])


def test_pagination_and_idempotency():
    item = {"name": "protected/profile/one.json", "generation": "1"}
    raw = json.dumps(report()).encode()
    pages = {None: ([], "next"), "next": ([item], None)}
    bodies = {(item["name"], "1"): raw}
    records = prepare(pages, bodies)
    assert len(records) == 1
    assert prepare(pages, bodies, records) == []
    conflicting = dict(records[0], execution_result=dict(records[0]["execution_result"], status="error"))
    with pytest.raises(BinanceLiveRunsError, match="existing_record_conflict"):
        prepare(pages, bodies, [conflicting])


def test_pagination_failure_and_wrong_source_fail_closed():
    with pytest.raises(BinanceLiveRunsError, match="archive_pagination_invalid"):
        prepare({None: ([], "next"), "next": ([], "next")}, {})
    with pytest.raises(BinanceLiveRunsError, match="archive_object_invalid"):
        prepare({None: ([{"name": "elsewhere/report.json", "generation": "1"}], None)}, {})
    with pytest.raises(BinanceLiveRunsError, match="archive_read_failed"):
        prepare({None: ([{"name": "protected/profile/one.json", "generation": "1"}], None)}, {})


def test_archive_reads_use_at_most_four_workers_and_keep_all_generations():
    barrier = threading.Barrier(4, timeout=5)
    lock = threading.Lock()
    active = 0
    peak = 0
    seen = []
    items = [{'name': f'protected/profile/{i}.json', 'generation': str(i)} for i in range(1, 5)]
    def read(name, generation):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            seen.append((name, generation))
        barrier.wait()
        with lock:
            active -= 1
        return json.dumps(report()).encode()
    result = prepare_archive_records(lambda _token: (items, None), read, prefix='protected/profile/',
                                     stream_id='synthetic', account_scope_sha256='a' * 64,
                                     existing_records=[])
    assert peak == 4
    assert sorted(seen) == sorted((item['name'], item['generation']) for item in items)
    assert len(result) == 1


def test_duplicate_identity_with_different_content_rejected():
    first, second = report(), report()
    second["status"] = "error"
    items = [{"name": "protected/profile/one.json", "generation": "1"},
             {"name": "protected/profile/two.json", "generation": "2"}]
    bodies = {(items[0]["name"], "1"): json.dumps(first).encode(),
              (items[1]["name"], "2"): json.dumps(second).encode()}
    with pytest.raises(BinanceLiveRunsError, match="archive_record_conflict"):
        prepare({None: (items, None)}, bodies)


def test_schema_mismatch_rejected():
    value = report()
    value["schema_version"] = "other"
    with pytest.raises(BinanceLiveRunsError, match="report_scope_mismatch"):
        convert(value)


def test_existing_performance_store_round_trip(tmp_path):
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore

    store = PerformanceStore(local_root=tmp_path)
    item = {"name": "protected/profile/one.json", "generation": "1"}
    pages = {None: ([item], None)}
    bodies = {(item["name"], "1"): json.dumps(report()).encode()}
    records = prepare(pages, bodies)
    store.save_live_run_record("crypto_live_pool_rotation", "crypto", records[0], stream_id="synthetic")
    existing = store.list_live_run_records("crypto", strategy_profile="crypto_live_pool_rotation", stream_id="synthetic")
    assert prepare(pages, bodies, existing) == []


def test_interrupted_local_saves_are_idempotent_on_retry(tmp_path):
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore

    store = PerformanceStore(local_root=tmp_path)
    first, second = report(), report()
    second["started_at"] = "2026-10-01T01:00:00Z"
    second["external_cash_flow_interval"]["start_at"] = "2026-10-01T00:00:00Z"
    second["external_cash_flow_interval"]["end_at"] = "2026-10-01T01:00:00Z"
    items = [{"name": "protected/profile/one.json", "generation": "1"},
             {"name": "protected/profile/two.json", "generation": "2"}]
    bodies = {(items[0]["name"], "1"): json.dumps(first).encode(),
              (items[1]["name"], "2"): json.dumps(second).encode()}
    pages = {None: (items, None)}
    additions = prepare(pages, bodies)
    store.save_live_run_record("crypto_live_pool_rotation", "crypto", additions[0], stream_id="synthetic")
    existing = store.list_live_run_records("crypto", strategy_profile="crypto_live_pool_rotation", stream_id="synthetic")
    retry = prepare(pages, bodies, existing)
    assert retry == additions[1:]
    store.save_live_run_record("crypto_live_pool_rotation", "crypto", retry[0], stream_id="synthetic")
    existing = store.list_live_run_records("crypto", strategy_profile="crypto_live_pool_rotation", stream_id="synthetic")
    assert prepare(pages, bodies, existing) == []


def test_unrelated_profiles_and_paper_runs_do_not_block_bound_source():
    good, other, paper = report(), report(), report()
    other['strategy_profile'] = 'other'
    paper['dry_run'] = True
    items = [{'name': f'protected/profile/{i}.json', 'generation': str(i)} for i in range(1, 4)]
    bodies = {(item['name'], item['generation']): json.dumps(value).encode()
              for item, value in zip(items, (good, other, paper))}
    assert len(prepare({None: (items, None)}, bodies)) == 1


def test_equal_payload_different_source_metadata_preserves_existing():
    item = {'name': 'protected/profile/one.json', 'generation': '1'}
    pages = {None: ([item], None)}
    bodies = {(item['name'], '1'): json.dumps(report()).encode()}
    record = prepare(pages, bodies)[0]
    existing = dict(record, _source={'entries': [{'kind': 'artifact'}]})
    assert prepare(pages, bodies, [existing]) == []


def test_existing_other_account_or_malformed_interval_is_rejected_even_without_new_reports():
    item = {'name': 'protected/profile/one.json', 'generation': '1'}
    records = prepare({None: ([item], None)}, {(item['name'], '1'): json.dumps(report()).encode()})
    import copy
    other = copy.deepcopy(records[0])
    other['execution_result']['external_cash_flow_interval']['account_scope_sha256'] = 'c' * 64
    with pytest.raises(BinanceLiveRunsError, match='existing_account_scope_mismatch'):
        prepare({None: ([], None)}, {}, [other])
    malformed = copy.deepcopy(records[0])
    malformed['execution_result']['external_cash_flow_interval']['net_external_cash_flow'] = None
    with pytest.raises(BinanceLiveRunsError, match='external_cash_flow_interval_invalid'):
        prepare({None: ([], None)}, {}, [malformed])
    gap = copy.deepcopy(records[0])
    gap['execution_result']['external_cash_flow_interval'] = None
    assert prepare({None: ([], None)}, {}, [gap]) == []
