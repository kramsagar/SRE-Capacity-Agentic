"""
scripts/collectors/splunk_collector.py

Calls splunk-mcp tool: run_spl_query
That's it. Your splunk-mcp/server.py handles auth, app context, everything.
"""

import json
import logging
import yaml
from pathlib import Path

ROOT         = Path(__file__).parent.parent.parent
QUERIES_PATH = ROOT / "references" / "splunk_queries.yaml"

log = logging.getLogger(__name__)


def _load_queries(namespace: str, days: int) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(
        json.dumps(raw)
        .replace("{namespace}", namespace)
        .replace("{lookback_days}", str(days))
    )


def _strip_search(spl: str) -> str:
    """run_spl_query does NOT want the leading 'search' keyword."""
    spl = spl.strip()
    return spl[7:].strip() if spl.lower().startswith("search ") else spl


class SplunkCollector:
    """
    Fetches API call trends via splunk-mcp.

    Tool used: run_spl_query
      args: { spl: "...", earliest: "-30d", latest: "now" }

    mcp: MCPClient instance (already started)
    """

    def __init__(self, mcp):
        self.mcp = mcp

    async def collect_api_trends(
        self, namespace: str, days: int = 30
    ) -> dict[str, list[tuple[str, float]]]:

        queries  = _load_queries(namespace, days)
        services = list(queries["api_calls"]["per_service_trend"].keys())
        log.info(f"[Splunk] collect_api_trends: namespace={namespace}  days={days}  services={services}")

        results = {}
        for svc in services:
            raw_spl = queries["api_calls"]["per_service_trend"][svc]
            spl     = _strip_search(raw_spl)

            log.debug(f"[Splunk] run_spl_query: {svc}  spl={spl[:80]}")
            try:
                raw = await self.mcp.call(
                    "splunk-mcp",
                    "run_spl_query",
                    {
                        "spl":      spl,
                        "earliest": f"-{days}d",
                        "latest":   "now",
                    },
                )
                rows = self._extract_rows(raw)
                results[svc] = [
                    (row.get("_time", ""), float(row.get("api_calls", 0) or 0))
                    for row in rows
                ]
                log.info(f"[Splunk] {svc}: {len(results[svc])} data points")

            except Exception as e:
                log.error(f"[Splunk] run_spl_query failed for {svc}: {e}")
                results[svc] = []

        return results

    def _extract_rows(self, raw) -> list[dict]:
        """Handle whatever your splunk-mcp returns."""
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("results", "rows", "data"):
                if key in raw:
                    return raw[key]
        if isinstance(raw, str):
            # Newline-delimited JSON (Splunk export format)
            rows = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    rows.append(obj.get("result", obj) if "result" in obj else obj)
                except json.JSONDecodeError:
                    pass
            return rows
        return []
