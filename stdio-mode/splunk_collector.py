"""
scripts/collectors/splunk_collector.py

Fetches daily API-call counts per service from Splunk.

YOUR SPLUNK MCP SERVER TOOLS (from splunk-mcp/server.py):
  run_spl_query         args: spl (required), earliest (default -1h), latest (default now)
                        NOTE: spl must NOT include leading 'search' keyword
                        Auth/app context handled internally by your MCP server

  list_all_dashboards   args: none
  list_all_alerts       args: none
  list_metrics          args: none
  tail_index_events     args: index, category, ...
  analyze_dashboard_all_panels  args: dashboard_name, input_tokens, earliest, latest, appname

Two modes:
  "splunk_mcp"  — calls your server.py via VS Code MCP (default)
                  uses tool: run_spl_query
  "http"        — direct Splunk REST API with httpx (for standalone CLI testing)
                  uses synchronous export endpoint

SPL QUERIES come from references/splunk_queries.yaml
  {namespace} and {lookback_days} placeholders are substituted at runtime.
"""

import httpx
import yaml
import json
import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT         = Path(__file__).parent.parent.parent
QUERIES_PATH = ROOT / "references" / "splunk_queries.yaml"

log = logging.getLogger(__name__)


# ── URL sanitiser (http mode only) ────────────────────────────────────

def _base_url(raw: str) -> str:
    """Strip any path after host:port so pasted full URLs don't break httpx."""
    raw    = raw.strip().rstrip("/")
    parsed = urlparse(raw)
    base   = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base = f"{base}:{parsed.port}"
    if base != raw:
        log.warning(f"[Splunk] URL normalised: '{raw}' → '{base}'")
    return base


def _load_queries(namespace: str, lookback_days: int = 30) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(
        json.dumps(raw)
        .replace("{namespace}", namespace)
        .replace("{lookback_days}", str(lookback_days))
    )


def _strip_search_keyword(spl: str) -> str:
    """
    Your run_spl_query tool does NOT want the leading 'search' keyword.
    Strip it if present so queries from the YAML work in both modes.
    """
    spl = spl.strip()
    if spl.lower().startswith("search "):
        spl = spl[7:].strip()
    return spl


# ══════════════════════════════════════════════════════════════════════
# SplunkCollector
# ══════════════════════════════════════════════════════════════════════

class SplunkCollector:
    """
    Fetch daily API call counts per service using your Splunk MCP server.

    MCP mode (default — recommended):
        Uses your server.py tool: run_spl_query
        Auth and app context are handled by your MCP server internally.
        No SPLUNK_TOKEN or SPLUNK_APP needed in this mode.
        mcp_client is injected by capacity_agent.py at runtime.

    HTTP mode (standalone / CLI testing):
        Hits Splunk REST export endpoint directly.
        Needs SPLUNK_HOST, SPLUNK_TOKEN, SPLUNK_APP env vars.

    Returns:
        { "service-name": [("2025-01-15", 12340.0), ...], ... }
    """

    def __init__(
        self,
        # ── MCP mode args ─────────────────────────────────────────
        mcp_client          = None,
        mcp_server:  str   = "splunk-mcp",  # key in your mcp.json servers block

        # ── HTTP mode args (only needed for standalone CLI) ────────
        base_url:    str   = None,
        token:       str   = None,
        splunk_app:  str   = None,
        splunk_owner:str   = "nobody",
        verify_ssl:  bool  = False,

        mode:        str   = "splunk_mcp",
        timeout:     int   = 120,
    ):
        self.mcp_client   = mcp_client
        self.mcp_server   = mcp_server
        self.mode         = mode
        self.timeout      = timeout

        # HTTP mode config (from args or env vars)
        self.base_url     = _base_url(base_url or os.getenv("SPLUNK_HOST", "https://localhost:8089"))
        self.token        = token        or os.getenv("SPLUNK_TOKEN",  "")
        self.splunk_app   = splunk_app   or os.getenv("SPLUNK_APP",    "search")
        self.splunk_owner = splunk_owner or os.getenv("SPLUNK_OWNER",  "nobody")
        self.verify_ssl   = verify_ssl

        log.info(
            f"[Splunk] SplunkCollector init: mode={mode}  "
            f"mcp_server={mcp_server}  "
            f"{'base_url=' + self.base_url + '  app=' + self.splunk_app if mode == 'http' else 'auth=handled-by-mcp-server'}"
        )

    # ── Public API ────────────────────────────────────────────────────

    async def collect_api_trends(
        self, namespace: str, days: int = 30
    ) -> dict[str, list[tuple[str, float]]]:
        """Return per-service daily API call series."""
        log.info(f"[Splunk] collect_api_trends: namespace={namespace}  days={days}  mode={self.mode}")

        if self.mode == "splunk_mcp":
            return await self._mcp_collect(namespace, days)
        return await self._http_collect(namespace, days)

    async def collect_namespace_trend(
        self, namespace: str, days: int = 30
    ) -> list[tuple[str, float]]:
        """Total API calls across all services per day (summed)."""
        queries = _load_queries(namespace, days)
        spl     = queries["api_calls"]["namespace_trend"]
        rows    = await self._run_search(spl, days)
        totals: dict[str, float] = {}
        for row in rows:
            date = row.get("_time", "")
            for k, v in row.items():
                if not k.startswith("_"):
                    totals[date] = totals.get(date, 0.0) + float(v or 0)
        return sorted(totals.items())

    # ── MCP path — uses your run_spl_query tool ───────────────────────

    async def _mcp_collect(
        self, namespace: str, days: int
    ) -> dict[str, list[tuple[str, float]]]:
        """
        Calls your Splunk MCP server tool: run_spl_query
          args: { spl: "...", earliest: "-30d", latest: "now" }
          NOTE: spl must NOT start with 'search' keyword
          Your server handles Splunk auth/app context internally.
        """
        if self.mcp_client is None:
            log.error(
                "[Splunk] mcp_client is None — cannot call MCP tools. "
                "Use mode='http' for standalone testing."
            )
            return {}

        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())
        results  = {}

        log.info(f"[Splunk] MCP: running {len(services)} service queries via run_spl_query")

        for svc in services:
            raw_spl = queries["api_calls"]["per_service_trend"][svc]
            # Strip 'search' keyword — your tool doesn't want it
            spl = _strip_search_keyword(raw_spl)

            log.debug(f"[Splunk] MCP run_spl_query: service={svc}  spl={spl[:100]}...")

            try:
                raw = await self.mcp_client.call_tool(
                    server=self.mcp_server,
                    tool="run_spl_query",
                    args={
                        "spl":      spl,
                        "earliest": f"-{days}d",
                        "latest":   "now",
                    },
                )

                log.debug(f"[Splunk] MCP response for {svc}: {str(raw)[:300]}")
                rows = self._extract_rows(raw, svc)
                results[svc] = [
                    (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                    for row in rows
                ]
                log.info(f"[Splunk] {svc}: {len(results[svc])} data points via MCP")

            except Exception as e:
                log.error(f"[Splunk] MCP run_spl_query failed for {svc}: {type(e).__name__}: {e}")
                results[svc] = []

        return results

    def _extract_rows(self, raw, svc: str) -> list[dict]:
        """
        Handle different response formats your MCP server might return:
          1. List of dicts directly:  [{_time: ..., api_calls: ...}, ...]
          2. Dict with results key:   {results: [...]}
          3. Dict with rows key:      {rows: [...]}
          4. Newline-delimited JSON string (some MCP implementations)
        """
        if raw is None:
            log.warning(f"[Splunk] run_spl_query returned None for {svc}")
            return []

        # Already a list
        if isinstance(raw, list):
            log.debug(f"[Splunk] {svc}: response is list of {len(raw)} rows")
            return raw

        if isinstance(raw, dict):
            # Standard Splunk results wrapper
            for key in ("results", "rows", "data", "hits"):
                if key in raw:
                    rows = raw[key]
                    log.debug(f"[Splunk] {svc}: extracted {len(rows)} rows from '{key}' key")
                    return rows
            # MCP might wrap in content list (some implementations)
            if "content" in raw:
                content = raw["content"]
                if isinstance(content, list):
                    return content
                if isinstance(content, str):
                    return self._parse_ndjson(content, svc)
            log.warning(f"[Splunk] {svc}: unexpected dict format, keys={list(raw.keys())[:8]}")
            return []

        # String response — try newline-delimited JSON
        if isinstance(raw, str):
            return self._parse_ndjson(raw, svc)

        log.warning(f"[Splunk] {svc}: unexpected response type {type(raw)}")
        return []

    def _parse_ndjson(self, text: str, svc: str) -> list[dict]:
        """Parse newline-delimited JSON (Splunk export format)."""
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # Export format: {"preview":false,"result":{...}}
                if "result" in obj:
                    rows.append(obj["result"])
                elif isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                log.debug(f"[Splunk] Skipping non-JSON line for {svc}: {line[:60]}")
        log.debug(f"[Splunk] {svc}: parsed {len(rows)} rows from NDJSON")
        return rows

    # ── HTTP path — direct Splunk REST (standalone testing) ───────────

    async def _http_collect(
        self, namespace: str, days: int
    ) -> dict[str, list[tuple[str, float]]]:
        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())

        export_url = (
            f"{self.base_url}/servicesNS/{self.splunk_owner}"
            f"/{self.splunk_app}/search/jobs/export"
        )
        log.info(f"[Splunk] HTTP mode export URL: {export_url}")

        results = {}
        for svc in services:
            spl  = queries["api_calls"]["per_service_trend"][svc]
            rows = await self._http_export(spl, days, export_url)
            results[svc] = [
                (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                for row in rows
            ]
            log.info(f"[Splunk] {svc}: {len(results[svc])} data points via HTTP")
        return results

    async def _http_export(self, spl: str, days: int, export_url: str) -> list[dict]:
        """Synchronous Splunk export — single POST, streams results."""
        # HTTP mode DOES need the 'search' keyword
        search_query = spl.strip()
        if not search_query.lower().startswith("search "):
            search_query = f"search {search_query}"

        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type":  "application/x-www-form-urlencoded"}

        log.debug(f"[Splunk] HTTP export: {search_query[:100]}  earliest=-{days}d")

        try:
            async with httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
            ) as client:
                r = await client.post(
                    export_url,
                    headers=headers,
                    data={
                        "search":        search_query,
                        "earliest_time": f"-{days}d",
                        "latest_time":   "now",
                        "output_mode":   "json",
                        "search_mode":   "normal",
                    },
                )
                if r.status_code == 401:
                    log.error("[Splunk] 401 Unauthorized — check SPLUNK_TOKEN")
                    return []
                if r.status_code == 403:
                    log.error(f"[Splunk] 403 Forbidden — check token has search access in app '{self.splunk_app}'")
                    return []
                if r.status_code != 200:
                    log.error(f"[Splunk] HTTP {r.status_code}: {r.text[:200]}")
                    return []
                return self._parse_ndjson(r.text, "http-export")

        except httpx.ConnectError as e:
            log.error(f"[Splunk] Cannot connect to {self.base_url}: {e}")
            return []
        except httpx.TimeoutException:
            log.error(f"[Splunk] Export timed out after {self.timeout}s")
            return []
        except Exception as e:
            log.error(f"[Splunk] HTTP export failed: {type(e).__name__}: {e}")
            return []

    async def _run_search(self, spl: str, days: int) -> list[dict]:
        """Internal router — used by collect_namespace_trend."""
        if self.mode == "splunk_mcp":
            spl_clean = _strip_search_keyword(spl)
            try:
                raw = await self.mcp_client.call_tool(
                    server=self.mcp_server,
                    tool="run_spl_query",
                    args={"spl": spl_clean, "earliest": f"-{days}d", "latest": "now"},
                )
                return self._extract_rows(raw, "namespace_trend")
            except Exception as e:
                log.error(f"[Splunk] MCP namespace trend failed: {e}")
                return []
        export_url = (
            f"{self.base_url}/servicesNS/{self.splunk_owner}"
            f"/{self.splunk_app}/search/jobs/export"
        )
        return await self._http_export(spl, days, export_url)


# ── CLI smoke-test ──────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser(
        description="Splunk collector test — HTTP mode (no MCP needed)"
    )
    p.add_argument("--namespace", default=os.getenv("OCP_NAMESPACE", "alprc-prod"))
    p.add_argument("--days",      type=int, default=14)
    p.add_argument("--url",       default=os.getenv("SPLUNK_HOST",  "https://localhost:8089"))
    p.add_argument("--token",     default=os.getenv("SPLUNK_TOKEN", ""))
    p.add_argument("--app",       default=os.getenv("SPLUNK_APP",   "search"))
    p.add_argument("--owner",     default=os.getenv("SPLUNK_OWNER", "nobody"))
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--debug",     action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(name)-42s %(levelname)-8s  %(message)s",
    )

    c = SplunkCollector(
        base_url=args.url,
        token=args.token,
        splunk_app=args.app,
        splunk_owner=args.owner,
        verify_ssl=not args.no_verify,
        mode="http",
    )

    print(f"\nMode       : HTTP (direct REST — use for standalone testing)")
    print(f"Splunk URL : {c.base_url}")
    print(f"App        : {c.splunk_app}")
    print(f"Namespace  : {args.namespace}")
    print(f"Lookback   : {args.days} days\n")

    data = await c.collect_api_trends(args.namespace, args.days)
    for svc, series in data.items():
        print(f"{svc}: {len(series)} data points")
        for date, calls in series[-5:]:
            print(f"  {date}  {calls:>10.0f} calls")

if __name__ == "__main__":
    asyncio.run(_cli())
