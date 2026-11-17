#!/usr/bin/env python3
"""VPS health cycle — route strategy evidence to issue-only optimization monitoring."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DOMAINS = ("cn_equity", "hk_equity", "us_equity", "crypto")
SCORE_ALERT = 60.0
DRIFT_REVIEW = 0.50
DRIFT_CRITICAL = 0.75
_ALERT_STATE_RELATIVE_PATH = Path("data/alert-state/health_cycle.json")
_DELIVERY_STATUSES = frozenset({"pending", "sent", "failed", "unknown"})
_DELIVERY_ATTEMPT_LIMIT = 2
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ALERT_STATE_THREAD_LOCK = threading.RLock()
_ALERT_STATE_LOCK_DEPTH = threading.local()
_CLOUD_CHECKPOINT = threading.local()
_ARTIFACT_STATUS_RELATIVE_PATH = Path("data/lifecycle-artifacts/status.json")
_ARTIFACT_STATUS_SCHEMA = "quant_monitor_lifecycle_artifact_status.v1"
_SAFE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")
_OPERATIONAL_ERROR_CODES = frozenset({
    "artifact_sync_status_unavailable",
    "artifact_sync_unexpected",
    "consumer_path_conflict",
    "drift_data_unavailable",
    "github_api_invalid",
    "github_api_rate_limit",
    "github_api_unavailable",
    "monitor_data_unavailable",
    "trusted_artifact_unavailable",
})
_GITHUB_UPSTREAM_CODES = frozenset({
    "github_api_invalid",
    "github_api_rate_limit",
    "github_api_unavailable",
})
_STATUS_FILE_REASONS = frozenset({
    "artifact_status_missing", "artifact_status_read_failed", "artifact_status_invalid",
    "artifact_status_stale", "artifact_status_future",
})
_SAFE_RESET_AT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:+.\-Z]{8,32}$")
_LIVE_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_BINANCE_LIVE_PROFILE = "crypto_live_pool_rotation"
_LIVE_MAX_LAG = timedelta(hours=2)
_LIVE_FUTURE_TOLERANCE = timedelta(minutes=5)
_OPERATIONAL_ERROR_TYPES = frozenset({
    "FileNotFoundError",
    "JSONDecodeError",
    "KeyError",
    "LifecycleArtifactError",
    "OSError",
    "RuntimeError",
    "TypeError",
    "ValueError",
})
_OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY = "QuantStrategyLab/QuantAIService"


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and _EVENT_ID_RE.fullmatch(value) is not None


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_bounded_str(value: Any, *, max_len: int = 200, no_space: bool = False) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= max_len:
        return False
    return not no_space or not any(char.isspace() for char in value)


def _is_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _optional_str(mapping: Any, key: str) -> str | None:
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(key)
    return value if isinstance(value, str) else None


def _optional_int(mapping: Any, key: str) -> int | None:
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(key)
    return value if type(value) is int else None


def _require_dict(value: Any, error_message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OSError(error_message)
    return value


class _LifecycleIdentity:
    __slots__ = ("domain", "strategy_profile", "platform")

    def __init__(self, *, domain: Any, strategy_profile: str | None, platform: Any) -> None:
        self.domain = domain
        self.strategy_profile = strategy_profile
        self.platform = platform


def _read_lifecycle_identity(record: Any, *, default_domain: Any = None) -> _LifecycleIdentity:
    """Extract duck-typed lifecycle attributes from QPK objects or test doubles."""
    if record is None:
        return _LifecycleIdentity(domain=default_domain, strategy_profile=None, platform=None)
    profile = getattr(record, "strategy_profile", None)
    return _LifecycleIdentity(
        domain=getattr(record, "domain", default_domain),
        strategy_profile=profile if isinstance(profile, str) else None,
        platform=getattr(record, "platform", None),
    )


def _is_binance_live_record(
    record: Any,
    *,
    platform: str | None = None,
    require_platform: bool = False,
) -> bool:
    if record is None:
        return False
    identity = _read_lifecycle_identity(record)
    if identity.domain != "crypto" or identity.strategy_profile != _BINANCE_LIVE_PROFILE:
        return False
    if platform is not None:
        return identity.platform == platform
    if require_platform:
        return bool(str(identity.platform or "").strip())
    return True


def _has_complete_live_coverage(snapshot: Any, request: Mapping[str, str]) -> bool:
    coverage = getattr(snapshot, "interval_return_coverage", None)
    if not isinstance(coverage, Mapping):
        return False
    return (
        coverage.get("coverage_status") == "complete_requested_window"
        and coverage.get("requested_window_complete") is True
        and coverage.get("requested_start_at") == request["required_start_at"]
        and coverage.get("requested_end_at") == request["required_end_at"]
        and coverage.get("return_start_at") == request["required_start_at"]
        and coverage.get("return_end_at") == request["required_end_at"]
        and type(coverage.get("return_count")) is int
        and coverage.get("return_count", 0) >= 10
    )


def _same_live_provenance(drift: Any, snapshot: Any) -> bool:
    return (
        getattr(drift, "as_of", None) == getattr(snapshot, "as_of", None)
        and getattr(drift, "source_revision", None) == getattr(snapshot, "source_revision", None)
    )


class _DriftQualification:
    __slots__ = ("reason", "alert_suppressed", "baseline_available")

    def __init__(self, *, reason: Any, alert_suppressed: Any, baseline_available: Any) -> None:
        self.reason = reason
        self.alert_suppressed = alert_suppressed
        self.baseline_available = baseline_available

    @classmethod
    def from_record(cls, drift: Any) -> _DriftQualification:
        return cls(
            reason=getattr(drift, "reason", ""),
            alert_suppressed=getattr(drift, "alert_suppressed", False),
            baseline_available=getattr(drift, "baseline_available", True),
        )

    def is_qualified(self) -> bool:
        return (
            self.alert_suppressed is False
            and self.baseline_available is True
            and isinstance(self.reason, str)
            and not self.reason.strip()
        )


def _collect_drift_results(run_drift_detection, *, domains=DOMAINS):
    results: dict[str, list[Any]] = {}
    errors: list[dict[str, str]] = []
    for domain in domains:
        try:
            results[domain] = list(run_drift_detection(domain))
        except Exception as exc:
            errors.append(
                {
                    "domain": domain,
                    "code": "drift_data_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
    return results, errors


def _qualified_profile_rows(rows, domain, expected_profiles):
    """Keep each current expected identity once; reject unrelated/duplicate rows."""
    grouped: dict[str, list[Any]] = {}
    unexpected = False
    for row in rows:
        identity = _read_lifecycle_identity(row, default_domain=domain)
        profile = identity.strategy_profile
        # Older lifecycle objects may omit domain; their collecting call owns it.
        if profile is None or profile not in expected_profiles or identity.domain != domain:
            unexpected = True
            continue
        grouped.setdefault(profile, []).append(row)
    qualified = []
    for profile, records in grouped.items():
        if len(records) != 1:
            unexpected = True
            continue
        qualified.append(records[0])
    return qualified, {row.strategy_profile for row in qualified}, unexpected


def _binance_live_window(env: Mapping[str, str] | None = None, *, now: datetime | None = None) -> tuple[dict[str, str] | None, str | None]:
    """Resolve an explicit live monitor target and checkpoint window."""
    env = os.environ if env is None else env
    stream_id = str(env.get("BINANCE_LIVE_MONITOR_STREAM_ID") or "").strip()
    start_raw = str(env.get("BINANCE_LIVE_MONITOR_REQUIRED_START_AT") or "").strip()
    end_raw = str(env.get("BINANCE_LIVE_MONITOR_REQUIRED_END_AT") or "").strip()
    if not stream_id or not start_raw or not end_raw:
        return None, "live_coverage_configuration_missing"
    if not _LIVE_STREAM_ID_RE.fullmatch(stream_id):
        return None, "live_coverage_configuration_invalid"
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        if (start.tzinfo is None or end.tzinfo is None
                or start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0)):
            raise ValueError("timestamps must be UTC")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if start >= end:
            raise ValueError("window is reversed")
    except (TypeError, ValueError, OverflowError):
        return None, "live_coverage_configuration_invalid"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return None, "live_coverage_configuration_invalid"
    age = current.astimezone(timezone.utc) - end
    if age > _LIVE_MAX_LAG:
        return None, "live_coverage_stale"
    if age < -_LIVE_FUTURE_TOLERANCE:
        return None, "live_coverage_future"
    return {
        "stream_id": stream_id,
        "required_start_at": start.isoformat(),
        "required_end_at": end.isoformat(),
    }, None


def _run_binance_live_profile_monitor(run_monitor, *, source_revision: str, env=None, now=None):
    """Run only the configured Binance live profile against an exact window."""
    request, error = _binance_live_window(env, now=now)
    if error:
        return [], error
    environment = os.environ if env is None else env
    root = Path(environment.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    status_path = root / "data/lifecycle-artifacts/binance-report-source.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            as_of = datetime.fromisoformat(status["as_of"].replace("Z", "+00:00"))
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError("source status timestamp missing timezone")
            age = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
            if (status.get("schema_version") != "quant_monitor_binance_report_source.v1"
                    or status.get("stream_id") != request["stream_id"]
                    or status.get("status") != "ready"
                    or age > timedelta(hours=2) or age < -timedelta(minutes=5)):
                return [], "live_archive_source_unavailable"
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return [], "live_archive_source_unavailable"
    try:
        snapshots = list(run_monitor(
            "crypto",
            strategy_profile=_BINANCE_LIVE_PROFILE,
            live_stream_id=request["stream_id"],
            required_start_at=request["required_start_at"],
            required_end_at=request["required_end_at"],
            min_observations=10,
            source_revision=source_revision,
        ))
    except Exception:
        return [], "live_coverage_incomplete"
    if len(snapshots) != 1 or not _is_binance_live_record(snapshots[0], platform=request["stream_id"]):
        return [], "live_coverage_incomplete"
    if not _has_complete_live_coverage(snapshots[0], request):
        return [], "live_coverage_incomplete"
    return snapshots, None


def _run_expected_monitor_profiles(run_monitor, domain, profiles, source_revision, *, env=None):
    """Isolate the one live profile; keep unrelated research monitors independent."""
    if domain != "crypto" or _BINANCE_LIVE_PROFILE not in profiles:
        return list(run_monitor(domain, source_revision=source_revision)), None
    snapshots = []
    live_error = None
    for profile in profiles:
        if profile == _BINANCE_LIVE_PROFILE:
            rows, live_error = _run_binance_live_profile_monitor(
                run_monitor, source_revision=source_revision, env=env,
            )
            snapshots.extend(rows)
            continue
        try:
            snapshots.extend(run_monitor(
                domain, strategy_profile=profile, source_revision=source_revision,
            ))
        except Exception:
            continue
    return snapshots, live_error


def _run_binance_live_profile_drift(snapshot, *, store=None):
    """Qualify drift from this exact live snapshot; never reuse prior profile drift."""
    try:
        from quant_platform_kit.strategy_lifecycle.drift_detector import detect_drift
        from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore

        if not _is_binance_live_record(snapshot, require_platform=True):
            return None, "live_drift_snapshot_unavailable"
        store = store or PerformanceStore.from_env()
        baseline = store.load_latest_backtest("crypto", _BINANCE_LIVE_PROFILE)
        if not _is_binance_live_record(baseline):
            return None, "live_drift_baseline_unavailable"
        # Previous drift records have no live-stream identity and cannot carry
        # severity into another stream. The direct detector still honors any
        # qualification failure attached to this exact snapshot.
        drift = detect_drift(snapshot, backtest=baseline)
        if (
            not _is_binance_live_record(drift)
            or not _same_live_provenance(drift, snapshot)
            or not _drift_is_qualified_for_monitoring(drift)
        ):
            return None, "live_drift_unavailable"
        return drift, None
    except Exception:
        return None, "live_drift_unavailable"


def _run_expected_monitor_drifts(run_drift_detection, domain, profiles, live_snapshot=None):
    """Keep research drift calls intact and bind live drift to its verified snapshot."""
    if domain != "crypto" or _BINANCE_LIVE_PROFILE not in profiles:
        return list(run_drift_detection(domain)), None
    results = []
    live_error = None
    for profile in profiles:
        if profile == _BINANCE_LIVE_PROFILE:
            if live_snapshot is None:
                live_error = "live_drift_snapshot_unavailable"
            else:
                live_drift, live_error = _run_binance_live_profile_drift(live_snapshot)
                if live_drift is not None:
                    results.append(live_drift)
            continue
        try:
            results.extend(run_drift_detection(domain, strategy_profile=profile))
        except Exception:
            continue
    return results, live_error


def _refresh_and_collect_drift(
    run_monitor, run_drift_detection, *, domains=DOMAINS, expected_profiles_by_domain=None,
    monitor_reason_by_domain=None, drift_reason_by_domain=None,
):
    snapshots: dict[str, list[Any]] = {}
    results: dict[str, list[Any]] = {}
    errors: list[dict[str, str]] = []
    for domain in domains:
        try:
            domain_snapshots = list(run_monitor(domain))
            if not domain_snapshots:
                raise RuntimeError("monitor produced no snapshots")
            if expected_profiles_by_domain is not None:
                expected = set(expected_profiles_by_domain.get(domain, ()))
                domain_snapshots, observed, unexpected = _qualified_profile_rows(
                    domain_snapshots, domain, expected,
                )
                missing = expected - observed
                if missing or unexpected:
                    errors.append(_artifact_status_error(
                        domain, code="monitor_data_unavailable",
                        reason_code=(monitor_reason_by_domain or {}).get(domain)
                        if missing and (monitor_reason_by_domain or {}).get(domain)
                        else "profile_coverage_incomplete" if missing else "profile_coverage_unexpected",
                    ))
                if not domain_snapshots:
                    continue
            snapshots[domain] = domain_snapshots
        except Exception as exc:
            errors.append(
                {
                    "domain": domain,
                    "code": "monitor_data_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        try:
            domain_drifts = list(run_drift_detection(domain))
            if expected_profiles_by_domain is not None:
                # Prior store evidence for a missing refresh cannot become a new
                # degradation signal. Qualified current profiles remain usable.
                domain_drifts, observed_drifts, unexpected = _qualified_profile_rows(
                    domain_drifts, domain, observed,
                )
                missing = expected - observed_drifts
                if missing or unexpected:
                    errors.append(_artifact_status_error(
                        domain, code="drift_data_unavailable",
                        reason_code=(drift_reason_by_domain or {}).get(domain)
                        if missing and (drift_reason_by_domain or {}).get(domain)
                        else "profile_coverage_incomplete" if missing else "profile_coverage_unexpected",
                    ))
            results[domain] = domain_drifts
        except Exception as exc:
            errors.append(
                {
                    "domain": domain,
                    "code": "drift_data_unavailable",
                    "error_type": type(exc).__name__,
                }
            )
    return snapshots, results, errors


def _artifact_status_error(
    domain: str,
    *,
    code: str = "artifact_sync_status_unavailable",
    error_type: str = "RuntimeError",
    reason_code: str | None = None,
    shared_root_cause: str | None = None,
    http_status: int | None = None,
    rate_limit_reset_at: str | None = None,
) -> dict[str, Any]:
    safe_code = code if _SAFE_TOKEN.fullmatch(code) else "artifact_sync_status_unavailable"
    safe_error_type = error_type if _SAFE_TOKEN.fullmatch(error_type) else "RuntimeError"
    error: dict[str, Any] = {
        "domain": domain,
        "code": safe_code,
        "error_type": safe_error_type,
    }
    if reason_code and _SAFE_TOKEN.fullmatch(reason_code):
        error["reason_code"] = reason_code
    if shared_root_cause and _SAFE_TOKEN.fullmatch(shared_root_cause):
        error["shared_root_cause"] = shared_root_cause
    if type(http_status) is int and 100 <= http_status <= 599:
        error["http_status"] = http_status
    if isinstance(rate_limit_reset_at, str) and _SAFE_RESET_AT.fullmatch(rate_limit_reset_at):
        error["rate_limit_reset_at"] = rate_limit_reset_at
    return error


def _load_expected_coverage(
    root: Path,
    *,
    domains=DOMAINS,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=2),
) -> tuple[
    dict[str, list[str]], dict[str, str], str | None, dict[str, dict[str, Any]], set[str],
]:
    """Read trusted profile coverage and provenance from one validated payload."""
    path = root / _ARTIFACT_STATUS_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        as_of = datetime.fromisoformat(str(payload["as_of"]).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            raise ValueError("status timestamp has no timezone")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age = current - as_of.astimezone(timezone.utc)
        domain_statuses = payload["domains"]
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _ARTIFACT_STATUS_SCHEMA
            or not isinstance(domain_statuses, dict)
        ):
            raise ValueError("artifact status is invalid")
        if age > max_age:
            reason = "artifact_status_stale"
        elif age < -timedelta(minutes=5):
            reason = "artifact_status_future"
        else:
            reason = None
    except (OSError, KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        reason = ("artifact_status_missing" if isinstance(exc, FileNotFoundError)
                  else "artifact_status_read_failed" if isinstance(exc, OSError)
                  else "artifact_status_invalid")
        error_type = type(exc).__name__
    else:
        error_type = "ValueError"
    if reason:
        return {}, {}, None, {
            domain: _artifact_status_error(domain, error_type=error_type,
                                           reason_code=reason, shared_root_cause=reason)
            for domain in domains
        }, set()

    shared_upstream = payload.get("shared_upstream")
    shared_reason = None
    upstream_candidate = _optional_str(shared_upstream, "reason_code")
    if upstream_candidate in _GITHUB_UPSTREAM_CODES:
        shared_reason = upstream_candidate

    expected_profiles: dict[str, list[str]] = {}
    not_configured: set[str] = set()
    source_revisions: dict[str, str] = {}
    errors: dict[str, dict[str, Any]] = {}
    for domain in domains:
        status = domain_statuses.get(domain)
        if not isinstance(status, dict):
            errors[domain] = _artifact_status_error(domain)
            continue
        profiles = status.get("profiles")
        if status.get("status") == "not_configured" and profiles in (None, []):
            not_configured.add(domain)
            continue
        head_sha = str(status.get("head_sha") or "")
        valid_ready = (
            status.get("status") == "ready"
            and _is_positive_int(status.get("artifact_id"))
            and _is_positive_int(status.get("run_id"))
            and re.fullmatch(r"[0-9a-f]{40}", head_sha)
            and isinstance(profiles, list)
            and bool(profiles)
            and all(_is_nonempty_str(profile) for profile in profiles)
        )
        if valid_ready:
            expected_profiles[domain] = list(profiles)
            source_revisions[domain] = head_sha
            continue
        domain_shared = _optional_str(status, "shared_root_cause")
        if domain_shared in _GITHUB_UPSTREAM_CODES:
            shared_for_domain = domain_shared
        elif shared_reason and (
            str(status.get("code") or "") in _GITHUB_UPSTREAM_CODES
            or str(status.get("reason_code") or "") in _GITHUB_UPSTREAM_CODES
        ):
            shared_for_domain = shared_reason
        else:
            shared_for_domain = None
        reason = status.get("reason_code")
        reset_at = _optional_str(status, "rate_limit_reset_at")
        if reset_at is None:
            reset_at = _optional_str(shared_upstream, "rate_limit_reset_at")
        errors[domain] = _artifact_status_error(
            domain,
            code=str(status.get("code") or "artifact_sync_status_unavailable"),
            error_type=str(status.get("error_type") or "RuntimeError"),
            reason_code=str(reason) if reason is not None else None,
            shared_root_cause=shared_for_domain,
            http_status=_optional_int(status, "http_status"),
            rate_limit_reset_at=reset_at,
        )
    return expected_profiles, source_revisions, str(payload["as_of"]), errors, not_configured


def _load_lifecycle_artifact_status(
    root: Path, *, domains=DOMAINS, now: datetime | None = None,
    max_age: timedelta = timedelta(hours=2),
) -> tuple[tuple[str, ...], dict[str, str], list[dict[str, Any]]]:
    """Preserve the existing three-part status API and legacy error behavior."""
    expected, revisions, _as_of, errors, not_configured = _load_expected_coverage(
        root, domains=domains, now=now, max_age=max_age,
    )
    return tuple(expected), revisions, [
        errors[domain] if domain in errors else _artifact_status_error(domain)
        for domain in domains if domain in errors or domain in not_configured
    ]


def _format_data_error_alerts(
    data_errors: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Build Telegram lines/identities; compress shared GitHub upstream roots."""

    notify_lines: list[str] = []
    alert_identities: list[str] = []
    shared_groups: dict[str, list[dict[str, Any]]] = {}
    status_groups: dict[str, list[dict[str, Any]]] = {}
    remainder: list[dict[str, Any]] = []

    for error in data_errors:
        shared = str(error.get("shared_root_cause") or "")
        code = str(error.get("code") or "")
        reason = str(error.get("reason_code") or code)
        if shared in _STATUS_FILE_REASONS and code == "artifact_sync_status_unavailable" and reason == shared:
            status_groups.setdefault(shared, []).append(error)
        elif shared in _GITHUB_UPSTREAM_CODES or (
            shared
            and _SAFE_TOKEN.fullmatch(shared)
            and (code in _GITHUB_UPSTREAM_CODES or reason in _GITHUB_UPSTREAM_CODES)
        ):
            shared_groups.setdefault(shared or reason, []).append(error)
        else:
            remainder.append(error)

    for reason, group in sorted(status_groups.items()):
        domains = ",".join(sorted(str(item.get("domain") or "") for item in group))
        notify_lines.append(f"[artifact_status] artifact_sync_status_unavailable reason={reason} domains={domains}")
        # Preserve the previous per-domain identity despite the grouped display.
        alert_identities.extend(
            f"data_error:{item.get('domain') or ''}:artifact_sync_status_unavailable:{item.get('error_type') or ''}"
            for item in group
        )

    for shared_key, group in sorted(shared_groups.items()):
        domains = ",".join(sorted(str(item.get("domain") or "") for item in group))
        sample = group[0]
        reason = str(sample.get("reason_code") or sample.get("code") or shared_key)
        if reason not in _OPERATIONAL_ERROR_CODES:
            reason = shared_key if shared_key in _OPERATIONAL_ERROR_CODES else "github_api_unavailable"
        line = (
            f"[github_api_upstream] {reason} domains={domains} "
            "(not a strategy/trading signal)"
        )
        reset_at = sample.get("rate_limit_reset_at")
        if isinstance(reset_at, str) and _SAFE_RESET_AT.fullmatch(reset_at):
            line += f" rate_limit_reset_at={reset_at}"
        notify_lines.append(line)
        alert_identities.append(f"data_error:github_api_upstream:{reason}:{domains}")

    for error in remainder:
        domain = str(error.get("domain") or "")
        code = str(error.get("code") or "")
        error_type = str(error.get("error_type") or "")
        reason = error.get("reason_code")
        if (
            isinstance(reason, str)
            and _SAFE_TOKEN.fullmatch(reason)
            and reason != code
        ):
            notify_lines.append(
                f"[{domain}] {code} reason={reason} ({error_type})"
            )
            alert_identities.append(
                f"data_error:{domain}:{code}:{reason}:{error_type}"
            )
        else:
            notify_lines.append(f"[{domain}] {code} ({error_type})")
            alert_identities.append(f"data_error:{domain}:{code}:{error_type}")
    return notify_lines, alert_identities


def _alert_fingerprint(lines: list[str]) -> str:
    payload = "\n".join(sorted(str(line) for line in lines))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_alert_body(lines: list[str], *, locale: str | None = None) -> str:
    from quant_platform_kit.common.notification_localization import resolve_notification_locale

    zh = resolve_notification_locale(os.environ.get("QSL_NOTIFY_LANG") if locale is None else locale) == "zh"
    explanations = {
        "artifact_status_missing": ("同步状态文件缺失", "Synchronization status file is missing"),
        "artifact_status_read_failed": ("同步状态文件读取失败", "Synchronization status file could not be read"),
        "artifact_status_invalid": ("同步状态文件格式或内容无效", "Synchronization status file is invalid"),
        "artifact_status_stale": ("同步状态文件已过期", "Synchronization status file is stale"),
        "artifact_status_future": ("同步状态文件时间超前", "Synchronization status file is future-dated"),
        "live_coverage_configuration_missing": ("实时覆盖配置缺失，当前监测证据不可用", "Live coverage configuration is missing; monitoring evidence is unavailable"),
        "live_drift_snapshot_unavailable": ("当前实时快照不可用，无法计算漂移", "Current live snapshot is unavailable; drift cannot be calculated"),
        "live_coverage_configuration_invalid": ("实时覆盖配置无效", "Live coverage configuration is invalid"),
        "live_coverage_stale": ("实时覆盖窗口已过期", "Live coverage window is stale"),
        "live_coverage_future": ("实时覆盖窗口时间超前", "Live coverage window is future-dated"),
        "live_archive_source_unavailable": ("实时归档来源不可用", "Live archive source is unavailable"),
        "live_coverage_incomplete": ("实时覆盖不完整", "Live coverage is incomplete"),
        "live_drift_baseline_unavailable": ("实时漂移基准不可用", "Live drift baseline is unavailable"),
        "live_drift_unavailable": ("实时漂移证据不可用", "Live drift evidence is unavailable"),
        "live_health_score_unavailable": ("实时健康评分不可用", "Live health score is unavailable"),
        "profile_coverage_incomplete": ("预期策略证据不完整", "Expected strategy evidence is incomplete"),
        "profile_coverage_unexpected": ("策略证据身份不符合预期", "Strategy evidence contains unexpected identities"),
    }
    rendered = []
    monitors = [line for line in lines if line.startswith("[crypto] monitor_data_unavailable ")
                and "reason=live_coverage_configuration_missing " in line]
    drifts = [line for line in lines if line.startswith("[crypto] drift_data_unavailable ")
              and "reason=live_drift_snapshot_unavailable " in line]
    live_monitor, live_drift = (monitors[0], drifts[0]) if len(monitors) == len(drifts) == 1 else (None, None)
    for line in lines:
        if live_monitor and live_drift and line == live_drift:
            continue
        if live_monitor and live_drift and line == live_monitor:
            detail = ("实时覆盖配置缺失；当前实时快照不可用，无法计算漂移" if zh else
                      "Live coverage configuration is missing; the current live snapshot is unavailable for drift")
            rendered.append(f"{detail}{'：' if zh else ': '}{live_monitor}{'；' if zh else '; '}{live_drift}")
            continue
        match = re.search(r"\breason=([A-Za-z][A-Za-z0-9_]*)", line)
        translations = explanations.get(match.group(1)) if match else None
        if translations:
            explanation = translations[0 if zh else 1]
        else:
            if "issue_record_failed" in line:
                explanation = "优化记录投递失败" if zh else "Optimization record delivery failed"
            elif "github_api_" in line:
                explanation = "GitHub 上游访问异常，相关证据不可用" if zh else "GitHub upstream access failed; evidence is unavailable"
            else:
                explanation = "监测数据或证据不可用，请根据代码排查" if zh else "Monitoring data or evidence is unavailable; investigate the code"
        rendered.append(f"{explanation}{'：' if zh else ': '}{line}")
    heading = ("🚨 量化监控｜运行告警\n"
               "• 范围：数据、证据或优化记录投递异常\n"
               if zh else "🚨 quant-monitor operational\n• scope: data/evidence or optimization-record delivery failure\n")
    return (heading
            + "\n".join(f"• {line}" for line in rendered))


def _alert_state_path(root: Path) -> Path:
    return root / _ALERT_STATE_RELATIVE_PATH


def _load_alert_state(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_alert_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_alert_state(root: Path, payload: dict[str, Any]) -> None:
    path = _alert_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".health_cycle.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    callback = getattr(_CLOUD_CHECKPOINT, 'callback', None)
    if callback is not None:
        callback()


@contextmanager
def cloud_alert_checkpoint(callback):
    """Persist each delivery intent/receipt before the next external operation."""
    previous = getattr(_CLOUD_CHECKPOINT, 'callback', None)
    _CLOUD_CHECKPOINT.callback = callback
    try:
        yield
    finally:
        _CLOUD_CHECKPOINT.callback = previous


def _is_duplicate_alert(root: Path, fingerprint: str) -> bool:
    return str(_load_alert_state(root).get("fingerprint") or "") == fingerprint


def _delivery_target_hash(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()


def _empty_delivery_result() -> dict[str, Any]:
    return {
        "all_sent": False,
        "suppressed": False,
        "errors": [],
        "counts": {"pending": 0, "sent": 0, "failed": 0, "unknown": 0},
    }


def _delivery_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"status", "attempts"}:
        return None
    status = value.get("status")
    attempts = value.get("attempts")
    if status not in _DELIVERY_STATUSES or type(attempts) is not int or attempts < 0:
        return None
    return {"status": status, "attempts": attempts}


def _load_delivery_payload(root: Path) -> dict[str, Any]:
    path = _alert_state_path(root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OSError("alert_state_unreadable") from exc
    payload = _require_dict(payload, "alert_state_malformed")
    deliveries = payload.get("deliveries", {})
    if deliveries is None:
        deliveries = {}
    deliveries = _require_dict(deliveries, "alert_state_malformed")
    normalized: dict[str, dict[str, dict[str, Any]]] = {}
    for event_id, targets in deliveries.items():
        if not _is_hex64(event_id):
            raise OSError("alert_state_malformed")
        targets = _require_dict(targets, "alert_state_malformed")
        event_targets: dict[str, dict[str, Any]] = {}
        for target_hash, record in targets.items():
            parsed = _delivery_record(record)
            if not _is_hex64(target_hash) or parsed is None:
                raise OSError("alert_state_malformed")
            event_targets[target_hash] = parsed
        normalized[event_id] = event_targets
    payload["deliveries"] = normalized
    return payload


@contextmanager
def _alert_state_exclusive(root: Path):
    """Serialize every read-modify-write of health_cycle.json.

    Same-thread reentry skips a second flock so a nested diagnosis update
    cannot deadlock on the lock held by delivery. The nested writer must
    reload before its own write; callers that already hold the payload reload
    again after the nested call.
    """
    if not isinstance(root, Path) or not root.is_dir():
        raise OSError("alert_state_root_unavailable")
    depth = int(getattr(_ALERT_STATE_LOCK_DEPTH, "depth", 0) or 0)
    if depth:
        _ALERT_STATE_LOCK_DEPTH.depth = depth + 1
        try:
            yield
        finally:
            _ALERT_STATE_LOCK_DEPTH.depth = depth
        return
    state_path = _alert_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    with _ALERT_STATE_THREAD_LOCK, lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ALERT_STATE_LOCK_DEPTH.depth = 1
        try:
            yield
        finally:
            _ALERT_STATE_LOCK_DEPTH.depth = 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _health_event_ids(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("health_sent_events")
    if not isinstance(raw, list):
        return []
    event_ids: list[str] = []
    for item in raw:
        if _is_hex64(item) and item not in event_ids:
            event_ids.append(item)
    return event_ids


def _remember_health_event(payload: dict[str, Any], event_id: str) -> None:
    event_ids = _health_event_ids(payload)
    if event_id not in event_ids:
        event_ids.append(event_id)
    payload["health_sent_events"] = event_ids


def _persist_delivery_payload(root: Path, payload: dict[str, Any]) -> None:
    payload["schema_version"] = "quant_monitor_alert_state.v1"
    _write_alert_state(root, payload)


def _target_plan(record: dict[str, Any] | None) -> str:
    if record is None:
        return "claim"
    status = record["status"]
    if status == "sent":
        return "skip"
    if status in {"pending", "unknown"}:
        return "hold"
    if status == "failed" and record["attempts"] < _DELIVERY_ATTEMPT_LIMIT:
        return "claim"
    return "hold"


def _count_delivery(counts: dict[str, int], status: str) -> None:
    if status in counts:
        counts[status] += 1


def _deliver_telegram_targets_locked(
    root: Path,
    event_id: str,
    chat_ids: list[str],
    send_one: Callable[[str], str],
    *,
    confirm_legacy: bool,
) -> dict[str, Any]:
    result = _empty_delivery_result()
    payload = _load_delivery_payload(root)
    deliveries = payload.get("deliveries") or {}
    event_targets = deliveries.get(event_id) or {}
    if payload.get("fingerprint") == event_id and not event_targets:
        result["suppressed"] = True
        return result
    changed = False
    for target_hash, record in list(event_targets.items()):
        if record["status"] == "pending":
            event_targets[target_hash] = {"status": "unknown", "attempts": record["attempts"]}
            changed = True
    if changed:
        deliveries[event_id] = event_targets
        payload["deliveries"] = deliveries
        _persist_delivery_payload(root, payload)
    claimed = False
    for chat_id in chat_ids:
        target_hash = _delivery_target_hash(chat_id)
        record = event_targets.get(target_hash)
        plan = _target_plan(record)
        if plan == "skip":
            _count_delivery(result["counts"], "sent")
            continue
        if plan == "hold":
            status = "unknown" if record is None else record["status"]
            if status == "pending":
                status = "unknown"
            _count_delivery(result["counts"], status)
            if status == "unknown":
                if "telegram_delivery_unknown" not in result["errors"]:
                    result["errors"].append("telegram_delivery_unknown")
            elif "telegram_delivery_failed" not in result["errors"]:
                result["errors"].append("telegram_delivery_failed")
            continue
        attempts = 1 if record is None else record["attempts"] + 1
        event_targets[target_hash] = {"status": "pending", "attempts": attempts}
        deliveries[event_id] = event_targets
        payload["deliveries"] = deliveries
        if confirm_legacy:
            _remember_health_event(payload, event_id)
        _persist_delivery_payload(root, payload)
        claimed = True
        try:
            outcome = send_one(chat_id)
        except Exception:
            outcome = "unknown"
        if outcome not in {"sent", "failed", "unknown"}:
            outcome = "unknown"
        try:
            payload = _load_delivery_payload(root)
        except OSError:
            result["errors"].append("alert_state_write_failed")
            _count_delivery(result["counts"], "unknown")
            break
        deliveries = payload.get("deliveries") or {}
        event_targets = deliveries.get(event_id) or {}
        event_targets[target_hash] = {"status": outcome, "attempts": attempts}
        deliveries[event_id] = event_targets
        payload["deliveries"] = deliveries
        try:
            _persist_delivery_payload(root, payload)
        except OSError:
            result["errors"].append("alert_state_write_failed")
            _count_delivery(result["counts"], "unknown")
            break
        _count_delivery(result["counts"], outcome)
        if outcome == "unknown" and "telegram_delivery_unknown" not in result["errors"]:
            result["errors"].append("telegram_delivery_unknown")
        elif outcome == "failed" and "telegram_delivery_failed" not in result["errors"]:
            result["errors"].append("telegram_delivery_failed")
    requested = [_delivery_target_hash(chat_id) for chat_id in chat_ids]
    all_sent = not result["errors"] and all(
        event_targets.get(target_hash, {}).get("status") == "sent" for target_hash in requested
    )
    if confirm_legacy and event_id in (payload.get("deliveries") or {}):
        owned = event_id in _health_event_ids(payload)
        current = payload.get("fingerprint")
        _remember_health_event(payload, event_id)
        fingerprint_changed = all_sent and current in (None, "", event_id) and current != event_id
        if fingerprint_changed:
            payload["fingerprint"] = event_id
        if claimed or not owned or fingerprint_changed:
            _persist_delivery_payload(root, payload)
    if result["errors"]:
        return result
    if all_sent:
        if claimed:
            result["all_sent"] = True
        else:
            result["suppressed"] = True
    return result


def deliver_telegram_targets(
    root: Path,
    event_id: str,
    chat_ids: tuple[str, ...] | list[str],
    send_one: Callable[[str], str],
    *,
    confirm_legacy: bool = False,
) -> dict[str, Any]:
    """Persist per-target delivery, then send only targets that are still eligible.

    Missing or unreadable state refuses the send. Pending left by a crash is
    unknown and is not sent again. A legacy fingerprint with no target records
    suppresses that event without inventing sent targets.
    """
    result = _empty_delivery_result()
    if not isinstance(root, Path) or not root.is_dir():
        result["errors"].append("alert_state_root_unavailable")
        return result
    if not _is_hex64(event_id):
        result["errors"].append("alert_state_malformed")
        return result
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in chat_ids:
        chat_id = str(raw or "").strip()
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        ordered.append(chat_id)
    if not ordered:
        result["errors"].append("telegram_missing_env")
        return result
    try:
        with _alert_state_exclusive(root):
            return _deliver_telegram_targets_locked(
                root, event_id, ordered, send_one, confirm_legacy=confirm_legacy,
            )
    except OSError as exc:
        code = str(exc)
        if code not in {
            "alert_state_unreadable",
            "alert_state_malformed",
            "alert_state_root_unavailable",
            "alert_state_write_failed",
        }:
            code = "alert_state_write_failed"
        result["errors"].append(code)
        return result


def _record_alert(root: Path, fingerprint: str) -> None:
    with _alert_state_exclusive(root):
        try:
            payload = _load_operational_diagnosis_state(root)
        except OSError:
            return
        payload.update(
            schema_version="quant_monitor_alert_state.v1",
            fingerprint=fingerprint,
        )
        _write_alert_state(root, payload)


def _health_telegram_chat_ids() -> tuple[str, ...]:
    raw = os.environ.get("GLOBAL_TELEGRAM_CHAT_ID") or ""
    ordered: list[str] = []
    seen: set[str] = set()
    for part in str(raw).replace(";", ",").replace("\n", ",").split(","):
        chat_id = part.strip()
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        ordered.append(chat_id)
    return tuple(ordered)


def _drop_sent_targets(deliveries: dict[str, Any], event_id: str) -> None:
    targets = deliveries.get(event_id)
    if not isinstance(targets, dict):
        return
    kept = {
        target_hash: record
        for target_hash, record in targets.items()
        if not (isinstance(record, dict) and record.get("status") == "sent")
    }
    if kept:
        deliveries[event_id] = kept
    else:
        deliveries.pop(event_id, None)


def _clear_alert(root: Path) -> None:
    try:
        with _alert_state_exclusive(root):
            _clear_alert_locked(root)
    except OSError:
        return


def _clear_alert_locked(root: Path) -> None:
    try:
        payload = _load_operational_diagnosis_state(root)
    except OSError:
        return
    event_ids = _health_event_ids(payload)
    confirmed = payload.pop("fingerprint", None)
    if isinstance(confirmed, str) and confirmed not in event_ids:
        event_ids.append(confirmed)
    payload.pop("health_sent_events", None)
    deliveries = payload.get("deliveries")
    if isinstance(deliveries, dict):
        for event_id in event_ids:
            _drop_sent_targets(deliveries, event_id)
        if not deliveries:
            payload.pop("deliveries", None)
        else:
            payload["deliveries"] = deliveries
    keep = bool(payload.get("operational_diagnosis_attempts")) or bool(payload.get("deliveries"))
    if payload.get("operational_diagnosis_last_attempt_date"):
        keep = True
    if keep:
        payload["schema_version"] = "quant_monitor_alert_state.v1"
        _write_alert_state(root, payload)
        return
    try:
        _alert_state_path(root).unlink()
    except FileNotFoundError:
        pass


def _operational_diagnosis_attempted(root: Path, fingerprint: str) -> bool:
    attempts = _load_operational_diagnosis_state(root).get("operational_diagnosis_attempts")
    return isinstance(attempts, list) and fingerprint in attempts


def _load_operational_diagnosis_state(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_alert_state_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise OSError("operational diagnosis state is unavailable") from exc
    payload = _require_dict(payload, "operational diagnosis state is unavailable")
    attempts = payload.get("operational_diagnosis_attempts", [])
    if not isinstance(attempts, list) or any(not _is_hex64(item) for item in attempts):
        raise OSError("operational diagnosis state is unavailable")
    attempt_date = payload.get("operational_diagnosis_last_attempt_date")
    if attempt_date is not None:
        try:
            if not isinstance(attempt_date, str) or datetime.strptime(attempt_date, "%Y-%m-%d").strftime("%Y-%m-%d") != attempt_date:
                raise ValueError("invalid date")
        except ValueError as exc:
            raise OSError("operational diagnosis state is unavailable") from exc
    return payload


def _prepare_operational_diagnosis_root(root: Path) -> None:
    """Create the diagnosis consumer directory before the shared state lock.

    Delivery refuses a missing monitor root before taking that lock. Diagnosis
    still has to create ``data/diagnosis-consumer`` on the first attempt.
    """
    if not isinstance(root, Path):
        raise OSError("operational diagnosis state is unavailable")
    root.mkdir(parents=True, exist_ok=True)


def _record_operational_diagnosis_attempt(root: Path, fingerprint: str, *, attempt_date: str | None = None) -> None:
    _prepare_operational_diagnosis_root(root)
    with _alert_state_exclusive(root):
        payload = _load_operational_diagnosis_state(root)
        attempts = payload.get("operational_diagnosis_attempts")
        if not isinstance(attempts, list):
            attempts = []
        if fingerprint not in attempts:
            attempts.append(fingerprint)
        payload.update(
            schema_version="quant_monitor_alert_state.v1",
            operational_diagnosis_attempts=attempts,
        )
        if attempt_date is not None:
            payload["operational_diagnosis_last_attempt_date"] = attempt_date
        _write_alert_state(root, payload)


def _forget_operational_diagnosis_attempt(root: Path, fingerprint: str) -> None:
    with _alert_state_exclusive(root):
        payload = _load_operational_diagnosis_state(root)
        attempts = payload.get("operational_diagnosis_attempts")
        if not isinstance(attempts, list):
            return
        payload["operational_diagnosis_attempts"] = [item for item in attempts if item != fingerprint]
        _write_alert_state(root, payload)


def _operational_diagnosis_prompt(data_errors: list[dict[str, str]], *, observation: dict[str, str] | None = None) -> str:
    incidents: list[dict[str, str]] = []
    for error in data_errors:
        domain = str(error.get("domain") or "")
        if domain not in DOMAINS:
            continue
        code = str(error.get("code") or "")
        error_type = str(error.get("error_type") or "")
        incidents.append(
            {
                "code": code if code in _OPERATIONAL_ERROR_CODES else "artifact_sync_status_unavailable",
                "domain": domain,
                "error_type": error_type if error_type in _OPERATIONAL_ERROR_TYPES else "RuntimeError",
            }
        )
    incidents.sort(key=lambda item: (item["domain"], item["code"], item["error_type"]))
    context: dict[str, Any] = {"incidents": incidents}
    if observation is not None:
        context["observation"] = {key: observation[key] for key in (
            "observed_at", "latest_cycle_at", "current_state",
        )}
    payload = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return (
        "Diagnose these sanitized quant-monitor data availability incidents. "
        "Use repository evidence read-only. Return likely cause, evidence to inspect, and safe next checks. "
        "When observation metadata is present, state both observation times and distinguish still_observed, "
        "partially_observed, and not_observed_in_latest. Absence from the latest cycle does not prove "
        "account or trading recovery. Historical diagnosis never authorizes a repair or replay. "
        "Do not place orders, access accounts, change files, create patches, publish, deploy, or notify.\n"
        + payload
    )


def _operational_diagnosis_fingerprint(data_errors: list[dict[str, str]]) -> str:
    identities = [
        f"data_error:{error.get('domain', '')}:{error.get('code', '')}:{error.get('error_type', '')}"
        for error in data_errors
    ]
    return _alert_fingerprint(identities)


def _rehearsal_subscription(client, subscription_id, operation):
    """Use the worker's existing exact-project subscription tools; never subscribe."""
    from ai_service.scenarios import ContractError

    reply = client.request("POST", "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": operation, "arguments": {
            "project": _OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY,
            "subscription_id": subscription_id,
        }},
    })
    if "error" in reply or reply.get("result", {}).get("isError"):
        raise ContractError("subscription operation rejected")
    record = reply["result"]["structuredContent"]
    if record.get("id") != subscription_id:
        raise ContractError("subscription identity mismatch")
    return record


def _rehearsal_creation(value):
    """Project only non-sensitive evidence fields; never serialize raw context."""
    from ai_service.scenarios import ContractError

    request, response = value["request"], value["response"]
    if set(request["arguments"]) != {"project"}:
        raise ContractError("subscription arguments must select exactly one project")
    safe = {
        **{key: value[key] for key in ("kind", "worker", "provider", "created_at", "expires_at")},
        "request": {"name": request["name"], "arguments": {"project": request["arguments"]["project"]},
                    "ttlMs": request.get("ttlMs"),
                    "delivery": {key: request["delivery"][key] for key in ("mode", "url")}},
        "response": {key: response[key] for key in ("id", "refreshBefore")},
    }
    leaves = [safe[key] for key in ("kind", "worker", "provider", "created_at", "expires_at")]
    leaves += [safe["request"]["name"], safe["request"]["arguments"]["project"]]
    leaves += list(safe["request"]["delivery"].values()) + list(safe["response"].values())
    if any(not isinstance(value, str) for value in leaves) or type(safe["request"]["ttlMs"]) is not int:
        raise ContractError("creation evidence fields must be scalar protocol values")
    return safe


class _RehearsalPaths:
    __slots__ = ("intent", "binding", "submitted", "claimed", "cancel", "verified")

    def __init__(
        self,
        *,
        intent: Path,
        binding: Path,
        submitted: Path,
        claimed: Path,
        cancel: Path,
        verified: Path,
    ) -> None:
        self.intent = intent
        self.binding = binding
        self.submitted = submitted
        self.claimed = claimed
        self.cancel = cancel
        self.verified = verified

    @classmethod
    def from_receipt(cls, path: Path) -> _RehearsalPaths:
        raw = str(path)
        return cls(
            intent=path,
            binding=Path(raw + ".binding"),
            submitted=Path(raw + ".submitted"),
            claimed=Path(raw + ".claimed"),
            cancel=Path(raw + ".cancel"),
            verified=Path(raw + ".verified"),
        )


class _RehearsalState:
    __slots__ = ("subscription_id", "result", "keep_subscription", "expires", "creation", "verified")

    def __init__(
        self,
        *,
        subscription_id: str,
        result: dict[str, Any],
        keep_subscription: bool = False,
        expires: datetime | None = None,
        creation: Mapping[str, Any] | None = None,
        verified: dict[str, Any] | None = None,
    ) -> None:
        self.subscription_id = subscription_id
        self.result = result
        self.keep_subscription = keep_subscription
        self.expires = expires
        self.creation = creation
        self.verified = verified


def _read_bound_receipt(receipt: Path, binding: Mapping[str, Any], contract_error: type[Exception]) -> dict[str, Any]:
    if receipt.is_symlink():
        raise contract_error("receipt must not be a symbolic link")
    value = json.loads(receipt.read_text())
    if any(value.get(key) != expected for key, expected in binding.items()):
        raise contract_error("receipt binds a different operation")
    return value


def _check_rehearsal_subscription(state: Mapping[str, Any], creation: Mapping[str, Any], contract_error: type[Exception]) -> None:
    if (state.get("created_at") != creation["created_at"]
            or state.get("expires_at") != creation["expires_at"]):
        raise contract_error("subscription readback is not this creation")


def _check_rehearsal_record(
    record: Mapping[str, Any],
    task_id: str,
    task: Any,
    binding: Mapping[str, Any],
    claimed_path: Path,
    *,
    expected_worker: str,
    expected_attempt: str,
    write_private: Callable[..., Any],
    digest: Callable[[Any], str],
    contract_error: type[Exception],
) -> Mapping[str, Any]:
    if (record.get("id") != task_id or record.get("owner") != task.spec["project"]
            or record.get("provider") != "dot" or type(record.get("revision")) is not int
            or record["revision"] != 1
            or not {"worker", "attempt"} <= record.keys()
            or digest(record.get("request")) != digest(task.request)):
        raise contract_error("task readback mismatch")
    seen = _read_bound_receipt(claimed_path, binding, contract_error) if claimed_path.exists() else None
    identity = {
        "task_id": task_id, "worker": record.get("worker"),
        "attempt": record.get("attempt"), "revision": record["revision"],
    }
    if seen is not None and any(seen.get(key) != value for key, value in identity.items()):
        raise contract_error("observed claim identity regressed")
    if (record.get("attempt"), record.get("worker")) == (None, None):
        if record["status"] not in {
            "queued", "submitting", "running", "outcome_unknown",
            "cancel_requested", "cancelled", "failed",
        }:
            raise contract_error("terminal output requires an actual claim")
    elif (record.get("attempt"), record.get("worker")) != (expected_attempt, expected_worker):
        raise contract_error("incomplete or wrong claim identity")
    elif seen is None:
        # Persist observed identity before accepting any later cancellation.
        write_private(claimed_path, {"status": "observed", **binding, **identity})
    return record


def _prepare_rehearsal_context(
    *,
    instance: Any,
    occurrence: Any,
    submitted_at: Any,
    receipt_path: Any,
    subscription_id: str,
    expected_worker: str,
    expected_attempt: str,
    subscription_creation: Any,
    load_instance: Callable[[Any], dict[str, Any]],
    prepare: Callable[..., Any],
    digest: Callable[[Any], str],
    contract_error: type[Exception],
) -> tuple[Any, dict[str, Any], dict[str, Any], datetime, datetime, _RehearsalPaths]:
    loaded = load_instance(instance)
    if (loaded["project"] != _OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY
            or loaded["template"] != "protocol_probe"
            or loaded["inputs"]["data_classification"] != "synthetic"
            or loaded["inputs"]["fields"].get("ttl_seconds") != 600
            or len(loaded["stages"]) != 1
            or loaded["stages"][0]["limits"]["timeout_seconds"] != 600):
        raise contract_error("Q04 requires its fixed project, synthetic probe and 600s limits")
    task = prepare(
        loaded,
        {"schema_version": "synthetic.protocol-input.v1", "series": [100, 102, 99, 105], "no_order": True},
        occurrence=occurrence,
        submitted_at=submitted_at,
    )
    # This is evidence from the in-process subscription creation boundary,
    # not authentication of a native subscription or proof of live acceptance.
    creation = _rehearsal_creation(subscription_creation)
    request, response = creation["request"], creation["response"]
    if (creation["kind"] != "offline.fixture" or creation["worker"] != expected_worker
            or creation["provider"] != "dot" or request["name"] != "task.requested"
            or request["arguments"] != {"project": task.spec["project"]}
            or type(request.get("ttlMs")) is not int or request["ttlMs"] != 600000
            or request["delivery"]["mode"] != "webhook"
            or response["id"] != subscription_id
            or subscription_id != "sub_" + digest([
                expected_worker, request["delivery"]["url"], "task.requested", request["arguments"]])
            or response["refreshBefore"] != creation["expires_at"]):
        raise contract_error("Q04 subscription creation evidence mismatch")
    expires = datetime.fromisoformat(creation["expires_at"].replace("Z", "+00:00"))
    created = datetime.fromisoformat(creation["created_at"].replace("Z", "+00:00"))
    if not 0 < (expires - created).total_seconds() <= 600:
        raise contract_error("Q04 subscription creation lifetime mismatch")
    binding = {
        "operation_key": task.spec["operation_key"],
        "request_sha256": digest(task.request),
        "project": task.spec["project"],
        "provider": "dot",
        "subscription_id": subscription_id,
        "expected_worker": expected_worker,
        "expected_attempt": expected_attempt,
        "subscription_creation": creation,
        "subscription_creation_sha256": digest(creation),
    }
    deadline = datetime.fromisoformat(task.spec["deadline"].replace("Z", "+00:00"))
    return task, creation, binding, deadline, expires, _RehearsalPaths.from_receipt(Path(receipt_path))


def _resolve_rehearsal_binding(
    paths: _RehearsalPaths,
    binding: Mapping[str, Any],
    state: _RehearsalState,
    *,
    write_private: Callable[..., Any],
    digest: Callable[[Any], str],
    contract_error: type[Exception],
) -> bool:
    # The immutable companion is complete/fsynced before any intent or submit.
    # Its existence never authorizes another submit, even if intent is lost.
    state.result = {"status": "deferred", "reason": "submit_outcome_unknown", "binding_unknown": True}
    state.keep_subscription = True
    fresh_binding = False
    try:
        if not paths.binding.exists() and (paths.intent.exists() or paths.submitted.exists()):
            raise contract_error("existing receipt has no immutable original binding")
        write_private(paths.binding, {"binding": binding, "binding_sha256": digest(binding)})
        fresh_binding = True
    except FileExistsError:
        pass
    if paths.binding.is_symlink():
        raise contract_error("binding receipt must not be a symbolic link")
    envelope = json.loads(paths.binding.read_text())
    original = {key: envelope["binding"][key] for key in binding}
    original["subscription_creation"] = _rehearsal_creation(original["subscription_creation"])
    if (any(not isinstance(value, str) for key, value in original.items() if key != "subscription_creation")
            or digest(original) != envelope["binding_sha256"]
            or original["project"] != _OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY
            or original["provider"] != "dot"):
        raise contract_error("immutable binding receipt is incomplete or inconsistent")
    state.result = {"status": "deferred", "reason": "submit_outcome_unknown", **original}
    # Cleanup must keep using the persisted original scope after mismatched input.
    state.subscription_id = original["subscription_id"]
    state.creation = original["subscription_creation"]
    state.expires = datetime.fromisoformat(state.creation["expires_at"].replace("Z", "+00:00"))
    if original != binding:
        raise contract_error("current inputs do not match the immutable original binding")
    fresh = False
    if fresh_binding:
        try:
            write_private(paths.intent, {"status": "intent", **binding})
            fresh = True
        except FileExistsError:
            _read_bound_receipt(paths.intent, binding, contract_error)
    else:
        _read_bound_receipt(paths.intent, binding, contract_error)
    if fresh:
        state.keep_subscription = False  # Complete fresh intent; no submit attempted.
    return fresh


def _submit_rehearsal_task(
    client: Any,
    task: Any,
    binding: Mapping[str, Any],
    paths: _RehearsalPaths,
    state: _RehearsalState,
    sub_state: Mapping[str, Any],
    *,
    deadline: datetime,
    cancel: bool,
    end: bool,
    expected_worker: str,
    expected_attempt: str,
    write_private: Callable[..., Any],
    digest: Callable[[Any], str],
    submit: Callable[..., Any],
    contract_error: type[Exception],
) -> None:
    if cancel or end or datetime.now(timezone.utc) >= deadline:
        raise contract_error("Q04 submission cancelled or expired")
    if sub_state["status"] != "active" or state.expires is None or datetime.now(timezone.utc) >= state.expires:
        raise contract_error("Q04 requires its still-active subscription")
    state.result = {"status": "deferred", "reason": "submit_outcome_unknown", **binding}
    state.keep_subscription = True
    record = submit(client, task)
    task_id = record.get("id")
    if not _is_nonempty_str(task_id):
        raise contract_error("task identity required")
    _check_rehearsal_record(
        record, task_id, task, binding, paths.claimed,
        expected_worker=expected_worker, expected_attempt=expected_attempt,
        write_private=write_private, digest=digest, contract_error=contract_error,
    )
    write_private(paths.submitted, {"status": record["status"], "task_id": task_id, **binding})


def _reconcile_submitted_rehearsal(
    client: Any,
    task: Any,
    binding: Mapping[str, Any],
    paths: _RehearsalPaths,
    state: _RehearsalState,
    *,
    deadline: datetime,
    cancel: bool,
    end: bool,
    expected_worker: str,
    expected_attempt: str,
    write_private: Callable[..., Any],
    digest: Callable[[Any], str],
    verify: Callable[..., dict[str, Any]],
    contract_error: type[Exception],
) -> None:
    # Do not replace submit uncertainty until this whole sidecar is usable.
    task_id = _read_bound_receipt(paths.submitted, binding, contract_error)["task_id"]
    if not _is_nonempty_str(task_id):
        raise contract_error("task identity required")
    if state.expires is None:
        raise contract_error("subscription expiry required")
    cancelling = (cancel or end or paths.cancel.exists()
                  or datetime.now(timezone.utc) >= min(deadline, state.expires))
    state.result = {
        "status": "deferred" if cancelling else "unavailable",
        "reason": "cancellation_unconfirmed" if cancelling else "synthetic_readback_rejected",
        "task_id": task_id,
        **binding,
    }
    record = _check_rehearsal_record(
        client.get(task_id), task_id, task, binding, paths.claimed,
        expected_worker=expected_worker, expected_attempt=expected_attempt,
        write_private=write_private, digest=digest, contract_error=contract_error,
    )
    state.result.update({key: record[key] for key in ("worker", "attempt", "revision")})
    terminal = {"completed", "failed", "invalid_output", "cancelled"}
    state.keep_subscription = record["status"] not in terminal
    if cancelling:
        if state.keep_subscription:
            try:
                write_private(paths.cancel, {"status": "intent", "task_id": task_id, **binding})
            except FileExistsError:
                if _read_bound_receipt(paths.cancel, binding, contract_error)["task_id"] != task_id:
                    raise contract_error("cancel identity mismatch")
            else:
                _check_rehearsal_record(
                    client.cancel(task_id), task_id, task, binding, paths.claimed,
                    expected_worker=expected_worker, expected_attempt=expected_attempt,
                    write_private=write_private, digest=digest, contract_error=contract_error,
                )
            record = _check_rehearsal_record(
                client.get(task_id), task_id, task, binding, paths.claimed,
                expected_worker=expected_worker, expected_attempt=expected_attempt,
                write_private=write_private, digest=digest, contract_error=contract_error,
            )
            state.result.update({key: record[key] for key in ("worker", "attempt", "revision")})
            state.keep_subscription = record["status"] not in terminal
        if record["status"] == "cancelled":
            state.result["status"], state.result["reason"] = "cancelled", "cancellation_confirmed"
    elif state.keep_subscription:
        state.result.update(
            status="deferred",
            reason=("ai_outcome_unknown" if record["status"] == "outcome_unknown" else "ai_task_pending"),
        )
    else:
        state.verified = {
            **binding,
            **verify(task, record, task_id=task_id, provider="dot"),
            "worker": record["worker"],
        }


def _cleanup_rehearsal_subscription(
    event_client: Any,
    state: _RehearsalState,
    *,
    end: bool,
    contract_error: type[Exception],
) -> None:
    try:
        sub_state = _rehearsal_subscription(event_client, state.subscription_id, "get_event_subscription")
        retain = (state.keep_subscription and not end and state.expires is not None
                  and datetime.now(timezone.utc) < state.expires)
        if retain and sub_state["status"] == "active":
            if state.creation is None:
                raise contract_error("subscription creation evidence missing")
            _check_rehearsal_subscription(sub_state, state.creation, contract_error)
            state.result["cleanup"] = "retained"
            return
        if sub_state["status"] != "gone":
            _rehearsal_subscription(event_client, state.subscription_id, "delete_event_subscription")
        sub_state = _rehearsal_subscription(event_client, state.subscription_id, "get_event_subscription")
        if sub_state["status"] != "gone":
            raise contract_error("subscription cleanup is unconfirmed")
        state.result["cleanup"] = "gone"
        if retain:
            state.result["subscription_status"] = "gone"
    except Exception:
        state.verified = None
        state.result.update(cleanup="unknown", cleanup_reason="subscription_cleanup_unconfirmed")
        if state.result.get("reason") != "submit_outcome_unknown":
            state.result.update(status="unavailable", reason="subscription_cleanup_unconfirmed")


def _finalize_rehearsal_result(
    paths: _RehearsalPaths | None,
    state: _RehearsalState,
    binding: Mapping[str, Any] | None,
    *,
    write_private: Callable[..., Any],
    contract_error: type[Exception],
) -> dict[str, Any]:
    if state.verified is None or paths is None or binding is None:
        return state.result
    state.verified["cleanup"] = "gone"
    try:
        try:
            write_private(paths.verified, state.verified)
        except FileExistsError:
            if _read_bound_receipt(paths.verified, binding, contract_error) != state.verified:
                raise contract_error("verified receipt mismatch")
        return state.verified
    except Exception:
        return {**state.result, "status": "unavailable", "reason": "verified_receipt_unavailable"}


def run_historical_diagnosis_rehearsal(
    *, instance=None, occurrence=None, submitted_at=None, receipt_path=None,
    client=None, event_client=None, subscription_id=None, cancel=False,
    expected_worker=None, expected_attempt=None, subscription_creation=None, end=False,
) -> dict[str, Any]:
    """Q04's fixed synthetic caller; clients and offline creation evidence are explicit.

    Pending work retains the same subscription across caller invocations. Dot
    claims/reports by callback; the caller never polls workers or subscribes.
    Explicit end or subscription expiry closes without claiming cancellation.
    """
    if any(value is None for value in (
        instance, occurrence, submitted_at, receipt_path, client, event_client,
        subscription_creation,
    )) or any(not _is_nonempty_str(value) for value in (
        subscription_id, expected_worker, expected_attempt,
    )):
        return {"status": "deferred", "reason": "explicit_synthetic_context_required"}

    from ai_service.scenarios import ContractError, load_instance
    from ai_service.scenarios.pilot import digest, prepare, submit, verify
    from ai_service.scenarios.pilot_cli import write_private

    state = _RehearsalState(
        subscription_id=subscription_id,
        result={"status": "unavailable", "reason": "synthetic_operation_rejected"},
    )
    paths: _RehearsalPaths | None = None
    binding: dict[str, Any] | None = None
    try:
        task, creation, binding, deadline, expires, paths = _prepare_rehearsal_context(
            instance=instance, occurrence=occurrence, submitted_at=submitted_at,
            receipt_path=receipt_path, subscription_id=subscription_id,
            expected_worker=expected_worker, expected_attempt=expected_attempt,
            subscription_creation=subscription_creation, load_instance=load_instance,
            prepare=prepare, digest=digest, contract_error=ContractError,
        )
        state.creation = creation
        state.expires = expires
        fresh = _resolve_rehearsal_binding(
            paths, binding, state, write_private=write_private, digest=digest,
            contract_error=ContractError,
        )
        sub_state = _rehearsal_subscription(event_client, state.subscription_id, "get_event_subscription")
        if state.creation is None:
            raise ContractError("subscription creation evidence missing")
        _check_rehearsal_subscription(sub_state, state.creation, ContractError)
        if fresh:
            _submit_rehearsal_task(
                client, task, binding, paths, state, sub_state,
                deadline=deadline, cancel=cancel, end=end,
                expected_worker=expected_worker, expected_attempt=expected_attempt,
                write_private=write_private, digest=digest, submit=submit,
                contract_error=ContractError,
            )
        if paths.submitted.exists():
            _reconcile_submitted_rehearsal(
                client, task, binding, paths, state,
                deadline=deadline, cancel=cancel, end=end,
                expected_worker=expected_worker, expected_attempt=expected_attempt,
                write_private=write_private, digest=digest, verify=verify,
                contract_error=ContractError,
            )
    except Exception:
        # Preserve uncertainty and its original binding; never echo transport errors.
        pass
    finally:
        _cleanup_rehearsal_subscription(event_client, state, end=end, contract_error=ContractError)
    return _finalize_rehearsal_result(
        paths, state, binding, write_private=write_private, contract_error=ContractError,
    )


def _diagnosis_binding():
    """Explicit non-secret scene binding; never infer a route from old CLI settings."""
    import stat

    path = os.environ.get("QUANT_MONITOR_DIAGNOSIS_BINDING")
    if not path:
        raise ValueError("native binding required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("private binding required")
        raw = stream.read(16001)
    if len(raw) > 16000:
        raise ValueError("binding too large")
    return json.loads(raw)


def _diagnosis_native_client(binding, factory):
    from ai_service.client import TaskClient, github_token

    if (not isinstance(binding, dict) or set(binding) != {
            "schema_version", "project", "resource", "audience", "mode", "provider", "profile", "model", "worker"}
            or binding["schema_version"] != "quant.monitor-diagnosis-native.v1"
            or binding["project"] != _OPERATIONAL_DIAGNOSIS_SOURCE_REPOSITORY
            or binding["mode"] not in {"agent", "api"}
            or any(not _is_bounded_str(binding[key], no_space=True)
                   for key in ("resource", "audience", "provider", "profile", "model"))
            or len(binding["profile"]) > 100 or ":" in binding["profile"]
            or (binding["mode"] == "agent" and not _is_bounded_str(binding["worker"]))
            or (binding["mode"] == "api" and binding["worker"] is not None)):
        raise ValueError("native binding required")
    if factory is None and not (
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    ):
        raise ValueError("workload identity required")
    client = (factory(binding) if factory else TaskClient(
        binding["resource"], lambda: github_token(binding["audience"])))
    if not isinstance(client, TaskClient) or client.resource != binding["resource"]:
        raise ValueError("native client mismatch")
    return client


def _diagnosis_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _diagnosis_request(errors, observation, binding):
    text = {"type": "string", "minLength": 1, "maxLength": 2000}
    checks = {"type": "array", "minItems": 1, "maxItems": 10, "items": text}
    properties = {"schema_version": {"const": "quant.monitor-diagnosis.v1"},
                  "likely_cause": text, "evidence_to_inspect": checks, "safe_next_checks": checks,
                  "advisory_only": {"const": True}, "execution_authority_granted": {"const": False}}
    prompt = _operational_diagnosis_prompt(errors, observation=observation)
    return {"mode": binding["mode"], "profile": binding["profile"], "model": binding["model"],
            "objective": prompt, "materials": [], "timeout_seconds": 600,
            "output_schema": {"type": "object", "additionalProperties": False,
                              "required": list(properties), "properties": properties}}


def _save_native_diagnosis(root, state):
    _write_alert_state(root, state)
    directory = os.open(_alert_state_path(root).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class _DiagnosisSession:
    __slots__ = ("root", "binding", "client", "now", "stage")

    def __init__(
        self,
        *,
        root: Path,
        binding: dict[str, Any],
        client: Any,
        now: Callable[[], float],
        stage: str = "diagnosis_state_processing",
    ) -> None:
        self.root = root
        self.binding = binding
        self.client = client
        self.now = now
        self.stage = stage


def _diagnosis_unavailable() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "ai_result_unavailable",
        "failure_stage": "diagnosis_result_processing",
        "failure_category": "result_unavailable",
    }


def _stop_operational_diagnosis(
    session: _DiagnosisSession,
    state: dict[str, Any],
    saved: dict[str, Any],
) -> dict[str, Any]:
    saved["phase"] = "halt"
    if saved["task_id"] and not saved["cancel_sent"]:
        saved["cancel_sent"] = True
        _save_native_diagnosis(session.root, state)
        try:
            session.client.cancel(saved["task_id"])
        except Exception:
            pass  # cancel may have committed; never repeat it automatically.
    else:
        _save_native_diagnosis(session.root, state)
    return {"status": "unavailable", "reason": "ai_task_stop_requested"}


def _halt_operational_diagnosis(
    session: _DiagnosisSession,
    state: dict[str, Any],
    saved: dict[str, Any],
) -> dict[str, Any]:
    saved["phase"] = "halt"
    _save_native_diagnosis(session.root, state)
    return _diagnosis_unavailable()


def _is_valid_saved_diagnosis(
    saved: Any,
    binding: Mapping[str, Any],
    data_errors: list[dict[str, str]],
) -> bool:
    expected_keys = {
        "binding", "request", "request_sha256", "deadline", "task_id", "task_deadline",
        "claim", "output_sha256", "cancel_sent", "phase", "errors", "observation",
    }
    if not isinstance(saved, dict) or set(saved) != expected_keys:
        return False
    return (
        saved["binding"] == binding
        and saved["request_sha256"] == _diagnosis_digest(saved["request"])
        and _diagnosis_digest(saved["request"]) == _diagnosis_digest(
            _diagnosis_request(data_errors, saved["observation"], binding),
        )
        and _is_finite_number(saved["deadline"])
        and type(saved["cancel_sent"]) is bool
        and saved["phase"] in {"intent", "pending", "completed", "halt"}
        and (saved["task_id"] is None or _is_bounded_str(saved["task_id"]))
        and (saved["task_deadline"] is None or _is_finite_number(saved["task_deadline"]))
    )


def _admit_or_resume_diagnosis(
    session: _DiagnosisSession,
    state: dict[str, Any],
    data_errors: list[dict[str, str]],
    fingerprint: str,
    observation: dict[str, str] | None,
    attempt_date: str | None,
    *,
    cancel: bool,
) -> tuple[dict[str, Any] | None, bool, dict[str, Any] | None]:
    native = _require_dict(state.get("operational_diagnosis_native", {}), "invalid diagnosis state")
    legacy_pending = _require_dict(state.get("operational_diagnosis_pending", {}), "invalid diagnosis state")
    saved = native.get(fingerprint)
    if saved is None:
        last_date = state.get("operational_diagnosis_last_attempt_date")
        if attempt_date is not None and last_date is not None and last_date >= attempt_date:
            return None, False, {"status": "skipped", "reason": "daily_attempt_limit"}
        if fingerprint in state.get("operational_diagnosis_attempts", []):
            return None, False, {"status": "skipped", "reason": "already_attempted"}
        # An old pending ID has no frozen native contract; never adopt or replace it.
        if legacy_pending.get(fingerprint) is not None or cancel:
            return None, False, _diagnosis_unavailable()
        request = _diagnosis_request(data_errors, observation, session.binding)
        saved = {
            "binding": session.binding,
            "request": request,
            "request_sha256": _diagnosis_digest(request),
            "deadline": session.now() + 600,
            "task_id": None,
            "task_deadline": None,
            "claim": None,
            "output_sha256": None,
            "cancel_sent": False,
            "phase": "intent",
            "errors": [{key: error[key] for key in ("domain", "code", "error_type")} for error in data_errors],
            "observation": observation,
        }
        native[fingerprint] = saved
        attempts = state.setdefault("operational_diagnosis_attempts", [])
        attempts.append(fingerprint)
        state["operational_diagnosis_native"] = native
        if attempt_date is not None:
            state["operational_diagnosis_last_attempt_date"] = attempt_date
        state["schema_version"] = "quant_monitor_alert_state.v1"
        session.stage = "diagnosis_attempt_persistence"
        _save_native_diagnosis(session.root, state)
        return saved, True, None

    if not _is_valid_saved_diagnosis(saved, session.binding, data_errors):
        return None, False, _diagnosis_unavailable()
    if saved.get("phase") == "completed":
        reason = (
            "daily_attempt_limit"
            if attempt_date and state.get("operational_diagnosis_last_attempt_date", "") >= attempt_date
            else "already_attempted"
        )
        return None, False, {"status": "skipped", "reason": reason}
    if saved.get("phase") == "halt":
        return None, False, _diagnosis_unavailable()
    if saved.get("task_id") is None:
        return None, False, {"status": "unavailable", "reason": "ai_outcome_unknown"}
    return saved, False, None


def _fetch_diagnosis_record(
    session: _DiagnosisSession,
    state: dict[str, Any],
    saved: dict[str, Any],
    fingerprint: str,
    *,
    fresh: bool,
    cancel: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    session.stage = "diagnosis_execution"
    current = session.now()
    if not _is_finite_number(current):
        return None, _halt_operational_diagnosis(session, state, saved)
    if cancel or current >= saved["deadline"] or (
            saved["task_deadline"] is not None and current >= saved["task_deadline"]):
        return None, _stop_operational_diagnosis(session, state, saved)
    try:
        record = (
            session.client.submit(saved["request"], "monitor-diagnosis:" + fingerprint)
            if fresh
            else session.client.get(saved["task_id"])
        )
    except Exception as exc:
        return None, {
            "status": "unavailable",
            "reason": "ai_outcome_unknown",
            "failure_stage": session.stage,
            "failure_category": _diagnosis_failure_category(exc),
        }
    return record, None


def _is_valid_diagnosis_record(
    record: Any,
    binding: Mapping[str, Any],
    saved: Mapping[str, Any],
) -> bool:
    if not isinstance(record, dict):
        return False
    return (
        record.get("owner") == binding["project"]
        and record.get("provider") == binding["provider"]
        and type(record.get("revision")) is int
        and record["revision"] == 1
        and record.get("result_kind") == "advisory"
        and _diagnosis_digest(record.get("request")) == saved["request_sha256"]
        and _is_bounded_str(record.get("id"))
        and (saved["task_id"] is None or saved["task_id"] == record["id"])
        and _is_finite_number(record.get("deadline"))
        and (saved["task_deadline"] is None or saved["task_deadline"] == record["deadline"])
    )


def _validated_diagnosis_claim(
    record: Mapping[str, Any],
    binding: Mapping[str, Any],
    saved: Mapping[str, Any],
) -> list[Any] | None:
    worker, attempt = record.get("worker"), record.get("attempt")
    claim = [worker, attempt, record["revision"]]
    has_identity = worker is not None or attempt is not None
    if binding["mode"] == "api" and has_identity:
        return None
    if binding["mode"] == "agent" and has_identity and (
        worker != binding["worker"] or not _is_bounded_str(attempt)
    ):
        return None
    if saved["claim"] is not None and saved["claim"] != claim:
        return None
    return claim


def _evaluate_diagnosis_record(
    session: _DiagnosisSession,
    state: dict[str, Any],
    saved: dict[str, Any],
    record: Any,
) -> dict[str, Any]:
    session.stage = "diagnosis_result_processing"
    if not _is_valid_diagnosis_record(record, session.binding, saved):
        return _halt_operational_diagnosis(session, state, saved)
    saved["task_id"], saved["task_deadline"] = record["id"], record["deadline"]
    # Save the known task before checking expiry or interpreting its result.
    _save_native_diagnosis(session.root, state)
    current = session.now()
    if not _is_finite_number(current) or current >= min(saved["deadline"], record["deadline"]):
        return _stop_operational_diagnosis(session, state, saved)
    claim = _validated_diagnosis_claim(record, session.binding, saved)
    if claim is None:
        return _halt_operational_diagnosis(session, state, saved)
    if record.get("worker") is not None:
        saved["claim"] = claim
    status = record.get("status")
    if status in {"queued", "submitting", "running"}:
        saved["phase"] = "pending"
        _save_native_diagnosis(session.root, state)
        return {"status": "deferred", "reason": "ai_task_pending", "job_id": record["id"]}
    if status == "completed" and (session.binding["mode"] == "api" or saved["claim"] is not None):
        from jsonschema import Draft202012Validator

        if Draft202012Validator(saved["request"]["output_schema"]).is_valid(record.get("output")):
            saved["phase"] = "completed"
            saved["output_sha256"] = _diagnosis_digest(record["output"])
            _save_native_diagnosis(session.root, state)
            current = session.now()
            if not _is_finite_number(current) or current >= min(saved["deadline"], record["deadline"]):
                return _stop_operational_diagnosis(session, state, saved)
            return {"status": "succeeded", "job_id": record["id"]}
    return _halt_operational_diagnosis(session, state, saved)


def _run_operational_diagnosis(
    root: Path,
    data_errors: list[dict[str, str]],
    fingerprint: str,
    *,
    observation: dict[str, str] | None = None,
    attempt_date: str | None = None,
    config_loader=None,
    client_factory=None,
    clock=None,
    cancel=False,
) -> dict[str, Any]:
    """One selected cloud handler; each invocation performs at most one read/submit.

    Intent and the daily attempt are durable before POST. A lost ID remains HOLD;
    resumption reads only a known, fully bound ID. Cancellation is a request, never
    proof that the worker stopped. Legacy client injection is no longer accepted.
    """
    if not data_errors:
        return {"status": "skipped", "reason": "no_data_errors"}
    if config_loader is None and client_factory is None and not (
        os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    ):
        return {"status": "skipped", "reason": "no_oidc"}
    if not _is_hex64(fingerprint):
        return {"status": "rejected", "reason": "invalid_fingerprint"}
    session = _DiagnosisSession(root=root, binding={}, client=None, now=clock or time.time)
    try:
        # Missing configuration must neither create state nor spend an attempt.
        _load_operational_diagnosis_state(root)
        try:
            binding = json.loads(json.dumps((config_loader or _diagnosis_binding)(), allow_nan=False))
            client = _diagnosis_native_client(binding, client_factory)
            if not _is_finite_number(session.now()) or type(cancel) is not bool:
                raise ValueError("invalid execution limits")
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            return {"status": "deferred", "reason": "ai_task_service_not_configured"}
        session.binding = binding
        session.client = client
        _prepare_operational_diagnosis_root(root)
        with _alert_state_exclusive(root):
            state = _load_operational_diagnosis_state(root)
            saved, fresh, early_result = _admit_or_resume_diagnosis(
                session, state, data_errors, fingerprint, observation, attempt_date, cancel=cancel,
            )
            if early_result is not None or saved is None:
                return early_result or _diagnosis_unavailable()
            record, early_result = _fetch_diagnosis_record(
                session, state, saved, fingerprint, fresh=fresh, cancel=cancel,
            )
            if early_result is not None:
                return early_result
            return _evaluate_diagnosis_record(session, state, saved, record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        early_stages = {"diagnosis_state_processing", "diagnosis_attempt_persistence"}
        return {
            "status": "deferred" if session.stage in early_stages else "unavailable",
            "reason": "dedupe_state_unavailable",
            "failure_stage": session.stage,
            "failure_category": _diagnosis_failure_category(exc),
        }


def _diagnosis_failure_category(error: Exception) -> str:
    """Fixed interface-error categories, without inferring a private cause."""
    return {
        OSError: "io_error", FileNotFoundError: "io_error", PermissionError: "io_error",
        IsADirectoryError: "io_error", NotADirectoryError: "io_error", TimeoutError: "io_error",
        ValueError: "value_error", json.JSONDecodeError: "value_error",
        TypeError: "type_error", KeyError: "key_error", AttributeError: "attribute_error",
        RuntimeError: "runtime_error",
    }.get(type(error), "unknown_error")


def _read_diagnosis_cycle(path: Path, *, now: datetime, max_age: timedelta) -> tuple[datetime, list[dict[str, str]]]:
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("cycle unavailable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("domains") != list(DOMAINS):
        raise ValueError("invalid cycle")
    as_of = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ValueError("cycle time must include timezone")
    age = now - as_of
    if age < -timedelta(minutes=5) or age > max_age:
        raise ValueError("cycle is not current")
    errors = payload.get("data_errors")
    if not isinstance(errors, list) or len(errors) > 100:
        raise ValueError("invalid errors")
    for error in errors:
        if (not isinstance(error, dict) or error.get("domain") not in DOMAINS
                or error.get("code") not in _OPERATIONAL_ERROR_CODES
                or error.get("error_type") not in _OPERATIONAL_ERROR_TYPES):
            raise ValueError("invalid error classification")
    return as_of, [{key: error[key] for key in ("domain", "code", "error_type")} for error in errors]


def diagnose_latest_cycle(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Consume only the current cycle; retain the original current-state interface."""
    try:
        paths = sorted((root / "data/health").glob("cycle_*.json"))
        if not paths:
            raise ValueError("latest cycle unavailable")
        _, errors = _read_diagnosis_cycle(paths[-1], now=now or datetime.now(timezone.utc), max_age=timedelta(hours=2))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"status": "rejected", "reason": "latest_cycle_unavailable"}
    if not errors:
        return {"status": "skipped", "reason": "no_data_errors"}
    return _run_operational_diagnosis(
        root / "data/diagnosis-consumer", errors, _operational_diagnosis_fingerprint(errors)
    )


def diagnose_recent_cycles(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Diagnose at most one unattempted recent category set, without recollection."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state_root = root / "data/diagnosis-consumer"
    failure_stage = "diagnosis_state_processing"
    try:
        state = _load_operational_diagnosis_state(state_root)
        native = _require_dict(state.get("operational_diagnosis_native", {}), "invalid diagnosis state")
        for fingerprint, pending in native.items():
            pending = _require_dict(pending, "invalid diagnosis state")
            if pending.get("phase") in {"intent", "pending"}:
                return _run_operational_diagnosis(
                    state_root, pending["errors"], fingerprint, observation=pending["observation"],
                    attempt_date=now.date().isoformat(),
                )
        last_date = state.get("operational_diagnosis_last_attempt_date")
        if last_date is not None and last_date >= now.date().isoformat():
            return {"status": "skipped", "reason": "daily_attempt_limit"}
        failure_stage = "cycle_discovery"
        paths = sorted((root / "data/health").glob("cycle_*.json"))
        if not paths:
            raise ValueError("latest cycle unavailable")
        failure_stage = "latest_cycle_input_processing"
        latest_at, latest_errors = _read_diagnosis_cycle(paths[-1], now=now, max_age=timedelta(hours=2))
        failure_stage = "recent_cycle_selection"
        cutoff_name = f"cycle_{(now - timedelta(hours=24)).strftime('%Y%m%dT%H%M%SZ')}.json"
        recent = [path for path in paths if path.name >= cutoff_name]
        if len(recent) > 512:
            return {
                "status": "rejected", "reason": "recent_cycle_limit_exceeded",
                "failure_stage": "recent_cycle_selection", "failure_category": "input_limit_exceeded",
            }
        for path in reversed(recent):
            failure_stage = "recent_cycle_input_processing"
            observed_at, errors = _read_diagnosis_cycle(path, now=now, max_age=timedelta(hours=24))
            if observed_at > latest_at:
                raise ValueError("cycle order invalid")
            if not errors:
                continue
            failure_stage = "recent_cycle_selection"
            fingerprint = _operational_diagnosis_fingerprint(errors)
            if fingerprint in state.get("operational_diagnosis_attempts", []):
                continue
            remaining = [error for error in errors if error in latest_errors]
            observation = {
                "observed_at": observed_at.isoformat(),
                "latest_cycle_at": latest_at.isoformat(),
                "current_state": "still_observed" if len(remaining) == len(errors) else (
                    "partially_observed" if remaining else "not_observed_in_latest"
                ),
            }
            failure_stage = "diagnosis_processing"
            result = _run_operational_diagnosis(
                state_root, errors, fingerprint, observation=observation,
                attempt_date=now.date().isoformat(),
            )
            return {**result, "observation": observation}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {
            "status": "rejected", "reason": "recent_cycles_or_state_unavailable",
            "failure_stage": failure_stage, "failure_category": _diagnosis_failure_category(exc),
        }
    return {"status": "skipped", "reason": "no_unattempted_recent_errors"}


def _drift_is_qualified_for_monitoring(drift: Any) -> bool:
    """Retained restrictions and suppressed checks are not new degradation evidence."""
    # QPK's nonempty reason denotes an unevaluable comparison. Older drift
    # objects without qualification metadata retain their existing behavior.
    return _DriftQualification.from_record(drift).is_qualified()


def _build_monitoring_findings(
    strategies: list[dict[str, Any]],
    drift_results: dict[str, list[Any]],
) -> list[Any]:
    from quant_platform_kit.strategy_lifecycle.watch.strategy_watch import build_strategy_monitoring_finding

    unqualified_profiles = {
        (domain, str(drift.strategy_profile or "").strip())
        for domain, drifts in drift_results.items()
        for drift in drifts
        if not _drift_is_qualified_for_monitoring(drift)
    }
    records: dict[tuple[str, str], dict[str, Any]] = {}
    metric_keys = (
        "overall_score",
        "performance_score",
        "risk_score",
        "decay_score",
        "stability_score",
        "operational_score",
        "status",
        "as_of",
    )
    for row in strategies:
        try:
            score = float(row.get("overall_score"))
        except (TypeError, ValueError):
            continue
        if score >= SCORE_ALERT:
            continue
        domain = str(row.get("domain") or "").strip()
        profile = str(row.get("strategy_profile") or "").strip()
        if not domain or not profile:
            continue
        # Do not let the dashboard score bypass the same profile's explicit
        # unqualified/suppressed drift result. Operational errors route separately.
        if (domain, profile) in unqualified_profiles:
            continue
        record = records.setdefault(
            (domain, profile),
            {"metrics": {}, "signals": [], "severity": "medium", "generated_at": ""},
        )
        record["metrics"].update({key: row[key] for key in metric_keys if key in row})
        record["signals"].append(
            {
                "metric": "overall_score",
                "reason": f"overall_score={score:.1f} is below monitoring threshold {SCORE_ALERT:.1f}",
            }
        )
        record["generated_at"] = str(row.get("as_of") or "")
        if str(row.get("status") or "").lower() == "critical" or score <= 40.0:
            record["severity"] = "high"

    for domain, drifts in drift_results.items():
        for drift in drifts:
            profile = str(drift.strategy_profile or "").strip()
            if (domain, profile) in unqualified_profiles:
                continue
            score = float(drift.drift_score or 0.0)
            if score < DRIFT_REVIEW:
                continue
            if not profile:
                continue
            record = records.setdefault(
                (domain, profile),
                {"metrics": {}, "signals": [], "severity": "medium", "generated_at": ""},
            )
            record["metrics"]["drift_score"] = score
            record["signals"].append(
                {
                    "metric": "drift_score",
                    "reason": f"drift_score={score:.2f} exceeds monitoring threshold {DRIFT_REVIEW:.2f}",
                }
            )
            if score >= DRIFT_CRITICAL:
                record["severity"] = "high"

    return [
        build_strategy_monitoring_finding(
            domain=domain,
            profile=profile,
            severity=record["severity"],
            metrics=record["metrics"],
            signals=record["signals"],
            source="quant-monitor/health-cycle",
            generated_at=record["generated_at"],
        )
        for (domain, profile), record in sorted(records.items())
    ]


def _send_health_telegram_target(text: str, token: str, chat_id: str) -> str:
    from quant_monitor_domain.briefing_dispatch import telegram_target_outcome

    return telegram_target_outcome(text=text, token=token, chat_id=chat_id)


def main(*, effects_enabled: bool = True, emit_summary: bool = True,
         on_summary: Callable[[dict[str, Any]], None] | None = None,
         findings_dispatch=None, diagnosis_enabled: bool = True, operational_errors=()) -> int:
    root = Path(os.environ.get("QUANT_MONITOR_ROOT") or Path(__file__).resolve().parents[1])
    out_dir = root / "data" / "health"
    dash_dir = out_dir / "dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    from quant_platform_kit.strategy_lifecycle.drift_detector import run_drift_detection
    from quant_platform_kit.strategy_lifecycle.health_dashboard import build_dashboard
    from quant_platform_kit.strategy_lifecycle.performance_monitor import run_monitor
    from quant_platform_kit.strategy_lifecycle.watch.runner import dispatch_strategy_watch_findings

    expected_profiles, source_revisions, _source_as_of, artifact_errors_by_domain, _not_configured = _load_expected_coverage(root)
    ready_domains = tuple(expected_profiles)
    artifact_errors = list(artifact_errors_by_domain.values())
    live_monitor_reasons: dict[str, str] = {}
    live_drift_reasons: dict[str, str] = {}
    live_snapshot_by_domain: dict[str, Any] = {}

    def run_monitor_with_revision(domain: str):
        # QPK #615 requires observation provenance; use the trusted artifact head_sha.
        snapshots, live_error = _run_expected_monitor_profiles(
            run_monitor,
            domain,
            expected_profiles.get(domain, ()),
            source_revisions[domain],
        )
        if live_error:
            live_monitor_reasons[domain] = live_error
        elif domain == "crypto" and _BINANCE_LIVE_PROFILE in expected_profiles.get(domain, ()):
            live_snapshot_by_domain[domain] = next(
                (row for row in snapshots if _read_lifecycle_identity(row).strategy_profile == _BINANCE_LIVE_PROFILE),
                None,
            )
        return snapshots

    def run_expected_drift(domain: str):
        drifts, live_error = _run_expected_monitor_drifts(
            run_drift_detection,
            domain,
            expected_profiles.get(domain, ()),
            live_snapshot_by_domain.get(domain),
        )
        if live_error:
            live_drift_reasons[domain] = live_error
        return drifts

    snapshot_results, drift_results, lifecycle_errors = _refresh_and_collect_drift(
        run_monitor_with_revision,
        run_expected_drift,
        domains=ready_domains,
        expected_profiles_by_domain=expected_profiles,
        monitor_reason_by_domain=live_monitor_reasons,
        drift_reason_by_domain=live_drift_reasons,
    )
    direct_live_score_rows: dict[str, dict[str, Any]] = {}
    live_drift_by_identity = {
        (domain, drift.strategy_profile): drift
        for domain, drifts in drift_results.items()
        for drift in drifts
    }
    for domain, snapshot in live_snapshot_by_domain.items():
        drift = live_drift_by_identity.get((domain, _BINANCE_LIVE_PROFILE))
        if snapshot is None or drift is None:
            continue
        try:
            from quant_platform_kit.strategy_lifecycle.strategy_health_score import compute_health_score

            score = compute_health_score(snapshot, drift=drift).to_dict()
            if score.get("domain") == domain and score.get("strategy_profile") == _BINANCE_LIVE_PROFILE:
                direct_live_score_rows[domain] = score
        except Exception:
            lifecycle_errors.append(_artifact_status_error(
                domain, code="monitor_data_unavailable", reason_code="live_health_score_unavailable",
            ))
    data_errors = artifact_errors + lifecycle_errors
    build_dashboard(output_dir=str(dash_dir), output_format="json")

    strategies: list[dict[str, Any]] = []
    json_path = dash_dir / "strategy_health_dashboard.json"
    collector_payload_invalid = False
    from build_dashboard_snapshot import build_payload

    if _BINANCE_LIVE_PROFILE in expected_profiles.get("crypto", ()):
        try:
            raw_dashboard = json.loads(json_path.read_text(encoding="utf-8"))
            raw_rows = raw_dashboard.get("strategies") if isinstance(raw_dashboard, dict) else None
            if not isinstance(raw_rows, list):
                raise ValueError("dashboard_data_unavailable")
            raw_rows = [
                row for row in raw_rows
                if not (isinstance(row, dict) and row.get("domain") == "crypto"
                        and row.get("strategy_profile") == _BINANCE_LIVE_PROFILE)
            ]
            if "crypto" in direct_live_score_rows:
                raw_rows.append(direct_live_score_rows["crypto"])
            raw_dashboard["strategies"] = raw_rows
            raw_dashboard["strategy_count"] = len(raw_rows)
            raw_dashboard["summary"] = {
                status: sum(isinstance(row, dict) and row.get("status") == status for row in raw_rows)
                for status in ("healthy", "watch", "review", "critical", "unavailable")
            }
            json_path.write_text(json.dumps(raw_dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            collector_payload_invalid = True

    normalized_path = out_dir / "strategy_health_dashboard.v1.json"
    review_dir = Path(os.environ.get("QUANT_REVIEW_DIR") or root / "data" / "strategy-reviews")
    normalized_payload = build_payload(
        health_file=json_path,
        review_dir=review_dir,
    )
    collector_payload_invalid = (
        normalized_payload.get("data_status") != "ready"
        or bool(normalized_payload.get("errors"))
    )
    refreshed_identities = {
        (domain, snapshot.strategy_profile)
        for domain, snapshots in snapshot_results.items()
        for snapshot in snapshots
    }
    # A prior store row cannot repair an expected profile's missing refresh.
    # A failed domain cannot contribute a health score to the published view.
    failed_domains = {error.get("domain") for error in data_errors}
    domains_known = failed_domains.issubset(DOMAINS)
    normalized_rows = [
        row for row in normalized_payload.get("strategies", [])
        if (row.get("domain"), row.get("profile")) in refreshed_identities
        and (not data_errors or (domains_known and row.get("domain") not in failed_domains))
    ]
    normalized_payload["strategies"] = normalized_rows
    normalized_payload["summary"] = {
        "strategy_count": len(normalized_rows),
        **{status: sum(row.get("status") == status for row in normalized_rows)
           for status in ("healthy", "watch", "review", "critical")},
    }
    if data_errors:
        normalized_payload["data_status"] = (
            "partial" if normalized_rows and domains_known and not collector_payload_invalid else "unavailable"
        )
        normalized_payload["errors"] = list(dict.fromkeys([
            *normalized_payload.get("errors", []),
            *(error["code"] for error in data_errors),
        ]))
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=normalized_path.parent,
            prefix=f".{normalized_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(normalized_payload, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        Path(temp_name).replace(normalized_path)
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink()
            except FileNotFoundError:
                pass
    if not collector_payload_invalid and json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
            collector_payload_invalid = True
        if isinstance(payload.get("strategies"), list):
            strategies = [
                row for row in payload["strategies"] if isinstance(row, dict)
                and (row.get("domain"), row.get("strategy_profile")) in refreshed_identities
            ]
    elif not json_path.is_file():
        collector_payload_invalid = True

    data_error_lines, data_alert_identities = _format_data_error_alerts(data_errors)
    # Operational heartbeat failures alert through the existing durable sender,
    # without invalidating otherwise usable strategy measurements.
    for code in operational_errors:
        if code in {'paper_watchdog_heartbeat_missing', 'paper_watchdog_heartbeat_unavailable'}:
            data_error_lines.append(f"[observation] {code}")
            data_alert_identities.append(f"observation:{code}")
    alert_identities: list[str] = list(data_alert_identities)
    current_drift_identities = {
        (domain, drift.strategy_profile)
        for domain, drifts in drift_results.items()
        for drift in drifts
    }
    monitoring_findings = _build_monitoring_findings([
        row for row in strategies
        if (row.get("domain"), row.get("strategy_profile")) in current_drift_identities
    ], drift_results)
    optimization_watch = (findings_dispatch or dispatch_strategy_watch_findings)(
        monitoring_findings,
        dry_run=False,
        comment_existing=False,
    ) if effects_enabled else {"issues": [], "errors": 0}

    if collector_payload_invalid:
        data_error_lines.append("[collector] dashboard_data_unavailable")
        alert_identities.append("data_error:collector:dashboard_data_unavailable")
    optimization_error_lines: list[str] = []
    optimization_errors = int(optimization_watch.get("errors") or 0)
    if optimization_errors:
        optimization_error_lines.append(
            f"[optimization] issue_record_failed ({optimization_errors})"
        )
        alert_identities.append(
            f"optimization_error:issue_record_failed:{optimization_errors}"
        )
    notify_lines = data_error_lines + optimization_error_lines
    telegram_sent = False
    duplicate_alert_suppressed = False
    telegram_delivery_errors: list[str] = []
    if effects_enabled and notify_lines:
        body = _build_alert_body(notify_lines)
        fingerprint = _alert_fingerprint(alert_identities)
        token = (os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN") or "").strip()
        chat_ids = _health_telegram_chat_ids()
        if token and chat_ids:
            delivery = deliver_telegram_targets(
                root,
                fingerprint,
                chat_ids,
                lambda chat_id: _send_health_telegram_target(body, token, chat_id),
                confirm_legacy=True,
            )
            telegram_sent = bool(delivery["all_sent"])
            duplicate_alert_suppressed = bool(delivery["suppressed"])
            telegram_delivery_errors = list(delivery["errors"])
        else:
            telegram_delivery_errors = ["telegram_missing_env"]
    elif effects_enabled:
        _clear_alert(root)

    operational_diagnosis = _run_operational_diagnosis(
        root,
        data_errors,
        _operational_diagnosis_fingerprint(data_errors),
    ) if effects_enabled and diagnosis_enabled else {"status": "skipped", "reason": "independent_consumer_or_preview"}

    summary = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "domains": list(DOMAINS),
        "strategy_count": len(strategies),
        "telegram_alerts": notify_lines,
        "telegram_sent": telegram_sent,
        "duplicate_alert_suppressed": duplicate_alert_suppressed,
        "telegram_delivery_errors": telegram_delivery_errors,
        "data_errors": data_errors,
        "operational_diagnosis": operational_diagnosis,
        "snapshot_count": sum(len(rows) for rows in snapshot_results.values()),
        "optimization_findings": len(monitoring_findings),
        "optimization_issues_created": len(
            [result for result in optimization_watch.get("issues", []) if result.get("created")]
        ),
        "optimization_issue_errors": optimization_errors,
        "ok": not notify_lines and not collector_payload_invalid,
        "collector_payload_valid": not collector_payload_invalid,
        "snapshot_data_status": normalized_payload.get("data_status"),
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"cycle_{ts}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if on_summary is not None:
        on_summary(summary)
    if emit_summary:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    if sys.argv[1:] == ["--diagnose-static-token-guard-rehearsal"]:
        result = run_historical_diagnosis_rehearsal()
        print(json.dumps(result))
        sys.exit(0 if result["status"] in {"succeeded", "deferred"} else 2)
    if sys.argv[1:] == ["--diagnose-recent"]:
        result = diagnose_recent_cycles(Path(os.environ["QUANT_MONITOR_ROOT"]))
        print(json.dumps(result))
        sys.exit(0 if result["status"] in {"succeeded", "skipped", "deferred"} else 2)
    if sys.argv[1:] == ["--diagnose-latest"]:
        result = diagnose_latest_cycle(Path(os.environ["QUANT_MONITOR_ROOT"]))
        print(json.dumps(result))
        sys.exit(0 if result["status"] in {"succeeded", "skipped", "deferred"} else 2)
    if sys.argv[1:]:
        sys.exit("Unsupported arguments")
    sys.exit(main())
