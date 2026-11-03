#!/usr/bin/env python3
"""Poll GitHub security alerts through allowlisted gh projections; never log bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib import error, request

UTC = timezone.utc
OWNERS = {"QuantStrategyLab": "QSL_SECURITY_ALERTS_TOKEN", "Pigbibi": "PIGBIBI_SECURITY_ALERTS_TOKEN"}
KINDS = ("secret-scanning", "dependabot")
LOG = logging.getLogger("security-alert-router")


class SafeError(Exception):
    """Only fixed diagnostic text belongs here."""


class DeliveryNotSent(SafeError):
    """Telegram definitively rejected the request (HTTP 4xx incl. 429): not delivered."""


class RedactionFilter(logging.Filter):
    def filter(self, record):
        text = record.getMessage()
        for name in (*OWNERS.values(), "GH_TOKEN", "GITHUB_TOKEN", "TG_TOKEN",
                     "GLOBAL_TELEGRAM_CHAT_ID"):
            value = os.environ.get(name)
            if value:
                text = text.replace(value, "[redacted]")
        text = re.sub(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}\b", "[redacted]", text)
        text = re.sub(r"(?i)(token|secret|password|chat_id)\s*[=:]\s*\S+", r"\1=[redacted]", text)
        record.msg, record.args, record.exc_info, record.exc_text = text, (), None, None
        return True


def stamp(now):
    return now.astimezone(UTC).isoformat()


def date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SafeError("invalid timestamp")
    return parsed.astimezone(UTC)


def gh_page(endpoint, projection, owner, optional=False):
    # gh receives the unavoidable API body in memory, but the secret and location
    # fields are discarded by --jq before stdout reaches this process. Never use
    # --verbose/--include, exception repr, or persist raw responses.
    env = os.environ.copy()
    env.pop("GH_DEBUG", None)
    if env.get(OWNERS[owner]):
        env["GH_TOKEN"] = env[OWNERS[owner]]
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate", "--jq", "(" + projection + ") | @json",
         "-H", "Accept: application/vnd.github+json"],
        capture_output=True, text=True, env=env, timeout=90, check=False,
    )
    if result.returncode:
        if optional and "(HTTP 404)" in result.stderr:
            return None
        raise SafeError("GitHub API read failed")
    projected = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return {"count": sum(page["count"] for page in projected),
            "items": [item for page in projected for item in page["items"]]}


def pages(endpoint, projection, owner, optional=False):
    # gh follows Link headers, including Dependabot cursor pagination. Severity
    # filtering cannot stop pagination, even if a projected page is empty.
    separator = "&" if "?" in endpoint else "?"
    data = gh_page(f"{endpoint}{separator}per_page=100", projection, owner, optional)
    if data is not None:
        yield from data["items"]


def poll(code_scanning=False):
    alerts, repo_count = [], 0
    for owner in OWNERS:
        prefix = "orgs" if owner == "QuantStrategyLab" else "users"
        repos = pages(f"{prefix}/{owner}/repos?type=public",
                      '{count:length,items:[.[] | select(.private == false and '
                      '.archived == false and .fork == false) | .full_name]}', owner)
        for repo in repos:
            if not re.fullmatch(re.escape(owner) + r"/[A-Za-z0-9_.-]+", repo):
                raise SafeError("invalid repository identity")
            repo_count += 1
            for kind in (*KINDS, "code-scanning") if code_scanning else KINDS:
                if kind == "secret-scanning":
                    fields = '{number,created_at,type:.secret_type,severity:"unknown"}'
                    select = ""
                elif kind == "dependabot":
                    fields = ('{number,created_at,type:"dependency",'
                              'severity:.security_advisory.severity}')
                    select = ('select(.security_advisory.severity == "high" or '
                              '.security_advisory.severity == "critical") | ')
                else:
                    fields = ('{number,created_at,type:"code",'
                              'severity:(.rule.security_severity_level // .rule.severity // "unknown")}')
                    select = ""
                projection = '{count:length,items:[.[] | ' + select + fields + ']}'
                for item in pages(f"repos/{repo}/{kind}/alerts?state=open", projection,
                                  owner, optional=kind == "code-scanning"):
                    # No API-supplied URLs, locations, rule text, or descriptions.
                    if type(item["number"]) is not int or item["number"] < 1:
                        raise SafeError("invalid alert number")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item["type"]):
                        raise SafeError("invalid alert type")
                    if item["severity"] not in {"unknown", "low", "medium", "high", "critical",
                                                "error", "warning", "note"}:
                        raise SafeError("invalid severity")
                    alerts.append({"repo": repo, "kind": kind, "number": item["number"],
                                   "type": item["type"], "severity": item["severity"],
                                   "created_at": stamp(date(item["created_at"]))})
    return alerts, repo_count


def identity(alert):
    value = f'{alert["repo"]}/{alert["kind"]}/{alert["number"]}'
    return hashlib.sha256(value.encode()).hexdigest()


def load_state(path):
    if not path.exists():
        return {"version": 1, "alerts": {}}
    value = json.loads(path.read_text())
    if set(value) != {"version", "alerts"} or value["version"] != 1:
        raise SafeError("invalid saved state")
    for key, entry in value["alerts"].items():
        if (not re.fullmatch(r"[a-f0-9]{64}", key)
                or set(entry) != {"first_reported", "reminded", "pending"}):
            raise SafeError("invalid saved state")
        date(entry["first_reported"])
        if type(entry["reminded"]) is not bool or type(entry["pending"]) is not bool:
            raise SafeError("invalid saved state")
    return value


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def plan(alerts, state, now):
    # Deduplicate repeated API rows; keep closed entries so reopen is not new.
    unique = {identity(alert): alert for alert in alerts}
    new = [a for key, a in unique.items() if key not in state["alerts"]]
    reminders = [a for key, a in unique.items() if key in state["alerts"]
                 and not state["alerts"][key]["reminded"] and not state["alerts"][key]["pending"]
                 and now - date(state["alerts"][key]["first_reported"]) >= timedelta(hours=24)]
    return new, reminders


def alert_line(alert, now):
    age = max(0, int((now - date(alert["created_at"])).total_seconds() // 3600))
    url = (f'https://github.com/{alert["repo"]}/security/'
           f'{alert["kind"]}/{alert["number"]}')
    return (f'{alert["repo"]} #{alert["number"]} {alert["kind"]}/{alert["type"]} '
            f'{alert["severity"]}\ncreated_at={alert["created_at"]} age={age}h\n{url}')


def messages(alerts, now, title, summary=False):
    if not alerts:
        return []
    if summary:
        # One bounded bootstrap (or large-batch) message, never a Telegram flood.
        text = f"{title}: {len(alerts)} open alerts. Review repository Security tabs.\n"
        included = 0
        for alert in alerts:
            line = alert_line(alert, now) + "\n"
            if len(text) + len(line) > 3500:
                break
            text += line
            included += 1
        text += f"\nDetails shown: {included}/{len(alerts)}. All counted alerts are tracked."
        return [text]
    return [f"{title}\n{alert_line(alert, now)}" for alert in alerts]


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TG_TOKEN", "")
        self.chat = os.environ.get("GLOBAL_TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat:
            raise SafeError("existing Telegram configuration unavailable")
        self.opener = request.build_opener(NoRedirect)
        self.last_send = 0.0
        info = self.call("getMe", {})
        bot = info.get("result", {})
        expected = os.environ.get("QUANT_SENTINEL_EXPECTED_BOT_ID_SHA256", "")
        if not expected or hashlib.sha256(str(bot.get("id")).encode()).hexdigest() != expected:
            raise SafeError("Telegram bot identity mismatch")
        username = bot.get("username", "")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", username):
            raise SafeError("invalid Telegram bot username")
        LOG.info("Telegram bot username=%s", username)

    def call(self, method, payload):
        req = request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                              data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as response:
                status = response.status
                LOG.info("Telegram HTTP status=%d", status)
                if status != 200:
                    raise SafeError("Telegram request failed")
                body = json.load(response)
        except error.HTTPError as exc:
            LOG.error("Telegram HTTP status=%d", exc.code)
            if 400 <= exc.code < 500:
                # Client errors and rate limits are rejected before delivery, so the
                # pending checkpoint can be rolled back safely. 5xx stays uncertain.
                raise DeliveryNotSent("Telegram rejected the request; not delivered") from None
            raise SafeError("Telegram request failed") from None
        except (error.URLError, TimeoutError, OSError):
            # Never print the exception: it can contain the bot URL/token.
            raise SafeError("Telegram delivery outcome unknown; no automatic retry") from None
        if body.get("ok") is not True:
            raise SafeError("Telegram request rejected")
        return body

    def send(self, text):
        time.sleep(max(0, 1.1 - (time.monotonic() - self.last_send)))
        from quant_platform_kit.notifications import (
            NotificationIntent, send_notification_intent, send_telegram_message,
        )
        intent = NotificationIntent(text=text, run_id="security-alert-router", chat_ids=(self.chat,))
        observed_status = None
        def opener(request, *, timeout):
            nonlocal observed_status
            try:
                return self.opener.open(request, timeout=timeout)
            except error.HTTPError as exc:
                observed_status = int(exc.code)
                raise
        if self.token == os.environ.get("TG_TOKEN"):
            receipt = send_notification_intent(intent, bot_token_env="TG_TOKEN", opener=opener, timeout=30)
            ok = receipt["ok"]
            observed_status = receipt["http_status"]
        else:
            ok = send_telegram_message(bot_token=self.token, chat_ids=intent.chat_ids, text=intent.text,
                                       opener=opener, timeout=30,
                                       printer=lambda *_args, **_kwargs: None)
        if not ok:
            if observed_status is not None and 400 <= observed_status < 500:
                raise DeliveryNotSent("Telegram rejected the request; not delivered")
            raise SafeError("Telegram delivery outcome unknown; no automatic retry")
        self.last_send = time.monotonic()


def route(alerts, state, now, path, sender):
    if any(entry["pending"] for entry in state["alerts"].values()):
        raise SafeError("previous delivery outcome requires reconciliation")
    new, reminders = plan(alerts, state, now)
    for group, title, reminder in ((new, "New security alerts", False),
                                   (reminders, "Still open after 24h", True)):
        summary = not state["alerts"] or len(group) > 10
        batches = [group] if summary and group else [[alert] for alert in group]
        for batch in batches:
            for alert in batch:
                key = identity(alert)
                if not reminder:
                    state["alerts"][key] = {"first_reported": stamp(now), "reminded": False,
                                            "pending": True}
                else:
                    state["alerts"][key]["pending"] = True
            save_state(path, state)
            try:
                sender(messages(batch, now, title, summary=summary)[0])
            except DeliveryNotSent:
                # Definitely not delivered: undo only this batch's pending marks so the
                # next scheduled run retries. Uncertain outcomes stay pending.
                for alert in batch:
                    key = identity(alert)
                    if reminder:
                        state["alerts"][key]["pending"] = False
                    else:
                        state["alerts"].pop(key, None)
                save_state(path, state)
                raise
            for alert in batch:
                key = identity(alert)
                state["alerts"][key]["pending"] = False
                if reminder:
                    state["alerts"][key]["reminded"] = True
                else:
                    state["alerts"][key]["first_reported"] = stamp(now)
            save_state(path, state)


def main(argv=None):
    from quant_platform_kit.notifications import install_log_redaction
    install_log_redaction(wrap_streams=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="counts only; no sends or state writes")
    parser.add_argument("--code-scanning", action="store_true", help="also poll code scanning; 404 is disabled")
    parser.add_argument("--state", type=Path, default=Path("security-alert-state.json"))
    parser.add_argument("--fixture", type=Path, help="offline synthetic alerts; requires --dry-run")
    args = parser.parse_args(argv)
    if args.fixture and not args.dry_run:
        parser.error("--fixture requires --dry-run")
    handler = logging.StreamHandler()
    handler.addFilter(RedactionFilter())
    LOG.handlers = [handler]
    LOG.propagate = False
    LOG.setLevel(logging.INFO)
    install_log_redaction(wrap_streams=True)
    try:
        state = load_state(args.state)
        if args.fixture:
            alerts = json.loads(args.fixture.read_text())
            repo_count = len({a["repo"] for a in alerts})
        else:
            alerts, repo_count = poll(args.code_scanning)
        now = datetime.now(UTC)
        new, reminders = plan(alerts, state, now)
        print(json.dumps({"repositories": repo_count, "open": len(alerts),
                          "new": len(new), "reminders": len(reminders),
                          "pending": sum(e["pending"] for e in state["alerts"].values())},
                         sort_keys=True))
        if not args.dry_run:
            if any(entry["pending"] for entry in state["alerts"].values()):
                raise SafeError("previous delivery outcome requires reconciliation")
            if new or reminders:
                route(alerts, state, now, args.state, Telegram().send)
    except Exception:
        # Fixed diagnostics only: no response bodies, alert details or exceptions.
        LOG.error("Security alert routing failed; no automatic retry")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
