"""Telegram outcomes through QPK, with no credential-bearing report records."""
from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request


def deliver(text, *, token, chat_id, token_env="TG_TOKEN", opener=None):
    from quant_platform_kit.notifications import (
        NotificationIntent, redact_sensitive_text, send_notification_intent, send_telegram_message,
    )
    intent = NotificationIntent(text=redact_sensitive_text(text), run_id="monitor", chat_ids=(chat_id,))
    observation = {"status": None, "negative_ack": False}

    def observed_opener(request, *, timeout):
        try:
            with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if status is None and callable(getattr(response, "getcode", None)):
                    status = response.getcode()
                status = 200 if status is None else int(status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read() if callable(getattr(exc, "read", None)) else b""
        observation["status"] = status
        try:
            body = json.loads(raw)
            observation["negative_ack"] = isinstance(body, dict) and body.get("ok") is False
        except (ValueError, UnicodeError):
            pass
        response = io.BytesIO(raw)
        response.status = status
        return response

    if token == os.environ.get(token_env):
        receipt = send_notification_intent(intent, bot_token_env=token_env,
                                           opener=observed_opener, timeout=15)
        ok = receipt["ok"]
    else:
        ok = send_telegram_message(bot_token=token, chat_ids=intent.chat_ids, text=intent.text,
                                   opener=observed_opener, timeout=15,
                                   printer=lambda *_args, **_kwargs: None)
    status = observation["status"]
    outcome = "sent" if ok else "failed" if observation["negative_ack"] or (
        status is not None and 400 <= status < 500) else "unknown"
    return {"delivery": outcome, "http_status": status, "ok": ok}
