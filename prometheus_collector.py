"""
scripts/collectors/prometheus_collector.py

Fetches CPU / Memory / HPA time-series from Prometheus.

Two modes — controlled by the 'mode' argument:
  "http"        — direct REST call to Prometheus (for standalone CLI use)
  "grafana_mcp" — calls the grafana MCP server that VS Code already started.
                  In this mode the collector calls mcp_client.call_tool()
                  which is injected by capacity_agent.py at runtime.

When running inside VS Code Copilot agent the capacity_agent.py passes
an mcp_client object so this file never spawns its own subprocess.
"""

import httpx
import yaml
import json
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

ROOT = Path(__file__).parent.parent.parent
QUERIES_PATH = ROOT / "references" / "prometheus_queries.yaml"


# ── helpers ────────────────────────────────────────────────────────────

def _load_queries(namespace: str) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(json.dumps(raw).replace("{namespace}", namespace))


def _parse_series(result: list) -> list[tuple[float, float]]:
    """Prometheus range result → [(unix_ts, value), ...]"""
    if not result:
        return []
    values = result[0].get("values", [])
    return [(float(ts), float(v)) for ts, v in values]


def _parse_scalar(result: list) -> Optional[float]:
    """Prometheus instant result → single float"""
    if not result:
        return None
    return float(result[0]["value"][1])


# ── main class ─────────────────────────────────────────────────────────

class PrometheusCollector:
    """
    Collect CPU, memory and HPA metrics for every service in a namespace.

    Args:
        base_url:   Prometheus URL  (http mode only)
        mode:       "http" | "grafana_mcp"
        mcp_client: injected by capacity_agent when mode="grafana_mcp"
                    must expose  await mcp_client.call_tool(server, tool, args)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9090",
        mode: str = "http",
        mcp_client=None,
        timeout: int = 30,
    ):
        self.base_url   = base_url.rstrip("/")
        self.mode       = mode
        self.mcp_client = mcp_client
        self.timeout    = timeout

    # ── public ───────────────────────────────────────────────────────

    async def collect_all(self, namespace: str, lookback_days: int = 30) -> dict:
        """Return metrics for every service + namespace totals."""
        queries  = _load_queries(namespace)
        services = [k for k in queries.get("microservices", {})]
        results  = {}
        for svc in services:
            results[svc] = await self.collect_service(namespace, svc, lookback_days)
        results["__namespace__"] = await self._collect_ns_totals(namespace, queries, lookback_days)
        return results

    async def collect_service(self, namespace: str, service: str, days: int = 30) -> dict:
        q = _load_queries(namespace)["microservices"].get(service, {})
        return {
            "cpu": {
                "usage":   await self._range(q["cpu"]["usage"], days),
                "limit":   await self._instant(q["cpu"]["limit"]),
                "request": await self._instant(q["cpu"]["request"]),
            },
            "memory": {
                "usage":   await self._range(q["memory"]["usage"], days),
                "limit":   await self._instant(q["memory"]["limit"]),
                "request": await self._instant(q["memory"]["request"]),
            },
            "hpa": {
                "current": await self._instant(q["hpa"]["current_replicas"]),
                "max":     await self._instant(q["hpa"]["max_replicas"]),
                "desired": await self._instant(q["hpa"]["desired_replicas"]),
            },
        }

    # ── private: route to correct backend ────────────────────────────

    async def _range(self, query: str, days: int) -> list[tuple[float, float]]:
        if self.mode == "grafana_mcp":
            return await self._mcp_range(query, days)
        return await self._http_range(query, days)

    async def _instant(self, query: str) -> Optional[float]:
        if self.mode == "grafana_mcp":
            return await self._mcp_instant(query)
        return await self._http_instant(query)

    # ── HTTP backend ──────────────────────────────────────────────────

    async def _http_range(self, query: str, days: int) -> list[tuple[float, float]]:
        end   = datetime.now()
        start = end - timedelta(days=days)
        step  = max(60, days * 86400 // 720)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{self.base_url}/api/v1/query_range",
                params={"query": query, "start": start.timestamp(),
                        "end": end.timestamp(), "step": step},
            )
            r.raise_for_status()
            return _parse_series(r.json()["data"]["result"])

    async def _http_instant(self, query: str) -> Optional[float]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/api/v1/query",
                                 params={"query": query})
            r.raise_for_status()
            return _parse_scalar(r.json()["data"]["result"])

    # ── Grafana MCP backend ───────────────────────────────────────────
    # The MCP server exposes two relevant tools:
    #   query_prometheus       → instant value
    #   query_prometheus_range → time series
    # Full tool list: https://github.com/0xteamhq/mcp-grafana#tools

    async def _mcp_range(self, query: str, days: int) -> list[tuple[float, float]]:
        end   = datetime.now()
        start = end - timedelta(days=days)
        step  = max(60, days * 86400 // 720)

        result = await self.mcp_client.call_tool(
            server="grafana",
            tool="query_prometheus_range",
            args={
                "expr":  query,
                "start": start.isoformat(),
                "end":   end.isoformat(),
                "step":  str(step),
            },
        )
        # Grafana MCP returns Grafana data-frame format
        try:
            frames = result.get("frames", [])
            if not frames:
                return []
            times = frames[0]["data"]["values"][0]   # epoch ms
            vals  = frames[0]["data"]["values"][1]
            return [(float(t) / 1000, float(v)) for t, v in zip(times, vals)]
        except Exception:
            return []

    async def _mcp_instant(self, query: str) -> Optional[float]:
        result = await self.mcp_client.call_tool(
            server="grafana",
            tool="query_prometheus",
            args={"expr": query},
        )
        try:
            frames = result.get("frames", [])
            if not frames:
                return None
            return float(frames[0]["data"]["values"][1][0])
        except Exception:
            return None

    # ── namespace totals ──────────────────────────────────────────────

    async def _collect_ns_totals(self, namespace: str, queries: dict, days: int) -> dict:
        ns = queries.get("namespace_totals", {})
        return {
            "cpu": {
                "usage":      await self._range(ns["cpu_usage"], days),
                "quota":      await self._instant(ns["cpu_quota"]),
                "used_quota": await self._instant(ns["cpu_used_quota"]),
            },
            "memory": {
                "usage":      await self._range(ns["memory_usage"], days),
                "quota":      await self._instant(ns["memory_quota"]),
                "used_quota": await self._instant(ns["memory_used_quota"]),
            },
            "pods": {
                "count": await self._instant(ns["pod_count"]),
                "quota": await self._instant(ns["pod_quota"]),
            },
        }


# ── CLI smoke-test ──────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="payments-prod")
    p.add_argument("--service",   default=None)
    p.add_argument("--days",      type=int, default=7)
    p.add_argument("--url",       default="http://localhost:9090")
    args = p.parse_args()

    c = PrometheusCollector(base_url=args.url, mode="http")
    if args.service:
        d = await c.collect_service(args.namespace, args.service, args.days)
    else:
        d = await c.collect_all(args.namespace, args.days)

    for k, v in d.items():
        cpu_pts = len(v.get("cpu", {}).get("usage", [])) if isinstance(v, dict) else 0
        print(f"  {k}: {cpu_pts} CPU data points")

if __name__ == "__main__":
    asyncio.run(_cli())
