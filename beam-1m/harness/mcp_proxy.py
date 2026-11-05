#!/usr/bin/env python3
"""A stdio MCP server for `claude -p` that hands the reader's memory-tool calls to run.py.

The tools, their descriptions and input schemas are the SDK's own (run.py wrote them to DEFS); every result is the
SDK's text for that call. Newline-delimited JSON-RPC 2.0, the tools capability only.
env: QID, TOOLS_URL (run.py's loopback address), TOOLS_TOKEN, DEFS (the definitions JSON)
"""
import json
import os
import sys
import urllib.request

QID, TOOLS_URL, TOKEN = os.environ["QID"], os.environ["TOOLS_URL"], os.environ["TOOLS_TOKEN"]
DEFS = json.load(open(os.environ["DEFS"]))


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def run(name, arguments):
    r = urllib.request.Request(TOOLS_URL + "/run", method="POST",
                               data=json.dumps({"qid": QID, "name": name, "input": arguments}).encode(),
                               headers={"Content-Type": "application/json", "X-Harness-Token": TOKEN})
    with urllib.request.urlopen(r, timeout=600) as f:
        return json.load(f)["text"]


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue  # a notification
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                                      "serverInfo": {"name": "m", "version": "1.0.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": d["name"], "description": d["description"], "inputSchema": d["input_schema"]} for d in DEFS]}})
    elif method == "tools/call":
        p = msg.get("params") or {}
        try:
            text = run(p.get("name"), p.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": False}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"Tool failed: {e}"}], "isError": True}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
