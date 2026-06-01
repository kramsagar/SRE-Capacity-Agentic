"""
scripts/collectors/splunk_collector.py

Fetches daily API-call counts per service from Splunk.

Two modes:
  "http"       — Splunk REST API direct  (standalone CLI)
  "splunk_mcp" — calls the splunk MCP server VS Code started.
                 mcp_client injected by capacity_agent.py.

Splunk MCP tool used: "search"
  docs: https://github.com/livehybrid/splunk-mcp
"""

import httpx
import yaml
import json
import asyncio
from pathlib import Path
from typing import Optional

ROOT         = Path(__file__).parent.parent.parent
QUERIES_PATH = ROOT / "references" / "splunk_queries.yaml"


def _load_queries(namespace: str, lookback_days: int = 30) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(
        json.dumps(raw)
        .replace("{namespace}", namespace)
        .replace("{lookback_days}", str(lookback_days))
    )


class SplunkCollector:
    """
    Fetch daily API call counts per service.

    Returns:
        { "payment-service": [("2025-01-15", 12340.0), ...], ... }
    """

    def __init__(
        self,
        base_url:   str = "https://localhost:8089",
        token:      str = "",
        username:   str = "",
        password:   str = "",
        mode:       str = "http",
        mcp_client=None,
        verify_ssl: bool = False,
        timeout:    int  = 60,
    ):
        self.base_url   = base_url.rstrip("/")
        self.token      = token
        self.username   = username
        self.password   = password
        self.mode       = mode
        self.mcp_client = mcp_client
        self.verify_ssl = verify_ssl
        self.timeout    = timeout

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
        """Total API calls across all services per day."""
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

    # ── Splunk MCP backend ────────────────────────────────────────────
    # Tool: "search"  args: { query, earliest_time, latest_time }
    # Returns: { results: [ {_time, ..., count}, ... ] }

    async def _mcp_collect(
        self, namespace: str, days: int
    ) -> dict[str, list[tuple[str, float]]]:
        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())
        results  = {}

        for svc in services:
            spl = queries["api_calls"]["per_service_trend"][svc]
            raw = await self.mcp_client.call_tool(
                server="splunk",
                tool="search",
                args={
                    "query":          f"search {spl}",
                    "earliest_time":  f"-{days}d",
                    "latest_time":    "now",
                },
            )
            rows = raw.get("results", [])
            results[svc] = [
                (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                for row in rows
            ]
        return results

    # ── HTTP backend ──────────────────────────────────────────────────

    async def _http_collect(
        self, namespace: str, days: int
    ) -> dict[str, list[tuple[str, float]]]:
        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())
        results  = {}
        for svc in services:
            spl  = queries["api_calls"]["per_service_trend"][svc]
            rows = await self._run_search(spl, days)
            results[svc] = [
                (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                for row in rows
            ]
        return results

    async def _run_search(self, spl: str, days: int) -> list[dict]:
        """Splunk REST: create job → poll → fetch results."""
        headers = self._headers()
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout) as client:
            # Create job
            resp = await client.post(
                f"{self.base_url}/services/search/jobs",
                headers=headers,
                data={
                    "search":       f"search {spl}",
                    "earliest_time": f"-{days}d",
                    "latest_time":   "now",
                    "output_mode":   "json",
                },
            )
            resp.raise_for_status()
            sid = resp.json()["sid"]

            # Poll until done
            for _ in range(30):
                await asyncio.sleep(2)
                st = await client.get(
                    f"{self.base_url}/services/search/jobs/{sid}",
                    headers=headers, params={"output_mode": "json"},
                )
                state = st.json()["entry"][0]["content"]["dispatchState"]
                if state == "DONE":
                    break
                if state in ("FAILED", "ZOMBIE"):
                    return []

            # Fetch results
            res = await client.get(
                f"{self.base_url}/services/search/jobs/{sid}/results",
                headers=headers, params={"output_mode": "json", "count": 0},
            )
            res.raise_for_status()
            return res.json().get("results", [])

    def _headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/x-www-form-urlencoded"}
        import base64
        creds = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded"}


# ── CLI ─────────────────────────────────────────────────────────────────

async def _cli():
    import argparse, os
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="payments-prod")
    p.add_argument("--days",  type=int, default=14)
    p.add_argument("--url",   default=os.getenv("SPLUNK_HOST", "https://localhost:8089"))
    p.add_argument("--token", default=os.getenv("SPLUNK_TOKEN", ""))
    args = p.parse_args()

    c = SplunkCollector(base_url=args.url, token=args.token, mode="http")
    data = await c.collect_api_trends(args.namespace, args.days)
    for svc, series in data.items():
        print(f"{svc}: {len(series)} days")
        for date, calls in series[-3:]:
            print(f"  {date}: {calls:.0f} calls")

if __name__ == "__main__":
    asyncio.run(_cli())
