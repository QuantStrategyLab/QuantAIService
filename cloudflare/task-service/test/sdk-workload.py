"""Capture the unchanged SDK wire protocol without network or real credentials."""
from pathlib import Path
import importlib
import json
import socket
import sys

root = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").is_file())
sys.path.insert(0, str(root / "src"))
TaskClient = importlib.import_module("ai_service.client").TaskClient
HttpReply = importlib.import_module("ai_service.transport").HttpReply


def no_network(*args, **kwargs):
    raise AssertionError("SDK capture must not open a network connection")


socket.socket = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network

calls = []


class Capture:
    def request(self, url, *, method, headers, body):
        calls.append({"url": url, "method": method, "headers": headers,
                      "body": None if body is None else body.decode("utf-8")})
        return HttpReply(200, b"{}")


fixture = json.load(sys.stdin)
client = TaskClient(fixture["origin"], fixture["token"], transport=Capture())
client.message(fixture["task_id"], {"text": "Original SDK follow-up"}, "sdk-follow-up")
client.request("GET", "/v1/tasks/" + fixture["task_id"] + "/events?after=0")
json.dump(calls, sys.stdout)
