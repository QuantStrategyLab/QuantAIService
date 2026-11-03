#!/usr/bin/env python3
"""Prepare private Wrangler configuration and run quiet, read-only smoke checks."""
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request


def require(condition):
    if not condition:
        raise ValueError("invalid deployment configuration")


def mask(value):
    if isinstance(value, dict):
        for item in value.values():
            mask(item)
    elif isinstance(value, list):
        for item in value:
            mask(item)
    elif isinstance(value, str) and value:
        escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::add-mask::" + escaped)


def configure(directory):
    base = json.loads((directory / "wrangler.example.jsonc").read_text())
    private = json.loads(os.environ["WORKER_CONFIG_JSON"])
    require(isinstance(private, dict))
    mask(private)
    # Private data cannot replace executable paths or the committed log policy.
    require(not set(private) & {"main", "build", "assets", "site", "env", "account_id",
                                "observability", "no_bundle"})
    require(all(key in private for key in ("name", "vars", "kv_namespaces",
                                          "durable_objects", "services", "migrations",
                                          "routes", "workers_dev")))
    require(private["name"] == os.environ["WORKER_NAME"] and bool(private["name"]))
    require(isinstance(private["routes"], list) and isinstance(private["workers_dev"], bool))
    require(isinstance(private["vars"], dict))
    require(set(base["vars"]) <= set(private["vars"]))
    require(private["vars"]["ORGANIZATION"] == base["vars"]["ORGANIZATION"])
    require(private["vars"]["ORGANIZATION_ID"] == base["vars"]["ORGANIZATION_ID"])
    origin = private["vars"]["PUBLIC_ORIGIN"]
    parsed = urllib.parse.urlsplit(origin)
    require(parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
            and not parsed.password and parsed.path in ("", "/")
            and not parsed.query and not parsed.fragment)
    require(any(item.get("binding") == "OAUTH_KV" and item.get("id")
                for item in private["kv_namespaces"]))
    require(private["durable_objects"] == base["durable_objects"])
    require(any(item.get("binding") == "TASK_SERVICE" and item.get("service") == private["name"]
                and item.get("entrypoint") == "TaskService" for item in private["services"]))
    require(bool(private["migrations"]))
    config = {**base, **private}
    serialized = json.dumps(config)
    require("CONFIGURE_" not in serialized and ".example" not in serialized)
    config["main"] = str((directory / base["main"]).resolve())
    config["keep_vars"] = False
    log_policy = config["observability"]
    require(log_policy["enabled"] is True and log_policy["logs"]["enabled"] is True
            and log_policy["logs"]["invocation_logs"] is False)
    destination = directory / ".wrangler" / "production.json"
    destination.parent.mkdir(exist_ok=True, mode=0o700)
    # Only ephemeral runners hold this file; never upload it or Wrangler output.
    with open(destination, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(config, stream)
    print("Private deployment configuration validated.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def smoke(directory):
    config = json.loads((directory / ".wrangler" / "production.json").read_text())
    mask(config)
    token = os.environ.get("SMOKE_TASKS_TOKEN", "")
    mask(token)
    origin = config["vars"]["PUBLIC_ORIGIN"].rstrip("/")
    opener = urllib.request.build_opener(NoRedirect)
    checks = [("/healthz", False)]
    if token:
        checks += [("/v1/tasks?limit=1", True), ("/v1/tasks/stats", True)]
    for path, authenticated in checks:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(origin + path, headers=headers, method="GET")
        with opener.open(request, timeout=30) as response:
            require(response.status == 200)
            payload = json.load(response)
            require(isinstance(payload, dict))
            if not authenticated:
                require(payload == {"status": "ok"})
            elif path.startswith("/v1/tasks?"):
                require(isinstance(payload.get("tasks"), list) and "next_cursor" in payload)
            else:
                require(isinstance(payload.get("counts"), list) and isinstance(payload.get("stuck"), dict))
        print("Read-only smoke check passed.")
    if not token:
        print("Task smoke checks skipped: SMOKE_TASKS_TOKEN is not set.")


if __name__ == "__main__":
    try:
        {"configure": configure, "smoke": smoke}[sys.argv[1]](Path(sys.argv[2]).resolve())
    except Exception:
        # Parser, HTTP and filesystem exceptions can contain private values.
        print("Deployment preparation or smoke check failed; private details suppressed.", file=sys.stderr)
        raise SystemExit(1) from None
