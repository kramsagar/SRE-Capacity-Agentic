"""
scripts/test_splunk_collector.py

Standalone test for the Splunk collector.
Tests BOTH modes:
  Mode 1 — HTTP direct  : hits Splunk REST API directly (no MCP, no VS Code needed)
  Mode 2 — MCP mock     : simulates what VS Code Copilot does when it calls your
                          splunk-mcp/server.py, so you can test without VS Code open

Run:
    # Test HTTP mode (direct REST, no MCP)
    python scripts/test_splunk_collector.py --mode http

    # Test MCP mock mode (simulates MCP call by spawning your server.py directly)
    python scripts/test_splunk_collector.py --mode mcp_mock

    # Test a single raw SPL query (quickest sanity check)
    python scripts/test_splunk_collector.py --mode http --single-spl "index=IGTWY | head 5"

    # Full debug output
    python scripts/test_splunk_collector.py --mode http --debug
"""

import asyncio
import json
import logging
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.collectors.splunk_collector import SplunkCollector, _strip_search_keyword

log = logging.getLogger("test_splunk")


# ══════════════════════════════════════════════════════════════════════
# Mode 1 — HTTP direct
# ══════════════════════════════════════════════════════════════════════

async def test_http_mode(namespace: str, days: int, single_spl: str = None):
    """
    Hits your Splunk REST API directly — no MCP, no VS Code needed.
    Needs: SPLUNK_HOST, SPLUNK_TOKEN, SPLUNK_APP env vars.
    """
    print("\n" + "="*60)
    print("  MODE: HTTP direct (no MCP needed)")
    print("="*60)

    url   = os.getenv("SPLUNK_HOST",  "")
    token = os.getenv("SPLUNK_TOKEN", "")
    app   = os.getenv("SPLUNK_APP",   "search")

    if not url or not token:
        print("\n❌  Missing env vars. Set these first:")
        print("    $env:SPLUNK_HOST='https://wf-lp.splunkcloud.com:8089'")
        print("    $env:SPLUNK_TOKEN='eyJraW...'")
        print("    $env:SPLUNK_APP='wf_ui_app_ctdlr'")
        return False

    print(f"\n  SPLUNK_HOST : {url}")
    print(f"  SPLUNK_APP  : {app}")
    print(f"  SPLUNK_TOKEN: {token[:12]}...")
    print(f"  Namespace   : {namespace}")
    print(f"  Days        : {days}")

    c = SplunkCollector(
        base_url=url,
        token=token,
        splunk_app=app,
        mode="http",
        verify_ssl=False,
    )

    # ── Test 1: single SPL (quickest check) ─────────────────────────
    if single_spl:
        print(f"\n── Single SPL test ─────────────────────────────────────")
        print(f"  SPL: {single_spl}")
        rows = await c._http_export(
            spl=single_spl,
            days=days,
            export_url=(
                f"{c.base_url}/servicesNS/{c.splunk_owner}"
                f"/{c.splunk_app}/search/jobs/export"
            ),
        )
        if rows:
            print(f"  ✅ {len(rows)} rows returned")
            print(f"  First row keys: {list(rows[0].keys()) if rows else 'N/A'}")
            for r in rows[:3]:
                print(f"    {r}")
        else:
            print("  ❌ 0 rows — check SPL, index name, or token permissions")
        return bool(rows)

    # ── Test 2: connectivity (index=* | head 1) ──────────────────────
    print(f"\n── Test 1: Basic connectivity ──────────────────────────────")
    rows = await c._http_export(
        spl="index=* | head 1",
        days=1,
        export_url=(
            f"{c.base_url}/servicesNS/{c.splunk_owner}"
            f"/{c.splunk_app}/search/jobs/export"
        ),
    )
    if rows:
        print(f"  ✅ Connected — got {len(rows)} row(s) from index=*")
        print(f"  Sample row keys: {list(rows[0].keys())[:8]}")
    else:
        print("  ❌ No data — check SPLUNK_HOST, SPLUNK_TOKEN, SPLUNK_APP")
        return False

    # ── Test 3: index discovery ──────────────────────────────────────
    print(f"\n── Test 2: Available indexes ───────────────────────────────")
    index_rows = await c._http_export(
        spl="| eventcount summarize=false index=* | dedup index | fields index",
        days=1,
        export_url=(
            f"{c.base_url}/servicesNS/{c.splunk_owner}"
            f"/{c.splunk_app}/search/jobs/export"
        ),
    )
    if index_rows:
        indexes = [r.get("index", "") for r in index_rows if r.get("index")]
        print(f"  ✅ Indexes visible: {indexes[:10]}")
    else:
        print("  ⚠️  Could not list indexes (token may lack list_indexes capability)")

    # ── Test 4: full collect_api_trends ─────────────────────────────
    print(f"\n── Test 3: collect_api_trends (full run) ───────────────────")
    print(f"  Namespace: {namespace}  Days: {days}")
    data = await c.collect_api_trends(namespace, days)

    all_ok = True
    for svc, series in data.items():
        if series:
            total = sum(v for _, v in series)
            print(f"  ✅ {svc:<40} {len(series)} days  total_calls={total:.0f}")
        else:
            print(f"  ⚠️  {svc:<40} 0 data points — check SPL index/sourcetype in splunk_queries.yaml")
            all_ok = False

    return all_ok


# ══════════════════════════════════════════════════════════════════════
# Mode 2 — MCP mock (spawns your server.py directly)
# ══════════════════════════════════════════════════════════════════════

class MockMCPClient:
    """
    Simulates what VS Code Copilot does when it calls your MCP server.
    Spawns splunk-mcp/server.py as a subprocess and sends MCP JSON-RPC
    over stdin/stdout — exactly the stdio protocol VS Code uses.

    This lets you test the FULL MCP path without VS Code being open.
    """

    def __init__(self, server_script: str):
        self.server_script = server_script
        self._proc = None
        self._msg_id = 0

    async def start(self):
        log.debug(f"[MockMCP] Starting server: python {self.server_script}")
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, self.server_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Send MCP initialize handshake
        await self._send({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities":    {},
                "clientInfo":      {"name": "test-client", "version": "1.0"},
            },
        })
        resp = await self._recv()
        log.debug(f"[MockMCP] initialize response: {str(resp)[:200]}")

        # Send initialized notification
        await self._send({
            "jsonrpc": "2.0",
            "method":  "notifications/initialized",
            "params":  {},
        })
        print(f"  ✅ MCP server started: {self.server_script}")

    async def stop(self):
        if self._proc:
            self._proc.stdin.close()
            await self._proc.wait()

    async def call_tool(self, server: str, tool: str, args: dict) -> dict:
        """Simulate mcp_client.call_tool() — what capacity_agent.py calls."""
        log.debug(f"[MockMCP] call_tool: server={server}  tool={tool}  args={args}")
        msg_id = self._next_id()
        await self._send({
            "jsonrpc": "2.0",
            "id":      msg_id,
            "method":  "tools/call",
            "params":  {"name": tool, "arguments": args},
        })
        resp = await asyncio.wait_for(self._recv(), timeout=60)
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        result = resp.get("result", {})
        # MCP tools/call returns { content: [{type: text, text: "..."}] }
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            text = content[0]["text"]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Return as raw string — collector's _extract_rows handles it
                return text
        return result

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _recv(self) -> dict:
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                raise EOFError("MCP server closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                log.debug(f"[MockMCP] Non-JSON line: {line[:80]}")


async def test_mcp_mock_mode(namespace: str, days: int, server_script: str):
    """
    Spawns your splunk-mcp/server.py and calls run_spl_query through it.
    Tests the EXACT same path VS Code Copilot uses.
    """
    print("\n" + "="*60)
    print("  MODE: MCP mock (spawns your server.py directly)")
    print("="*60)

    if not Path(server_script).exists():
        print(f"\n❌  server.py not found: {server_script}")
        print(f"  Pass --server-script path/to/splunk-mcp/server.py")
        return False

    print(f"\n  server.py  : {server_script}")
    print(f"  Namespace  : {namespace}")
    print(f"  Days       : {days}")

    mcp = MockMCPClient(server_script)

    try:
        print(f"\n── Starting your MCP server ────────────────────────────────")
        await mcp.start()

        # Inject mock client into collector
        c = SplunkCollector(
            mcp_client=mcp,
            mcp_server="splunk-mcp",
            mode="splunk_mcp",
        )

        # ── Test 1: run_spl_query with trivial SPL ───────────────────
        print(f"\n── Test 1: run_spl_query basic ─────────────────────────────")
        spl = "index=* | head 3"
        print(f"  SPL: {spl}")
        try:
            raw = await mcp.call_tool(
                server="splunk-mcp",
                tool="run_spl_query",
                args={"spl": spl, "earliest": "-1h", "latest": "now"},
            )
            rows = c._extract_rows(raw, "test")
            if rows:
                print(f"  ✅ {len(rows)} rows returned")
                print(f"  Keys: {list(rows[0].keys())[:8] if rows else 'N/A'}")
            else:
                print(f"  ⚠️  0 rows — raw response: {str(raw)[:200]}")
        except Exception as e:
            print(f"  ❌ run_spl_query failed: {e}")
            return False

        # ── Test 2: full collect_api_trends via MCP ──────────────────
        print(f"\n── Test 2: collect_api_trends via MCP ──────────────────────")
        data = await c.collect_api_trends(namespace, days)

        all_ok = True
        for svc, series in data.items():
            if series:
                total = sum(v for _, v in series)
                print(f"  ✅ {svc:<40} {len(series)} days  total={total:.0f}")
            else:
                print(f"  ⚠️  {svc:<40} 0 data points")
                all_ok = False

        return all_ok

    finally:
        await mcp.stop()
        print("\n  MCP server stopped.")


# ══════════════════════════════════════════════════════════════════════
# SPL query preview — show exactly what will be sent
# ══════════════════════════════════════════════════════════════════════

def show_queries(namespace: str, days: int):
    """Print every SPL query that will be run for this namespace."""
    import yaml
    queries_path = ROOT / "references" / "splunk_queries.yaml"
    raw = yaml.safe_load(queries_path.read_text())
    subs = json.dumps(raw).replace("{namespace}", namespace).replace("{lookback_days}", str(days))
    queries = json.loads(subs)

    print("\n── SPL queries that will be sent ───────────────────────────")
    print(f"  (namespace={namespace}  days={days})\n")

    for svc, spl in queries["api_calls"]["per_service_trend"].items():
        clean = _strip_search_keyword(spl)
        print(f"  Service : {svc}")
        print(f"  MCP SPL : {clean[:120]}")
        print(f"  HTTP SPL: search {clean[:110]}")
        print()


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

async def main():
    import argparse
    p = argparse.ArgumentParser(description="Splunk collector test")
    p.add_argument("--mode",
                   choices=["http", "mcp_mock", "show_queries"],
                   default="http",
                   help=(
                       "http        = direct REST (no MCP, needs SPLUNK_HOST/TOKEN)\n"
                       "mcp_mock    = spawn your server.py and test via MCP protocol\n"
                       "show_queries= print the SPL queries for a namespace"
                   ))
    p.add_argument("--namespace",    default=os.getenv("OCP_NAMESPACE", "alprc-prod"))
    p.add_argument("--days",         type=int, default=14)
    p.add_argument("--single-spl",   default=None,
                   help="Run one specific SPL and exit (http mode only)")
    p.add_argument("--server-script",
                   default=str(ROOT.parent / "splunk-mcp" / "server.py"),
                   help="Path to your splunk-mcp/server.py (mcp_mock mode)")
    p.add_argument("--debug",        action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(name)-38s %(levelname)-8s  %(message)s",
    )

    print(f"\nSplunk Collector Test")
    print(f"─────────────────────")

    if args.mode == "show_queries":
        show_queries(args.namespace, args.days)
        return

    if args.mode == "http":
        ok = await test_http_mode(args.namespace, args.days, args.single_spl)
    else:
        ok = await test_mcp_mock_mode(args.namespace, args.days, args.server_script)

    print("\n" + "="*60)
    print(f"  Result: {'✅ ALL TESTS PASSED' if ok else '⚠️  SOME TESTS FAILED'}")
    print("="*60 + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
