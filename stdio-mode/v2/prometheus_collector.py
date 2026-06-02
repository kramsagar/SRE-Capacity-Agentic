"""
scripts/collectors/prometheus_collector.py

Calls grafana-mcp tools to get Prometheus metrics.
That's it. No HTTP, no auth, no datasource discovery.
Your grafana-mcp/index.js handles all of that.
"""

import json
import logging
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT         = Path(__file__).parent.parent.parent
QUERIES_PATH = ROOT / "references" / "prometheus_queries.yaml"

log = logging.getLogger(__name__)


def _load_queries(namespace: str) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(json.dumps(raw).replace("{namespace}", namespace))


class PrometheusCollector:
    """
    Fetches CPU / memory / HPA metrics via grafana-mcp.

    Tools used from grafana-mcp (index.js):
      query_prometheus_range  →  time series data
      query_prometheus        →  single instant value

    mcp: MCPClient instance (already started)
    """

    def __init__(self, mcp):
        self.mcp = mcp

    async def collect_all(self, namespace: str, days: int = 30) -> dict:
        queries  = _load_queries(namespace)
        services = list(queries.get("microservices", {}).keys())
        log.info(f"[Prometheus] collect_all: namespace={namespace}  days={days}  services={services}")

        results = {}
        for svc in services:
            results[svc] = await self._collect_service(namespace, svc, queries, days)

        results["__namespace__"] = await self._collect_namespace(queries, days)
        return results

    async def _collect_service(self, namespace: str, svc: str, queries: dict, days: int) -> dict:
        q = queries["microservices"][svc]
        log.debug(f"[Prometheus] collecting {svc}")
        return {
            "cpu": {
                "usage":   await self._range(q["cpu"]["usage"],    days),
                "limit":   await self._instant(q["cpu"]["limit"]),
                "request": await self._instant(q["cpu"]["request"]),
            },
            "memory": {
                "usage":   await self._range(q["memory"]["usage"],   days),
                "limit":   await self._instant(q["memory"]["limit"]),
                "request": await self._instant(q["memory"]["request"]),
            },
            "hpa": {
                "current": await self._instant(q["hpa"]["current_replicas"]),
                "max":     await self._instant(q["hpa"]["max_replicas"]),
                "desired": await self._instant(q["hpa"]["desired_replicas"]),
            },
        }

    async def _collect_namespace(self, queries: dict, days: int) -> dict:
        ns = queries.get("namespace_totals", {})
        return {
            "cpu":    {"usage": await self._range(ns["cpu_usage"],    days),
                       "quota": await self._instant(ns["cpu_quota"])},
            "memory": {"usage": await self._range(ns["memory_usage"], days),
                       "quota": await self._instant(ns["memory_quota"])},
            "pods":   {"count": await self._instant(ns["pod_count"])},
        }

    # ── Tool calls ────────────────────────────────────────────────────

    async def _range(self, expr: str, days: int) -> list[tuple[float, float]]:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        step  = max(60, days * 86400 // 720)

        try:
            raw = await self.mcp.call(
                "grafana-mcp",
                "query_prometheus_range",
                {
                    "expr":  expr,
                    "start": start.isoformat(),
                    "end":   end.isoformat(),
                    "step":  str(step),
                },
            )
            return self._parse_range(raw, expr)
        except Exception as e:
            log.error(f"[Prometheus] range failed: {e}  expr={expr[:60]}")
            return []

    async def _instant(self, expr: str):
        try:
            raw = await self.mcp.call(
                "grafana-mcp",
                "query_prometheus",
                {"expr": expr},
            )
            return self._parse_instant(raw, expr)
        except Exception as e:
            log.error(f"[Prometheus] instant failed: {e}  expr={expr[:60]}")
            return None

    # ── Response parsers ──────────────────────────────────────────────

    def _parse_range(self, raw, expr: str) -> list[tuple[float, float]]:
        """Handle whatever format grafana-mcp returns for range queries."""
        if not raw:
            return []

        # Grafana data-frame format: {frames: [{data: {values: [[timestamps],[values]]}}]}
        if isinstance(raw, dict):
            frames = raw.get("frames") or raw.get("results", {}).get("A", {}).get("frames", [])
            if frames:
                try:
                    times = frames[0]["data"]["values"][0]   # epoch ms
                    vals  = frames[0]["data"]["values"][1]
                    pts   = [(float(t) / 1000, float(v)) for t, v in zip(times, vals)]
                    log.debug(f"[Prometheus] range: {len(pts)} points for {expr[:40]}")
                    return pts
                except Exception as e:
                    log.warning(f"[Prometheus] frame parse failed: {e}  raw={str(raw)[:200]}")

            # Prometheus native format: {data: {result: [{values: [[ts,v],...]}]}}
            result = raw.get("data", {}).get("result", [])
            if result:
                values = result[0].get("values", [])
                return [(float(ts), float(v)) for ts, v in values]

        log.warning(f"[Prometheus] unexpected range format: {str(raw)[:150]}")
        return []

    def _parse_instant(self, raw, expr: str):
        """Handle whatever format grafana-mcp returns for instant queries."""
        if not raw:
            return None

        if isinstance(raw, dict):
            # Grafana frame format
            frames = raw.get("frames") or raw.get("results", {}).get("A", {}).get("frames", [])
            if frames:
                try:
                    val = frames[0]["data"]["values"][1][0]
                    return float(val)
                except Exception:
                    pass

            # Prometheus native
            result = raw.get("data", {}).get("result", [])
            if result:
                try:
                    return float(result[0]["value"][1])
                except Exception:
                    pass

        if isinstance(raw, (int, float)):
            return float(raw)

        return None
