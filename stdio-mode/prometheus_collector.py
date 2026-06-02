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

  GRAFANA_URL = base URL only, e.g. https://prod1-grafana.wellsfargo.net
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

    Auth (mirrors your grafana-mcp/index.js auth priority exactly):
      1. Bearer token  → GRAFANA_TOKEN env var  (service account glsa_...)
      2. Basic auth    → GRAFANA_USERNAME + GRAFANA_PASSWORD env vars
      3. None          → anonymous (Grafana allows if org permits)

    Query strategy (auto-selected):
      1. Grafana unified DS query API  →  POST /api/ds/query
         Works without knowing the datasource ID or UID.
         This is what your grafana-mcp uses internally via axios.

      2. Grafana datasource proxy  →  /api/datasources/proxy/uid/{uid}/api/v1/...
         Fallback if unified API returns 404.
         UID auto-discovered via GET /api/datasources.

      3. Direct Prometheus  →  /api/v1/query_range
         Used when PROMETHEUS_DIRECT=true (GRAFANA_URL points to Prometheus directly).

    Args:
        base_url  : Grafana base URL — same as GRAFANA_URL in your grafana-mcp
                    e.g. https://prod1-grafana.wellsfargo.net
                    Path after host:port stripped automatically.
        token     : Bearer token  (GRAFANA_TOKEN env var)
        username  : Basic auth username  (GRAFANA_USERNAME env var)
        password  : Basic auth password  (GRAFANA_PASSWORD env var)
        ds_uid    : Datasource UID — auto-discovered if blank
        mode      : "http" (direct REST) | "grafana_mcp" (via your MCP server)
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
        self.base_url = _base_url(base_url)
        self.token    = token \
                     or os.getenv("GRAFANA_TOKEN", "") \
                     or os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
        self.ds_uid            = ds_uid or os.getenv("GRAFANA_DS_UID", "")
        self._ds_uid_validated = False
        self.mode       = mode
        self.mcp_client = mcp_client
        self.timeout    = timeout
        self._direct    = os.getenv("PROMETHEUS_DIRECT", "false").lower() == "true"

        log.info(
            f"[Grafana] base_url={self.base_url}  "
            f"token={'set (' + self.token[:8] + '...)' if self.token else 'NOT SET'}  "
            f"ds_uid={self.ds_uid or '(will auto-discover)'}"
        )

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
        """Bearer token auth only — Grafana service account token."""
        h = {"Content-Type": content_type, "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        else:
            log.warning("[Grafana] No token set — request will be anonymous")
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
            async with httpx.AsyncClient(
                timeout=self.timeout,
                verify=False,   # mirrors grafana-mcp TLS_REJECT_UNAUTHORIZED=false default
            ) as client:

                # ── Strategy A: direct Prometheus ─────────────────────────
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

                # ── Ensure we have the correct validated datasource UID ────
                # Always re-discover on first query — env var GRAFANA_DS_UID
                # may be stale or wrong (e.g. 'hyaV9HiVz' vs the real UUID).
                # _discover_ds_uid() always fetches from /api/datasources and
                # warns if the env var value differs from the real one.
                if not self._ds_uid_validated:
                    self.ds_uid = await self._discover_ds_uid(client)
                    self._ds_uid_validated = True

                # ── Strategy B: unified DS query with uid ──────────────────
                # POST /api/ds/query with explicit datasource uid
                # This is what your Grafana version requires.
                if self.ds_uid:
                    url  = f"{self.base_url}/api/ds/query"
                    body = {
                        "from":  str(int(start.timestamp() * 1000)),
                        "to":    str(int(end.timestamp()   * 1000)),
                        "queries": [{
                            "refId":      "A",
                            "datasource": {
                                "type": "prometheus",
                                "uid":  self.ds_uid,   # ← required by your Grafana
                            },
                            "expr":          query,
                            "range":         True,
                            "instant":       False,
                            "intervalMs":    step * 1000,
                            "maxDataPoints": 720,
                        }],
                    }
                    log.debug(
                        f"[Grafana] Strategy=unified-ds-query-with-uid  "
                        f"uid={self.ds_uid}  url={url}"
                    )
                    r = await client.post(url, headers=self._headers(), json=body)
                    if r.status_code == 200:
                        return self._parse_ds_query_range(r.json(), query)
                    log.warning(
                        f"[Grafana] unified-ds-query HTTP {r.status_code} — "
                        f"falling back to datasource proxy"
                    )

                # ── Strategy C: datasource proxy ───────────────────────────
                # GET /api/datasources/proxy/uid/{uid}/api/v1/query_range
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

                log.error(
                    "[Grafana] No datasource UID available and all strategies failed.\n"
                    "  Set GRAFANA_DS_UID=ef80b373-0a84-4bcf-9a14-54b012cd2828 in your .env\n"
                    "  (health_check.py printed this value)"
                )
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
            async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:

                if self._direct:
                    url = f"{self.base_url}/api/v1/query"
                    r = await client.get(
                        url, headers=self._headers(), params={"query": query}
                    )
                    return self._handle_prom_instant_response(r, query)

                # Always validate UID on first use
                if not self._ds_uid_validated:
                    self.ds_uid = await self._discover_ds_uid(client)
                    self._ds_uid_validated = True

                # Unified DS query with uid (required by your Grafana)
                if self.ds_uid:
                    url  = f"{self.base_url}/api/ds/query"
                    body = {
                        "queries": [{
                            "refId":      "A",
                            "datasource": {
                                "type": "prometheus",
                                "uid":  self.ds_uid,
                            },
                            "expr":    query,
                            "instant": True,
                            "range":   False,
                        }],
                    }
                    log.debug(
                        f"[Grafana] Instant unified-ds-query uid={self.ds_uid}"
                    )
                    r = await client.post(url, headers=self._headers(), json=body)
                    if r.status_code == 200:
                        return self._parse_ds_query_instant(r.json(), query)
                    log.warning(
                        f"[Grafana] unified instant HTTP {r.status_code} — "
                        "falling back to datasource proxy"
                    )

                # Datasource proxy fallback
                if self.ds_uid:
                    proxy_url = (
                        f"{self.base_url}/api/datasources/proxy/uid"
                        f"/{self.ds_uid}/api/v1/query"
                    )
                    r2 = await client.get(
                        proxy_url, headers=self._headers(), params={"query": query}
                    )
                    return self._handle_prom_instant_response(r2, query)

                log.error("[Grafana] No datasource UID — instant query failed")
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
        GET /api/datasources — find the correct Prometheus datasource UID.

        Selection priority:
          1. GRAFANA_DS_NAME env var exact match  (e.g. "Prometheus-Prod-CLT")
          2. GRAFANA_DS_UID env var — validated against real datasource list
          3. Heuristic: name contains prod/cluster/namespace keyword
          4. First Prometheus datasource found (last resort)

        Always fetches from API — never blindly trusts a cached value.
        Warns clearly when env var UID doesn't match discovered name.
        """
        ds_name_hint = os.getenv("GRAFANA_DS_NAME", "").strip()
        log.info(
            f"[Grafana] Discovering datasource from /api/datasources  "
            f"name_hint='{ds_name_hint or 'none'}'  uid_hint='{self.ds_uid or 'none'}'"
        )

        try:
            r = await client.get(
                f"{self.base_url}/api/datasources",
                headers=self._headers(),
            )
            if r.status_code != 200:
                log.warning(
                    f"[Grafana] /api/datasources returned {r.status_code}: {r.text[:200]}"
                )
                return self.ds_uid

            datasources = r.json()
            prom_sources = [ds for ds in datasources if ds.get("type") == "prometheus"]

            log.info(
                f"[Grafana] {len(prom_sources)} Prometheus datasource(s) found: "
                f"{[(ds.get('name'), ds.get('uid')) for ds in prom_sources]}"
            )

            if not prom_sources:
                log.error(
                    f"[Grafana] No Prometheus datasource in Grafana!\n"
                    f"  All datasources: {[ds.get('name') for ds in datasources]}"
                )
                return ""

            chosen = None

            # Priority 1 — exact name match from GRAFANA_DS_NAME env var
            if ds_name_hint:
                for ds in prom_sources:
                    if ds.get("name", "").strip() == ds_name_hint:
                        chosen = ds
                        log.info(
                            f"[Grafana] ✅ Matched by GRAFANA_DS_NAME='{ds_name_hint}': "
                            f"uid={ds.get('uid')}"
                        )
                        break
                if not chosen:
                    log.warning(
                        f"[Grafana] GRAFANA_DS_NAME='{ds_name_hint}' not found.\n"
                        f"  Available Prometheus sources: "
                        f"{[ds.get('name') for ds in prom_sources]}\n"
                        f"  Check spelling — it is case-sensitive."
                    )

            # Priority 2 — UID env var match
            if not chosen and self.ds_uid:
                for ds in prom_sources:
                    if ds.get("uid") == self.ds_uid:
                        chosen = ds
                        log.info(
                            f"[Grafana] ✅ Matched by GRAFANA_DS_UID='{self.ds_uid}': "
                            f"name='{ds.get('name')}'"
                        )
                        break
                if not chosen:
                    log.warning(
                        f"[Grafana] GRAFANA_DS_UID='{self.ds_uid}' not found in datasource list.\n"
                        f"  Available UIDs: "
                        f"{[(ds.get('name'), ds.get('uid')) for ds in prom_sources]}\n"
                        f"  Set GRAFANA_DS_NAME=Prometheus-Prod-CLT to pick by name instead."
                    )

            # Priority 3 — heuristic (prod/cluster keywords)
            if not chosen:
                namespace = os.getenv("OCP_NAMESPACE", "").lower()
                for ds in prom_sources:
                    name = ds.get("name", "").lower()
                    if any(k in name for k in ("prod-clt", "prod_clt", namespace)):
                        chosen = ds
                        log.info(
                            f"[Grafana] Selected by heuristic 'prod-clt': "
                            f"name='{ds.get('name')}'"
                        )
                        break

            # Priority 4 — fallback: first one
            if not chosen:
                chosen = prom_sources[0]
                log.warning(
                    f"[Grafana] No specific match found — using first Prometheus datasource: "
                    f"name='{chosen.get('name')}'  uid='{chosen.get('uid')}'\n"
                    f"  To fix: set GRAFANA_DS_NAME=Prometheus-Prod-CLT in your .env"
                )

            uid  = chosen.get("uid",  "")
            name = chosen.get("name", "")
            url  = chosen.get("url",  "")

            log.info(f"[Grafana] ✅ Using: name='{name}'  uid='{uid}'  url={url}")

            if self.ds_uid and self.ds_uid != uid:
                log.warning(
                    f"[Grafana] GRAFANA_DS_UID was '{self.ds_uid}' but using '{uid}' "
                    f"('{name}').\n"
                    f"  Update .env:  GRAFANA_DS_UID={uid}"
                )

            self.ds_uid = uid
            return uid

        except Exception as e:
            log.error(f"[Grafana] Datasource discovery failed: {type(e).__name__}: {e}")
            return self.ds_uid

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
    p.add_argument("--url",       default=os.getenv("GRAFANA_URL",         "http://localhost:3000"),
                   help="Grafana base URL — same as GRAFANA_URL in your grafana-mcp")
    # Auth — mirrors grafana-mcp/index.js priority
    p.add_argument("--token",     default=os.getenv("GRAFANA_TOKEN",       "") or
                                          os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""),
                   help="Bearer token (GRAFANA_TOKEN). Takes priority over username/password.")
    p.add_argument("--username",  default=os.getenv("GRAFANA_USERNAME",    ""),
                   help="Basic auth username (GRAFANA_USERNAME)")
    p.add_argument("--password",  default=os.getenv("GRAFANA_PASSWORD",    ""),
                   help="Basic auth password (GRAFANA_PASSWORD)")
    p.add_argument("--ds-uid",    default=os.getenv("GRAFANA_DS_UID",      ""),
                   help="Prometheus datasource UID — auto-discovered if blank")
    p.add_argument("--direct",    action="store_true",
                   help="GRAFANA_URL points directly to Prometheus (not Grafana)")
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
