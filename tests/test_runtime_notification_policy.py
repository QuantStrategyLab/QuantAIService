"""Offline P10 regressions; no production identities or transports."""
from unittest.mock import patch

import pytest

from quant_monitor_domain.briefing_dispatch import dispatch_runtime_digest
from tests.test_runtime_digest import _projection, _record


@pytest.mark.parametrize("status", ["market_closed", "no_signal", "no_submission", "filled", "partially_filled"])
def test_routine_projection_never_sends_second_daily_digest(status):
    with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome") as sender:
        result = dispatch_runtime_digest(_projection(_record(status)))
    sender.assert_not_called()
    assert result["errors"] == []
    assert "runtime_preview_only" in result["skipped"]
    assert result["candidate_persisted"] is False


def test_error_body_contains_no_target_identity_or_source_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("QUANT_MONITOR_ROOT", str(tmp_path))
    payload = _projection(_record("unknown", service="fake-private-service", scope="fake-private-account"))
    with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", return_value="sent") as sender:
        result = dispatch_runtime_digest(payload)
    text = sender.call_args.kwargs["text"]
    assert "【资产管理｜运行错误】" in text
    assert "是否提交订单：未知" in text
    assert "运行链接不可用" in text
    assert "fake-private" not in text
    assert result["telegram_sent"] is True


def test_read_error_cannot_be_silently_deferred(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setenv("GLOBAL_TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("QUANT_MONITOR_ROOT", str(tmp_path))
    payload = _projection(_record("market_closed"), read_errors=["gs://fake-bucket/private"])
    with patch("quant_monitor_domain.briefing_dispatch.telegram_target_outcome", return_value="unknown") as sender:
        first = dispatch_runtime_digest(payload)
        second = dispatch_runtime_digest(payload)
    assert sender.call_count == 1
    assert "telegram_delivery_unknown" in first["errors"]
    assert "telegram_delivery_unknown" in second["errors"]
