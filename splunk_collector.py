"""
scripts/collectors/splunk_collector.py

Fetches daily API-call counts per service from Splunk.

SEARCH METHOD — synchronous export (streaming):
  POST /servicesNS/{owner}/{app}/search/jobs/export
  No polling loop needed. Splunk streams results line-by-line in JSON format.
  This is faster and simpler than the async job→poll→fetch pattern.

APP CONTEXT:
  Splunk searches run inside an app namespace. Pass your app ID via:
    splunk_app  constructor arg   (default: "search")
    SPLUNK_APP  environment var

  For example SPLUNK_APP=abcd

URL FORMAT:
  SPLUNK_HOST must be base URL only → https://xyz.com:8089
  The collector builds the full path:
    https://xyz:8089/servicesNS/{owner}/{app}/search/jobs/export

Two modes:
  "http"       — direct HTTP export call (default, used in CLI and standalone)
  "splunk_mcp" — calls the splunk MCP server VS Code started
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


# ── URL helper ─────────────────────────────────────────────────────────

def _base_url(raw: str) -> str:
    """
    Strip any path after host:port so the user can paste full Splunk URLs
    without breaking anything.
      https://host:8089/servicesNS/nobody/app/...  →  https://host:8089
    """
    raw    = raw.strip().rstrip("/")
    parsed = urlparse(raw)
    base   = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base = f"{base}:{parsed.port}"
    if base != raw:
        log.info(f"[Splunk] URL normalised: '{raw}' → '{base}'")
    return base


def _load_queries(namespace: str, lookback_days: int = 30) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(
        json.dumps(raw)
        .replace("{namespace}", namespace)
        .replace("{lookback_days}", str(lookback_days))
    )


# ── collector ──────────────────────────────────────────────────────────

class SplunkCollector:
    """
    Fetch daily API call counts per service via Splunk synchronous export.

    Key args:
        base_url    — https://host:8089  (path after port is stripped automatically)
        token       — Bearer token (preferred) or leave blank + use username/password
        splunk_app  — Splunk app ID that owns the search context  e.g. wf_ui_app_ctdlr
        splunk_owner— Splunk namespace owner (default: "nobody")

    Returns:
        { "service-name": [("2025-01-15", 12340.0), ...], ... }
    """

    def __init__(
        self,
        base_url:      str  = "https://localhost:8089",
        token:         str  = "",
        username:      str  = "",
        password:      str  = "",
        splunk_app:    str  = None,   # e.g. "abcd"
        splunk_owner:  str  = "nobody",
        mode:          str  = "http",
        mcp_client           = None,
        verify_ssl:    bool = False,
        timeout:       int  = 120,    # export can take longer than async job
    ):
        self.base_url     = _base_url(base_url)
        self.token        = token
        self.username     = username
        self.password     = password
        # App from constructor arg → env var → default "search"
        self.splunk_app   = splunk_app or os.getenv("SPLUNK_APP", "search")
        self.splunk_owner = splunk_owner
        self.mode         = mode
        self.mcp_client   = mcp_client
        self.verify_ssl   = verify_ssl
        self.timeout      = timeout

        # Build the export endpoint once
        self.export_url = (
            f"{self.base_url}/servicesNS/{self.splunk_owner}"
            f"/{self.splunk_app}/search/jobs/export"
        )
        log.info(f"[Splunk] export endpoint: {self.export_url}")

    # ── public ───────────────────────────────────────────────────────

    async def collect_api_trends(
        self, namespace: str, days: int = 30
    ) -> dict[str, list[tuple[str, float]]]:
        """Return per-service daily API call series."""
        if self.mode == "splunk_mcp":
            return await self._mcp_collect(namespace, days)
        return await self._http_collect(namespace, days)

    async def collect_namespace_trend(
        self, namespace: str, days: int = 30
    ) -> list[tuple[str, float]]:
        """Total API calls across all services per day (summed)."""
        queries = _load_queries(namespace, days)
        spl     = queries["api_calls"]["namespace_trend"]
        rows    = await self._export_search(spl, days)
        totals: dict[str, float] = {}
        for row in rows:
            date = row.get("_time", "")
            for k, v in row.items():
                if not k.startswith("_"):
                    totals[date] = totals.get(date, 0.0) + float(v or 0)
        return sorted(totals.items())

    # ── HTTP export backend ───────────────────────────────────────────

    async def _http_collect(
        self, namespace: str, days: int
    ) -> dict[str, list[tuple[str, float]]]:
        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())

        log.info(f"[Splunk] collect_api_trends: namespace={namespace}, days={days}, "
                 f"app={self.splunk_app}, mode=http-export")

        # Run all service searches concurrently
        tasks   = {
            svc: self._export_search(
                queries["api_calls"]["per_service_trend"][svc], days
            )
            for svc in services
        }
        results = {}
        for svc, coro in tasks.items():
            rows = await coro
            results[svc] = [
                (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                for row in rows
            ]
            log.info(f"[Splunk] {svc}: {len(results[svc])} data points")

        return results

    async def _export_search(self, spl: str, days: int) -> list[dict]:
        """
        Splunk synchronous export — streams results directly, no polling.

        Endpoint:  POST /servicesNS/{owner}/{app}/search/jobs/export
        Params:
            search          SPL query string
            earliest_time   relative time e.g. -30d
            latest_time     now
            output_mode     json
            search_mode     normal

        Response: newline-delimited JSON — each line is one result row.
        Last line is a preview/summary object which we skip.
        """
        search_query = spl.strip()
        if not search_query.startswith("search "):
            search_query = f"search {search_query}"

        log.debug(f"[Splunk] export: {search_query[:120]}  earliest=-{days}d")

        try:
            async with httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=httpx.Timeout(self.timeout, connect=15.0),
            ) as client:
                resp = await client.post(
                    self.export_url,
                    headers=self._headers(),
                    data={
                        "search":        search_query,
                        "earliest_time": f"-{days}d",
                        "latest_time":   "now",
                        "output_mode":   "json",
                        "search_mode":   "normal",
                    },
                )

                if resp.status_code == 401:
                    log.error("[Splunk] 401 Unauthorized — check SPLUNK_TOKEN is valid")
                    return []
                if resp.status_code == 403:
                    log.error(f"[Splunk] 403 Forbidden — token may not have read access "
                              f"to app '{self.splunk_app}'")
                    return []
                if resp.status_code == 404:
                    log.error(f"[Splunk] 404 Not Found — check SPLUNK_APP='{self.splunk_app}' "
                              f"exists and export endpoint is correct: {self.export_url}")
                    return []
                if resp.status_code not in (200, 201):
                    log.error(f"[Splunk] Unexpected {resp.status_code}: {resp.text[:300]}")
                    return []

                return self._parse_export_response(resp.text)

        except httpx.ConnectError as e:
            log.error(f"[Splunk] Cannot connect to {self.base_url} — {e}")
            return []
        except httpx.TimeoutException:
            log.error(f"[Splunk] Export timed out after {self.timeout}s — "
                      "try --days with a smaller window or increase timeout")
            return []
        except Exception as e:
            log.error(f"[Splunk] Export failed: {type(e).__name__}: {e}")
            return []

    def _parse_export_response(self, body: str) -> list[dict]:
        """
        Parse Splunk export JSON stream.

        Each line in the response is one of:
          {"preview":false,"offset":0,"result":{...}}   ← actual result row
          {"preview":false,"offset":0,"lastrow":true}   ← end sentinel (skip)

        We extract only the "result" dicts.
        """
        rows = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "result" in obj and not obj.get("lastrow"):
                    rows.append(obj["result"])
            except json.JSONDecodeError:
                log.debug(f"[Splunk] Skipping non-JSON line: {line[:80]}")

        log.debug(f"[Splunk] Parsed {len(rows)} result rows from export")
        return rows

    def _headers(self) -> dict:
        if self.token:
            return {
                "Authorization": f"Bearer {self.token}",
                "Content-Type":  "application/x-www-form-urlencoded",
            }
        import base64
        creds = base64.b64encode(
            f"{self.username}:{self.password}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        }

    # ── Splunk MCP backend ────────────────────────────────────────────

    async def _mcp_collect(
        self, namespace: str, days: int
    ) -> dict[str, list[tuple[str, float]]]:
        """Use Splunk MCP server (VS Code stdio) instead of direct HTTP."""
        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())
        results  = {}

        for svc in services:
            spl = queries["api_calls"]["per_service_trend"][svc]
            raw = await self.mcp_client.call_tool(
                server="splunk",
                tool="search",
                args={
                    "query":         spl,
                    "earliest_time": f"-{days}d",
                    "latest_time":   "now",
                },
            )
            rows = raw.get("results", [])
            results[svc] = [
                (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                for row in rows
            ]
        return results


# ── CLI ──────────────────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Splunk Collector — direct export test")
    p.add_argument("--namespace", default="alprc-prod")
    p.add_argument("--days",      type=int, default=14)
    p.add_argument("--url",       default=os.getenv("SPLUNK_HOST",  "https://localhost:8089"))
    p.add_argument("--token",     default=os.getenv("SPLUNK_TOKEN", ""))
    p.add_argument("--app",       default=os.getenv("SPLUNK_APP",   "search"))
    p.add_argument("--owner",     default="nobody")
    p.add_argument("--no-verify", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    c = SplunkCollector(
        base_url=args.url,
        token=args.token,
        splunk_app=args.app,
        splunk_owner=args.owner,
        verify_ssl=not args.no_verify,
    )

    print(f"\nExport URL : {c.export_url}")
    print(f"Namespace  : {args.namespace}")
    print(f"Lookback   : {args.days} days\n")

    data = await c.collect_api_trends(args.namespace, args.days)
    for svc, series in data.items():
        print(f"{svc}: {len(series)} data points")
        for date, calls in series[-5:]:
            print(f"  {date}  {calls:>10.0f} calls")

if __name__ == "__main__":
    asyncio.run(_cli())
