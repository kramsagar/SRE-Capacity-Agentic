"""
scripts/collectors/prometheus_collector.py

Fetches CPU / Memory / HPA time-series from Grafana → Prometheus.

KEY FACTS ABOUT GRAFANA API:
  - Grafana does NOT need you to specify a datasource ID for running PromQL.
  - Use the unified query API:  POST /api/ds/query
    Body: { "queries": [{ "refId":"A", "datasource":{"type":"prometheus"}, "expr":"..." }] }
  - This automatically uses the default Prometheus datasource.
  - Alternatively, use the direct datasource proxy:
    GET /api/datasources/proxy/uid/{uid}/api/v1/query_range
    where uid is fetched automatically via GET /api/datasources

  GRAFANA_URL = base URL only, 
  Auth header: Authorization: Bearer glsa_...
"""

import httpx
import yaml
import json
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT         = Path(__file__).parent.parent.parent
QUERIES_PATH = ROOT / "references" / "prometheus_queries.yaml"

log = logging.getLogger(__name__)


# ── URL sanitiser (shared pattern) ────────────────────────────────────

def _base_url(raw: str) -> str:
    raw    = raw.strip().rstrip("/")
    parsed = urlparse(raw)
    base   = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base = f"{base}:{parsed.port}"
    if base != raw:
        log.warning(f"[Grafana] URL had path after host — stripped: '{raw}' → '{base}'")
    return base


def _load_queries(namespace: str) -> dict:
    raw = yaml.safe_load(QUERIES_PATH.read_text())
    return json.loads(json.dumps(raw).replace("{namespace}", namespace))


def _parse_series(result: list) -> list[tuple[float, float]]:
    if not result:
        return []
    values = result[0].get("values", [])
    return [(float(ts), float(v)) for ts, v in values]


def _parse_scalar(result: list) -> Optional[float]:
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# PrometheusCollector
# ══════════════════════════════════════════════════════════════════════

class PrometheusCollector:
    """
    Fetches metrics from Grafana → Prometheus.

    Query strategy (auto-selected):
      1. Grafana unified DS query API  →  POST /api/ds/query
         Works without knowing the datasource ID or UID.
         Used by default when token is provided.

      2. Grafana datasource proxy  →  /api/datasources/proxy/uid/{uid}/api/v1/...
         Used when GRAFANA_DS_UID env var is set, or after auto-discovery.

      3. Direct Prometheus  →  /api/v1/query_range
         Used when PROMETHEUS_DIRECT=true (GRAFANA_URL points to Prometheus, not Grafana).

    Args:
        base_url  : Grafana base URL — path after host:port is stripped automatically
        token     : Grafana service account token (glsa_...)
        ds_uid    : Datasource UID (optional — auto-discovered if blank)
        mode      : "http" | "grafana_mcp"
    """

    def __init__(
        self,
        base_url:  str = "http://localhost:3000",
        token:     str = "",
        ds_uid:    str = None,
        mode:      str = "http",
        mcp_client     = None,
        timeout:   int = 30,
    ):
        self.base_url   = _base_url(base_url)
        self.token      = token or os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
        self.ds_uid     = ds_uid or os.getenv("GRAFANA_DS_UID", "")
        self.mode       = mode
        self.mcp_client = mcp_client
        self.timeout    = timeout

        self._direct    = os.getenv("PROMETHEUS_DIRECT", "false").lower() == "true"

        log.info(f"[Grafana] base_url={self.base_url}  ds_uid={self.ds_uid or '(auto-discover)'}  "
                 f"direct={self._direct}  token={'set' if self.token else 'NOT SET'}")

    # ── public API ────────────────────────────────────────────────────

    async def collect_all(self, namespace: str, lookback_days: int = 30) -> dict:
        queries  = _load_queries(namespace)
        services = [k for k in queries.get("microservices", {})]
        log.info(f"[Grafana] collect_all: namespace={namespace}  days={lookback_days}  "
                 f"services={services}")
        results = {}
        for svc in services:
            results[svc] = await self.collect_service(namespace, svc, lookback_days)
        results["__namespace__"] = await self._collect_ns_totals(
            namespace, queries, lookback_days
        )
        log.info(f"[Grafana] collect_all done: {len(services)} services collected")
        return results

    async def collect_service(self, namespace: str, service: str, days: int = 30) -> dict:
        log.debug(f"[Grafana] collect_service: {service}  days={days}")
        q = _load_queries(namespace)["microservices"].get(service, {})
        result = {
            "cpu": {
                "usage":   await self._range(q["cpu"]["usage"],   days),
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
        cpu_pts = len(result["cpu"]["usage"])
        mem_pts = len(result["memory"]["usage"])
        log.info(f"[Grafana]   {service}: cpu_pts={cpu_pts}  mem_pts={mem_pts}  "
                 f"cpu_limit={result['cpu']['limit']}  mem_limit={result['memory']['limit']}")
        return result

    # ── routing ───────────────────────────────────────────────────────

    async def _range(self, query: str, days: int) -> list[tuple[float, float]]:
        if self.mode == "grafana_mcp":
            return await self._mcp_range(query, days)
        return await self._http_range(query, days)

    async def _instant(self, query: str) -> Optional[float]:
        if self.mode == "grafana_mcp":
            return await self._mcp_instant(query)
        return await self._http_instant(query)

    # ── headers ───────────────────────────────────────────────────────

    def _headers(self, content_type: str = "application/json") -> dict:
        h = {"Content-Type": content_type, "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    # ── HTTP: range query ──────────────────────────────────────────────

    async def _http_range(self, query: str, days: int) -> list[tuple[float, float]]:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        step  = max(60, days * 86400 // 720)

        log.debug(
            f"[Grafana] RANGE  query={query[:80]}...  "
            f"start={start.strftime('%Y-%m-%dT%H:%M')}  step={step}s"
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:

                # ── Strategy A: direct Prometheus ──────────────────────
                if self._direct:
                    url = f"{self.base_url}/api/v1/query_range"
                    log.debug(f"[Grafana] Strategy=direct-prometheus  url={url}")
                    r = await client.get(
                        url, headers=self._headers(),
                        params={"query": query,
                                "start": start.timestamp(),
                                "end":   end.timestamp(),
                                "step":  step},
                    )
                    return self._handle_prom_range_response(r, query)

                # ── Strategy B: Grafana unified DS query API ───────────
                # POST /api/ds/query  — works without datasource ID
                url  = f"{self.base_url}/api/ds/query"
                body = {
                    "from":  str(int(start.timestamp() * 1000)),
                    "to":    str(int(end.timestamp()   * 1000)),
                    "queries": [{
                        "refId":      "A",
                        "datasource": {"type": "prometheus"},
                        "expr":       query,
                        "range":      True,
                        "instant":    False,
                        "intervalMs": step * 1000,
                        "maxDataPoints": 720,
                    }],
                }
                log.debug(f"[Grafana] Strategy=unified-ds-query  url={url}")
                r = await client.post(url, headers=self._headers(), json=body)

                if r.status_code == 200:
                    return self._parse_ds_query_range(r.json(), query)

                # ── Strategy C: datasource proxy (fallback) ────────────
                if r.status_code in (404, 403) and not self.ds_uid:
                    log.debug(
                        f"[Grafana] Unified API returned {r.status_code} — "
                        "attempting datasource proxy after auto-discovery"
                    )
                    self.ds_uid = await self._discover_ds_uid(client)

                if self.ds_uid:
                    proxy_url = (
                        f"{self.base_url}/api/datasources/proxy/uid"
                        f"/{self.ds_uid}/api/v1/query_range"
                    )
                    log.debug(f"[Grafana] Strategy=datasource-proxy  url={proxy_url}")
                    r2 = await client.get(
                        proxy_url, headers=self._headers(),
                        params={"query": query,
                                "start": start.timestamp(),
                                "end":   end.timestamp(),
                                "step":  step},
                    )
                    return self._handle_prom_range_response(r2, query)

                # All strategies failed
                self._log_response_error(r, "range query")
                return []

        except httpx.InvalidURL as e:
            log.error(f"[Grafana] InvalidURL '{self.base_url}': {e}")
            log.error("  → GRAFANA_URL must be base URL only: https://host[:port]")
            return []
        except httpx.ConnectError as e:
            log.error(f"[Grafana] Cannot connect to {self.base_url}: {e}")
            return []
        except Exception as e:
            log.error(f"[Grafana] range query failed: {type(e).__name__}: {e}", exc_info=True)
            return []

    # ── HTTP: instant query ────────────────────────────────────────────

    async def _http_instant(self, query: str) -> Optional[float]:
        log.debug(f"[Grafana] INSTANT  query={query[:80]}...")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:

                if self._direct:
                    url = f"{self.base_url}/api/v1/query"
                    r = await client.get(
                        url, headers=self._headers(), params={"query": query}
                    )
                    return self._handle_prom_instant_response(r, query)

                # Unified DS query (instant)
                url  = f"{self.base_url}/api/ds/query"
                body = {
                    "queries": [{
                        "refId":      "A",
                        "datasource": {"type": "prometheus"},
                        "expr":       query,
                        "instant":    True,
                        "range":      False,
                    }],
                }
                r = await client.post(url, headers=self._headers(), json=body)

                if r.status_code == 200:
                    return self._parse_ds_query_instant(r.json(), query)

                if r.status_code in (404, 403) and not self.ds_uid:
                    self.ds_uid = await self._discover_ds_uid(client)

                if self.ds_uid:
                    proxy_url = (
                        f"{self.base_url}/api/datasources/proxy/uid"
                        f"/{self.ds_uid}/api/v1/query"
                    )
                    r2 = await client.get(
                        proxy_url, headers=self._headers(), params={"query": query}
                    )
                    return self._handle_prom_instant_response(r2, query)

                self._log_response_error(r, "instant query")
                return None

        except httpx.InvalidURL as e:
            log.error(f"[Grafana] InvalidURL: {e}")
            return None
        except Exception as e:
            log.error(f"[Grafana] instant query failed: {type(e).__name__}: {e}")
            return None

    # ── datasource auto-discovery ──────────────────────────────────────

    async def _discover_ds_uid(self, client: httpx.AsyncClient) -> str:
        """
        GET /api/datasources — find the first Prometheus datasource UID.
        Caches the result in self.ds_uid so discovery only runs once.
        """
        log.info("[Grafana] Auto-discovering Prometheus datasource UID...")
        try:
            r = await client.get(
                f"{self.base_url}/api/datasources",
                headers=self._headers(),
            )
            if r.status_code != 200:
                log.warning(f"[Grafana] /api/datasources returned {r.status_code}")
                return ""
            datasources = r.json()
            log.debug(f"[Grafana] Found {len(datasources)} datasources: "
                      f"{[ds.get('name') for ds in datasources]}")
            for ds in datasources:
                if ds.get("type") == "prometheus":
                    uid  = ds.get("uid", "")
                    name = ds.get("name", "")
                    log.info(f"[Grafana] Using Prometheus datasource: name='{name}'  uid='{uid}'")
                    return uid
            log.warning("[Grafana] No Prometheus datasource found in Grafana")
            return ""
        except Exception as e:
            log.error(f"[Grafana] Datasource discovery failed: {e}")
            return ""

    # ── response parsers ──────────────────────────────────────────────

    def _parse_ds_query_range(
        self, body: dict, query: str
    ) -> list[tuple[float, float]]:
        """Parse Grafana /api/ds/query response for a range query."""
        try:
            results = body.get("results", {}).get("A", {})
            frames  = results.get("frames", [])
            if not frames:
                log.debug(f"[Grafana] /api/ds/query returned 0 frames for: {query[:60]}")
                return []
            times = frames[0]["data"]["values"][0]   # epoch ms
            vals  = frames[0]["data"]["values"][1]
            pts   = [(float(t) / 1000, float(v)) for t, v in zip(times, vals)]
            log.debug(f"[Grafana] Range result: {len(pts)} points")
            return pts
        except Exception as e:
            log.error(f"[Grafana] Failed to parse ds/query range response: {e}")
            log.debug(f"[Grafana] Raw body: {str(body)[:400]}")
            return []

    def _parse_ds_query_instant(self, body: dict, query: str) -> Optional[float]:
        """Parse Grafana /api/ds/query response for an instant query."""
        try:
            results = body.get("results", {}).get("A", {})
            frames  = results.get("frames", [])
            if not frames:
                return None
            val = frames[0]["data"]["values"][1][0]
            log.debug(f"[Grafana] Instant result: {val}")
            return float(val)
        except Exception as e:
            log.error(f"[Grafana] Failed to parse ds/query instant response: {e}")
            return None

    def _handle_prom_range_response(
        self, r: httpx.Response, query: str
    ) -> list[tuple[float, float]]:
        if r.status_code == 401:
            log.error("[Grafana] 401 Unauthorized — GRAFANA_SERVICE_ACCOUNT_TOKEN missing or expired")
            return []
        if r.status_code == 400:
            log.error(f"[Grafana] 400 Bad Request (PromQL error?) — {r.text[:300]}")
            return []
        if r.status_code != 200:
            self._log_response_error(r, "range")
            return []
        data = r.json().get("data", {})
        result = data.get("result", [])
        pts = _parse_series(result)
        log.debug(f"[Grafana] Proxy range result: {len(pts)} points")
        return pts

    def _handle_prom_instant_response(
        self, r: httpx.Response, query: str
    ) -> Optional[float]:
        if r.status_code != 200:
            self._log_response_error(r, "instant")
            return None
        return _parse_scalar(r.json().get("data", {}).get("result", []))

    def _log_response_error(self, r: httpx.Response, context: str):
        log.error(
            f"[Grafana] {context} HTTP {r.status_code}  url={r.url}\n"
            f"  body={r.text[:300]}"
        )

    # ── Grafana MCP backend ───────────────────────────────────────────

    async def _mcp_range(self, query: str, days: int) -> list[tuple[float, float]]:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        step  = max(60, days * 86400 // 720)
        log.debug(f"[Grafana-MCP] range: {query[:60]}")
        result = await self.mcp_client.call_tool(
            server="grafana", tool="query_prometheus_range",
            args={"expr": query, "start": start.isoformat(),
                  "end": end.isoformat(), "step": str(step)},
        )
        try:
            frames = result.get("frames", [])
            if not frames:
                return []
            times = frames[0]["data"]["values"][0]
            vals  = frames[0]["data"]["values"][1]
            return [(float(t) / 1000, float(v)) for t, v in zip(times, vals)]
        except Exception as e:
            log.error(f"[Grafana-MCP] Failed to parse range response: {e}")
            return []

    async def _mcp_instant(self, query: str) -> Optional[float]:
        log.debug(f"[Grafana-MCP] instant: {query[:60]}")
        result = await self.mcp_client.call_tool(
            server="grafana", tool="query_prometheus",
            args={"expr": query},
        )
        try:
            frames = result.get("frames", [])
            return float(frames[0]["data"]["values"][1][0]) if frames else None
        except Exception as e:
            log.error(f"[Grafana-MCP] Failed to parse instant response: {e}")
            return None

    # ── namespace totals ──────────────────────────────────────────────

    async def _collect_ns_totals(self, namespace: str, queries: dict, days: int) -> dict:
        ns = queries.get("namespace_totals", {})
        return {
            "cpu":    {"usage":      await self._range(ns["cpu_usage"], days),
                       "quota":      await self._instant(ns["cpu_quota"]),
                       "used_quota": await self._instant(ns["cpu_used_quota"])},
            "memory": {"usage":      await self._range(ns["memory_usage"], days),
                       "quota":      await self._instant(ns["memory_quota"]),
                       "used_quota": await self._instant(ns["memory_used_quota"])},
            "pods":   {"count": await self._instant(ns["pod_count"]),
                       "quota": await self._instant(ns["pod_quota"])},
        }


# ── CLI ──────────────────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Grafana/Prometheus collector test")
    p.add_argument("--namespace", default="alprc-prod")
    p.add_argument("--service",   default=None)
    p.add_argument("--days",      type=int, default=7)
    p.add_argument("--url",       default=os.getenv("GRAFANA_URL",                  "http://localhost:3000"))
    p.add_argument("--token",     default=os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN",""))
    p.add_argument("--ds-uid",    default=os.getenv("GRAFANA_DS_UID",               ""))
    p.add_argument("--direct",    action="store_true", help="GRAFANA_URL points to Prometheus, not Grafana")
    p.add_argument("--debug",     action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    if args.direct:
        os.environ["PROMETHEUS_DIRECT"] = "true"

    c = PrometheusCollector(base_url=args.url, token=args.token, ds_uid=args.ds_uid)

    print(f"\nGrafana URL : {c.base_url}")
    print(f"DS UID      : {c.ds_uid or '(auto-discover on first query)'}")
    print(f"Direct Prom : {c._direct}")
    print(f"Token       : {'set (' + c.token[:8] + '...)' if c.token else 'NOT SET'}\n")

    if args.service:
        data = await c.collect_service(args.namespace, args.service, args.days)
        print(f"{args.service}: CPU={len(data['cpu']['usage'])} pts  "
              f"Mem={len(data['memory']['usage'])} pts")
    else:
        data = await c.collect_all(args.namespace, args.days)
        for svc, v in data.items():
            if svc == "__namespace__":
                continue
            print(f"  {svc:<40} CPU={len(v['cpu']['usage']):>4}pts  "
                  f"Mem={len(v['memory']['usage']):>4}pts")

if __name__ == "__main__":
    asyncio.run(_cli())
