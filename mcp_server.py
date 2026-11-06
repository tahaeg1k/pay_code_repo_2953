"""ScopePay as an MCP server (stdio), so Claude Desktop, Claude Code or any MCP client can shop with a ScopePay code.

No MCP SDK needed: this speaks JSON-RPC 2.0 over stdin/stdout directly. The ScopePay server must be running.
Claude Desktop config (claude_desktop_config.json):
  {"mcpServers": {"scopepay": {"command": "python3", "args": ["/full/path/to/mcp_server.py"],
                               "env": {"SCOPEPAY_URL": "http://localhost:8001"}}}}
Then paste a code into the chat: "Use ScopePay code SP-... to buy vegetarian groceries at Greenleaf, under £15."
"""
import json
import sys

import agent_tools

SERVER = {"name": "scopepay", "version": "0.1.0"}
INSTRUCTIONS = ("Pay at ScopePay demo shops with a scoped ScopePay code the user gives you. Check the code first, stay within "
                "its limits and intent, and never retry a held payment: the owner approves it in their wallet.")


def _tools():
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
            for t in agent_tools.schemas(with_code=True)]


def handle(msg, client):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification (e.g. notifications/initialized)
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools()}
    elif method == "tools/call":
        p = msg.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in {t["name"] for t in _tools()}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        out = agent_tools.run(client, name, args)
        result = {"content": [{"type": "text", "text": agent_tools.as_text(out)}],
                  "isError": bool(isinstance(out, dict) and out.get("error"))}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    client = agent_tools.ScopePayClient(agent_name="MCP client")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(msg, client)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
