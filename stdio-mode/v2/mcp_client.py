"""
agent/mcp_client.py

Spawns your 3 MCP servers as stdio subprocesses and calls their tools.

Servers (from .vscode/mcp.json):
  grafana-mcp  →  ../grafana-mcp/index.js       (node)
  splunk-mcp   →  ../splunk-mcp/server.py        (python)
  ocp-mcp      →  ../ocp-mcp/server.py           (python)

Usage:
    async with MCPClient() as mcp:
        result = await mcp.call("grafana-mcp", "query_prometheus_range", {...})
        result = await mcp.call("splunk-mcp",  "run_spl_query",          {...})
        result = await mcp.call("ocp-mcp",     "list_resources",         {...})
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent          # sre-agent/
SERVERS = {
    "grafana-mcp": {
        "cmd":  ["node", str(ROOT.parent / "grafana-mcp" / "index.js")],
        "env":  {
            "GRAFANA_URL":   os.getenv("GRAFANA_URL",   ""),
            "GRAFANA_TOKEN": os.getenv("GRAFANA_TOKEN", "")
                          or os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
        },
    },
    "splunk-mcp": {
        "cmd":  [sys.executable, str(ROOT.parent / "splunk-mcp" / "server.py")],
        "env":  {
            "SPLUNK_HOST":       os.getenv("SPLUNK_HOST",  ""),
            "SPLUNK_TOKEN":      os.getenv("SPLUNK_TOKEN", ""),
            "SPLUNK_APP":        os.getenv("SPLUNK_APP",   "search"),
            "SPLUNK_OWNER":      os.getenv("SPLUNK_OWNER", "nobody"),
            "SPLUNK_VERIFY_SSL": os.getenv("SPLUNK_VERIFY_SSL", "false"),
        },
    },
    "ocp-mcp": {
        "cmd":  [sys.executable, str(ROOT.parent / "ocp-mcp" / "server.py")],
        "env":  {
            "OCP_API_URL": os.getenv("OCP_API_URL", ""),
            "OCP_TOKEN":   os.getenv("OCP_TOKEN",   ""),
        },
    },
}


class _Server:
    """One running MCP stdio server process."""

    def __init__(self, name: str, cmd: list, extra_env: dict):
        self.name    = name
        self.cmd     = cmd
        self.env     = {**os.environ, **extra_env}
        self._proc   = None
        self._msg_id = 0
        self._lock   = asyncio.Lock()

    async def start(self):
        log.info(f"[MCP] Starting {self.name}: {' '.join(self.cmd)}")
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        await self._handshake()
        log.info(f"[MCP] {self.name} ready")

    async def stop(self):
        if self._proc:
            try:
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                self._proc.kill()
            log.info(f"[MCP] {self.name} stopped")

    async def call(self, tool: str, args: dict) -> any:
        """Send tools/call and return parsed result."""
        async with self._lock:   # one request at a time per server
            msg_id = self._next_id()
            await self._send({
                "jsonrpc": "2.0",
                "id":      msg_id,
                "method":  "tools/call",
                "params":  {"name": tool, "arguments": args},
            })
            resp = await asyncio.wait_for(self._recv(), timeout=120)

        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"[MCP] {self.name}/{tool} error {err.get('code')}: {err.get('message')}"
            )

        # MCP returns: { result: { content: [{type: text, text: "..."}] } }
        content = resp.get("result", {}).get("content", [])
        if not content:
            return {}

        text = content[0].get("text", "") if content[0].get("type") == "text" else ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Some tools return plain text — return as-is
            return text

    # ── MCP handshake ─────────────────────────────────────────────────

    async def _handshake(self):
        await self._send({
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  "initialize",
            "params":  {
                "protocolVersion": "2024-11-05",
                "capabilities":    {},
                "clientInfo":      {"name": "sre-capacity-agent", "version": "1.0"},
            },
        })
        resp = await asyncio.wait_for(self._recv(), timeout=15)
        log.debug(f"[MCP] {self.name} initialize: {str(resp)[:120]}")

        # Send initialized notification (no response expected)
        await self._send({
            "jsonrpc": "2.0",
            "method":  "notifications/initialized",
            "params":  {},
        })

    # ── stdio helpers ──────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()
        log.debug(f"[MCP] → {self.name}: {line[:120]}")

    async def _recv(self) -> dict:
        """Read lines until we get a valid JSON-RPC response (not a notification)."""
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                stderr = await self._proc.stderr.read(500)
                raise EOFError(
                    f"[MCP] {self.name} closed stdout.\n"
                    f"  stderr: {stderr.decode()[:300]}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                log.debug(f"[MCP] ← {self.name}: {line[:120]}")
                # Skip notifications (no id field)
                if "id" in msg:
                    return msg
            except json.JSONDecodeError:
                log.debug(f"[MCP] {self.name} non-JSON: {line[:80]}")


# ══════════════════════════════════════════════════════════════════════
# MCPClient — manages all 3 servers
# ══════════════════════════════════════════════════════════════════════

class MCPClient:
    """
    Start all 3 MCP servers and call their tools.

    Use as async context manager:
        async with MCPClient() as mcp:
            result = await mcp.call("splunk-mcp", "run_spl_query", {"spl": "..."})

    Or call start()/stop() manually.
    """

    def __init__(self, servers: list[str] = None):
        """
        servers: which servers to start. Default = all 3.
        Pass a subset to start only what you need:
            MCPClient(servers=["splunk-mcp"])
        """
        names = servers or list(SERVERS.keys())
        self._servers: dict[str, _Server] = {
            name: _Server(name, **{
                "cmd":       SERVERS[name]["cmd"],
                "extra_env": SERVERS[name]["env"],
            })
            for name in names
            if name in SERVERS
        }

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    async def start(self):
        """Start all servers in parallel."""
        await asyncio.gather(*[s.start() for s in self._servers.values()])

    async def stop(self):
        """Stop all servers."""
        await asyncio.gather(*[s.stop() for s in self._servers.values()])

    async def call(self, server: str, tool: str, args: dict) -> any:
        """
        Call a tool on a specific server.

        Args:
            server: "grafana-mcp" | "splunk-mcp" | "ocp-mcp"
            tool:   tool name from that server's list_tools
            args:   tool arguments dict

        Returns:
            Parsed JSON result, or raw string if not JSON.
        """
        if server not in self._servers:
            raise ValueError(
                f"Server '{server}' not started. "
                f"Available: {list(self._servers.keys())}"
            )
        log.debug(f"[MCP] call: {server}/{tool}  args={list(args.keys())}")
        return await self._servers[server].call(tool, args)


# ── CLI smoke-test ──────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser(description="MCP client smoke-test")
    p.add_argument("--server", default="splunk-mcp",
                   choices=["grafana-mcp", "splunk-mcp", "ocp-mcp"])
    p.add_argument("--tool",   default="run_spl_query")
    p.add_argument("--args",   default='{"spl":"index=* | head 1","earliest":"-5m"}',
                   help="JSON args string")
    p.add_argument("--debug",  action="store_true")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.debug else logging.INFO,
        format="%(asctime)s  %(name)-30s %(levelname)-8s  %(message)s",
    )

    args = json.loads(a.args)
    print(f"\nCalling {a.server}/{a.tool}")
    print(f"Args: {args}\n")

    async with MCPClient(servers=[a.server]) as mcp:
        result = await mcp.call(a.server, a.tool, args)
        print("Result:")
        print(json.dumps(result, indent=2) if isinstance(result, (dict, list)) else result)

if __name__ == "__main__":
    asyncio.run(_cli())
