"""A tiny MCP server over stdio for tests: tools echo and add; honours FAKE_MCP_SECRET in its env."""
import json
import os
import sys

TOOLS = [
    {"name": "echo", "description": "echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "add", "description": "add numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "secret", "description": "reveal the env secret length", "inputSchema": {"type": "object"}},
]


def reply(message_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main():
    print("log line that is not json", flush=True)  # clients must skip this
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        if "id" not in message:
            continue  # notification
        if method == "initialize":
            reply(message["id"], {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}})
        elif method == "tools/list":
            reply(message["id"], {"tools": TOOLS})
        elif method == "tools/call":
            name = message["params"].get("name")
            args = message["params"].get("arguments") or {}
            if name == "echo":
                reply(message["id"], {"content": [{"type": "text", "text": str(args.get("text", ""))}]})
            elif name == "add":
                reply(message["id"], {"content": [{"type": "text", "text": str(float(args.get("a", 0)) + float(args.get("b", 0)))}]})
            elif name == "secret":
                reply(message["id"], {"content": [{"type": "text", "text": str(len(os.environ.get("FAKE_MCP_SECRET", "")))}]})
            elif name == "slow":
                import time
                time.sleep(5)
                reply(message["id"], {"content": []})
            else:
                reply(message["id"], error={"code": -32601, "message": f"unknown tool {name} (token ghp_abcdefghijklmnopqrstuvwxyz0123456789)"})
        else:
            reply(message["id"], error={"code": -32601, "message": "method not found"})


if __name__ == "__main__":
    main()
