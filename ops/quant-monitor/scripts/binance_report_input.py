"""Convert an already-authorized execution report in memory; no source access."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sync_binance_live_runs import (
    BinanceLiveRunsError, DOMAIN, LIFECYCLE_SCHEMA, STRATEGY_PROFILE,
    _parse_utc, _validate_interval,
)

# Change the schema when the rules for skipping a report change.
ARCHIVE_SKIP_CACHE_SCHEMA = "quant_monitor_binance_archive_skips.v1"
MAX_ARCHIVE_SKIPS = 8192


def valid_stream_id(value: Any) -> bool:
    """Accept only IDs whose QPK storage key is identical and collision-free."""
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?", value))


def report_to_live_record(
    report: Mapping[str, Any], *, stream_id: str, account_scope_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Project a verified archive report; callers own source authorization and ordering."""
    if not valid_stream_id(stream_id):
        raise BinanceLiveRunsError("stream_id_invalid")
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (account_scope_sha256, source_sha256)):
        raise BinanceLiveRunsError("report_binding_invalid")
    if (not isinstance(report, Mapping) or report.get("schema_version") != "runtime_report.v1"
            or report.get("platform") != "binance"
            or report.get("strategy_profile") != STRATEGY_PROFILE
            or report.get("strategy_domain") != DOMAIN or report.get("dry_run") is not False):
        raise BinanceLiveRunsError("report_scope_mismatch")
    status = report.get("status")
    if not isinstance(status, str) or status not in {"ok", "aborted", "error", "unknown"}:
        raise BinanceLiveRunsError("report_status_invalid")
    started = _parse_utc(report.get("started_at"))
    interval = report.get("external_cash_flow_interval")
    if interval is not None:
        _validate_interval(interval, unknown_day=started.date().isoformat())
        if interval["account_scope_sha256"] != account_scope_sha256:
            raise BinanceLiveRunsError("report_account_scope_mismatch")
    return {
        "schema_version": LIFECYCLE_SCHEMA,
        "strategy_profile": STRATEGY_PROFILE,
        "domain": DOMAIN,
        "recorded_at": started.isoformat(),
        "record_kind": "execution",
        "lifecycle_stream_id": stream_id,
        "execution_result": {
            "platform": "binance", "status": status, "external_cash_flow": None,
            "external_cash_flow_interval": dict(interval) if interval is not None else None,
            "total_equity_usdt": report.get("total_equity_usdt"),
            "trend_equity_usdt": report.get("trend_equity_usdt"),
            "degraded_mode_level": report.get("degraded_mode_level"),
        },
        "_source": {"archive_report_sha256": source_sha256},
    }


def _record_core(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "_source"}


def _archive_read_fingerprint(record, archive_uri, account_scope_sha256):
    source = record["_source"]
    # A cache hit replaces a remote read, so bind the stored projection to
    # the immutable object generation and the currently authorized archive.
    verified = [archive_uri, account_scope_sha256, source.get("object_name_sha256"),
                source.get("object_generation"), source.get("archive_report_sha256"), _record_core(record)]
    return hashlib.sha256(json.dumps(verified, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare_archive_records(
    list_page, read_object, *, prefix: str, stream_id: str,
    account_scope_sha256: str, existing_records: list[Mapping[str, Any]], max_pages: int = 100,
    archive_uri: str = "", provenance_updates: list[dict[str, Any]] | None = None,
    skip_cache: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Prepare additions only after complete pagination and conflict checks.

    Caller must authenticate the exact archive prefix and provide a successfully
    read local-store snapshot. Callbacks never log bodies. No storage is written;
    the existing single-writer input adapter can save the additions to its local
    PerformanceStore before committing its input bundle. A bound archive URI
    allows reuse of verified generations. Legacy provenance can be refreshed
    separately from additions, after the full scan succeeds.
    A caller-validated skip cache is replaced only after a complete scan.
    """
    if (not isinstance(prefix, str) or not prefix or not prefix.endswith("/")
            or not valid_stream_id(stream_id)
            or not isinstance(account_scope_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", account_scope_sha256)
            or not isinstance(existing_records, list) or type(max_pages) is not int
            or not 1 <= max_pages <= 1000):
        raise BinanceLiveRunsError("archive_binding_invalid")
    existing = {}
    for record in existing_records:
        if (not isinstance(record, Mapping) or record.get("schema_version") != LIFECYCLE_SCHEMA
                or record.get("record_kind") != "execution"
                or record.get("strategy_profile") != STRATEGY_PROFILE
                or record.get("domain") != DOMAIN or record.get("lifecycle_stream_id") != stream_id
                or not isinstance(record.get("recorded_at"), str)):
            raise BinanceLiveRunsError("existing_record_invalid")
        result = record.get("execution_result")
        if (not isinstance(result, Mapping) or result.get("platform") != "binance"
                or "external_cash_flow_interval" not in result):
            raise BinanceLiveRunsError("existing_record_invalid")
        identity = record["recorded_at"]
        _parse_utc(identity)
        interval = result["external_cash_flow_interval"]
        if interval is not None:
            _validate_interval(interval, unknown_day=identity[:10])
            if interval["account_scope_sha256"] != account_scope_sha256:
                raise BinanceLiveRunsError("existing_account_scope_mismatch")
        if identity in existing and _record_core(existing[identity]) != _record_core(record):
            raise BinanceLiveRunsError("existing_record_conflict")
        existing[identity] = record

    verified = {}
    if archive_uri:
        for record in existing.values():
            source = record.get("_source")
            if (isinstance(source, Mapping) and source.get("archive_read_fingerprint")
                    and source["archive_read_fingerprint"] == _archive_read_fingerprint(
                        record, archive_uri, account_scope_sha256)):
                verified[(source["object_name_sha256"], source["object_generation"])] = record

    pending = {}
    reused = set()
    next_skips = {}
    tokens = set()
    token = None

    def remember_skip(object_key):
        if skip_cache is not None and len(next_skips) < MAX_ARCHIVE_SKIPS:
            name_hash, generation = object_key
            next_skips[name_hash] = generation

    with ThreadPoolExecutor(max_workers=4) as executor:
        for _ in range(max_pages):
            try:
                objects, next_token = list_page(token)
            except Exception as exc:
                raise BinanceLiveRunsError("archive_list_failed") from exc
            if not isinstance(objects, list) or (next_token is not None and not isinstance(next_token, str)):
                raise BinanceLiveRunsError("archive_page_invalid")
            for batch_start in range(0, len(objects), 4):
                batch = objects[batch_start:batch_start + 4]
                for item in batch:
                    if (not isinstance(item, Mapping) or not isinstance(item.get("name"), str)
                            or not item["name"].startswith(prefix) or not item["name"].endswith(".json")
                            or not isinstance(item.get("generation"), str)
                            or not item["generation"].isdigit()):
                        raise BinanceLiveRunsError("archive_object_invalid")
                reads = []
                for item in batch:
                    object_key = (hashlib.sha256(item["name"].encode()).hexdigest(), item["generation"])
                    record = verified.get(object_key)
                    if record is None and skip_cache is not None and skip_cache.get(object_key[0]) == object_key[1]:
                        remember_skip(object_key)
                        continue
                    future = None if record is not None else executor.submit(
                        read_object, item["name"], item["generation"])
                    reads.append((object_key, record, future))
                for object_key, record, future in reads:
                    if future is not None:
                        try:
                            raw = future.result()
                        except Exception as exc:
                            raise BinanceLiveRunsError("archive_read_failed") from exc
                        if not isinstance(raw, bytes) or not raw or len(raw) > 2 * 1024 * 1024:
                            raise BinanceLiveRunsError("archive_report_invalid")
                        try:
                            report = json.loads(raw)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise BinanceLiveRunsError("archive_report_invalid") from exc
                        if not isinstance(report, Mapping):
                            raise BinanceLiveRunsError("archive_report_invalid")
                        # A selected archive prefix may contain other profiles or paper runs.
                        if (report.get("platform") not in (None, "binance")
                                or report.get("strategy_profile") not in (None, STRATEGY_PROFILE)
                                or report.get("strategy_domain") not in (None, DOMAIN)
                                or report.get("dry_run") is True):
                            remember_skip(object_key)
                            continue
                        interval = report.get("external_cash_flow_interval")
                        scope = interval.get("account_scope_sha256") if isinstance(interval, Mapping) else None
                        if (isinstance(scope, str) and re.fullmatch(r"[0-9a-f]{64}", scope)
                                and scope != account_scope_sha256):
                            remember_skip(object_key)
                            continue
                        digest = hashlib.sha256(raw).hexdigest()
                        record = report_to_live_record(report, stream_id=stream_id,
                                                       account_scope_sha256=account_scope_sha256,
                                                       source_sha256=digest)
                        record["_source"]["object_name_sha256"] = object_key[0]
                        record["_source"]["object_generation"] = object_key[1]
                        if archive_uri:
                            record["_source"]["archive_read_fingerprint"] = _archive_read_fingerprint(
                                record, archive_uri, account_scope_sha256)
                    else:
                        reused.add(record["recorded_at"])
                    identity = record["recorded_at"]
                    previous = pending.get(identity)
                    if previous is not None and _record_core(previous) != _record_core(record):
                        raise BinanceLiveRunsError("archive_record_conflict")
                    if previous is None:
                        pending[identity] = record
            if next_token is None:
                break
            if not next_token or next_token in tokens:
                raise BinanceLiveRunsError("archive_pagination_invalid")
            tokens.add(next_token)
            token = next_token
        else:
            raise BinanceLiveRunsError("archive_pagination_incomplete")
    additions = []
    updates = []
    for identity, record in sorted(pending.items()):
        if identity in existing:
            previous = existing[identity]
            if _record_core(previous) != _record_core(record):
                raise BinanceLiveRunsError("existing_record_conflict")
            if archive_uri and provenance_updates is not None and identity not in reused:
                source = previous.get("_source")
                source = source if isinstance(source, Mapping) else {}
                updates.append({**previous, "_source": {**source, **record["_source"]}})
        else:
            additions.append(record)
    if provenance_updates is not None:
        provenance_updates.extend(updates)
    if skip_cache is not None:
        skip_cache.clear()
        skip_cache.update(next_skips)
    return additions
