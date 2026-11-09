"""A tiny stand-in for ``codex app-server`` used to test the Codex backend."""

import json
import os
import sys

MODE = os.environ.get("FAKE_CODEX_MODE", "normal")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def main():
    thread = None
    while True:
        message = read()
        method = message.get("method")
        params = message.get("params", {})
        if method == "initialize":
            send({"id": message["id"], "result": {"userAgent": "fake-codex/1"}})
        elif method == "initialized":
            continue
        elif method in ("thread/start", "thread/resume"):
            thread = params.get("threadId") or "thread-fake"
            assert params["approvalPolicy"] == "never"
            send({"id": message["id"], "result": {"thread": {"id": thread}}})
        elif method == "turn/start":
            send({"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
            if MODE == "crash":
                sys.exit(3)
            send({"method": "turn/started", "params": {"threadId": thread,
                                                       "turn": {"id": "turn-1"}}})
            if MODE == "usage_limit":
                send({"method": "error", "params": {"threadId": thread, "turnId": "turn-1",
                      "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"}}})
                send({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                    "id": "turn-1", "status": "failed"}}})
                continue
            send({"id": 900, "method": "item/tool/call", "params": {
                "threadId": thread, "turnId": "turn-1", "callId": "call-1",
                "tool": "geyser_call", "arguments": {"operation": "sync", "arguments": {}}}})
            reply = read()
            assert reply["id"] == 900 and "result" in reply, reply
            text = reply["result"]["contentItems"][0]["text"]
            send({"method": "thread/tokenUsage/updated", "params": {"threadId": thread,
                  "tokenUsage": {"last": {"inputTokens": 1000, "cachedInputTokens": 200,
                                          "outputTokens": 50}}}})
            send({"method": "item/completed", "params": {"threadId": thread, "item": {
                "type": "agentMessage", "id": "m1", "text": "synced: " + text[:100]}}})
            send({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": "turn-1", "status": "completed"}}})
        elif method == "turn/interrupt":
            send({"id": message["id"], "result": {}})
        elif "id" in message:
            send({"id": message["id"], "error": {"code": -32601, "message": "unknown"}})


main()
