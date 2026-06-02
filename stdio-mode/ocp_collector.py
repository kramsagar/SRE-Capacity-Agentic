"""
scripts/collectors/ocp_collector.py

Fetches namespace quota, HPA status, and deployment resource limits
from YOUR OCP MCP server (server.py) running in stdio mode in VS Code.

YOUR MCP SERVER TOOLS (from server.py):
  Tool 2  — verify_connectivity   args: cluster, token
  Tool 3  — list_namespaces       args: cluster, token
  Tool 4  — list_resources        args: cluster, token, namespace, resource
              resource values from _API_PATHS:
                "horizontalpodautoscalers"  → /apis/autoscaling/v2
                "deployments"               → /apis/apps/v1
                "pods"                      → /api/v1
  Tool 5  — describe_resource     args: cluster, token, namespace, resource, name
  Tool 8  — describe_namespace    args: cluster, token, namespace
              returns namespace details including resource quota and limits

Every tool requires: cluster (str) + token (str)  ← CLUSTER_PROPS

HOW COLLECTOR CALLS YOUR MCP:
  In VS Code Copilot Agent mode → capacity_agent.py injects mcp_client
  → mcp_client.call_tool(server="ocp-mcp", tool=<name>, args={...})

  For standalone testing → OCPCollector(mode="direct") hits OCP REST API
  directly with httpx (no MCP needed).
"""

import asyncio
import httpx
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ── Data models ────────────────────────────────────────────────────────

@dataclass
class HPAStatus:
    name:             str
    namespace:        str
    current_replicas: int
    desired_replicas: int
    max_replicas:     int
    min_replicas:     int

    @property
    def headroom_percent(self) -> float:
        return round(
            (self.max_replicas - self.current_replicas) / max(self.max_replicas, 1) * 100, 1
        )

    @property
    def utilization_percent(self) -> float:
        return round(self.current_replicas / max(self.max_replicas, 1) * 100, 1)

    @property
    def is_at_risk(self) -> bool:
        return self.utilization_percent >= 80.0


@dataclass
class NamespaceQuota:
    namespace:         str
    cpu_hard_cores:    Optional[float]
    cpu_used_cores:    Optional[float]
    memory_hard_bytes: Optional[float]
    memory_used_bytes: Optional[float]
    pods_hard:         Optional[int]
    pods_used:         Optional[int]

    @property
    def cpu_used_percent(self) -> Optional[float]:
        if self.cpu_hard_cores and self.cpu_hard_cores > 0:
            return round((self.cpu_used_cores or 0) / self.cpu_hard_cores * 100, 1)
        return None

    @property
    def memory_used_percent(self) -> Optional[float]:
        if self.memory_hard_bytes and self.memory_hard_bytes > 0:
            return round((self.memory_used_bytes or 0) / self.memory_hard_bytes * 100, 1)
        return None


# ══════════════════════════════════════════════════════════════════════
# OCPCollector
# ══════════════════════════════════════════════════════════════════════

class OCPCollector:
    """
    Collects OCP capacity data using YOUR MCP server tools.

    modes:
      "ocp_mcp"  — calls your server.py tools via VS Code MCP (default)
      "direct"   — hits OCP REST API directly with httpx (no MCP needed,
                   useful for standalone CLI testing)

    Args:
        cluster    : OCP API server URL  e.g. https://api.mycluster.example.com:6443
        token      : OCP Bearer token (from `oc whoami -t` or service account)
        mcp_client : injected by capacity_agent when mode="ocp_mcp"
        mcp_server : name of your MCP server as registered in .vscode/mcp.json
                     default "ocp-mcp" — change to match your server name
        verify_ssl : set False for self-signed OCP certs (common in enterprise)
    """

    def __init__(
        self,
        cluster:    str  = None,
        token:      str  = None,
        mode:       str  = "ocp_mcp",
        mcp_client         = None,
        mcp_server: str  = "ocp-mcp",
        verify_ssl: bool = False,
        timeout:    int  = 30,
    ):
        self.cluster    = cluster    or os.getenv("OCP_API_URL",  "")
        self.token      = token      or os.getenv("OCP_TOKEN",    "")
        self.mode       = mode
        self.mcp_client = mcp_client
        self.mcp_server = mcp_server
        self.verify_ssl = verify_ssl
        self.timeout    = timeout

        log.info(
            f"[OCP] OCPCollector init: mode={mode}  cluster={self.cluster}  "
            f"mcp_server={mcp_server}  token={'set' if self.token else 'NOT SET'}  "
            f"verify_ssl={verify_ssl}"
        )

    # ── CLUSTER_PROPS — passed to every tool call ─────────────────────

    @property
    def _cluster_args(self) -> dict:
        """Base args required by every tool in your server.py (CLUSTER_PROPS)."""
        return {
            "cluster": self.cluster,
            "token":   self.token,
        }

    # ── Public API ────────────────────────────────────────────────────

    async def collect_all(self, namespace: str) -> dict:
        """Run all three collections in parallel."""
        log.info(f"[OCP] collect_all: namespace={namespace}  mode={self.mode}")

        quota, hpa_list, deployments = await asyncio.gather(
            self.get_namespace_quota(namespace),
            self.get_all_hpa(namespace),
            self.get_deployment_resources(namespace),
        )

        log.info(
            f"[OCP] collect_all done: "
            f"quota={'ok' if quota.cpu_hard_cores else 'defaults'}  "
            f"HPAs={len(hpa_list)}  deployments={len(deployments)}"
        )
        return {
            "quota":       quota,
            "hpa":         {h.name: h for h in hpa_list},
            "deployments": deployments,
        }

    async def get_namespace_quota(self, namespace: str) -> NamespaceQuota:
        """
        Uses Tool 8 — describe_namespace.
        Returns namespace details including ResourceQuota hard/used limits.
        """
        log.debug(f"[OCP] get_namespace_quota → tool: describe_namespace  ns={namespace}")

        raw = await self._call(
            tool="describe_namespace",
            extra_args={"namespace": namespace},
        )

        if raw is None:
            log.warning(f"[OCP] describe_namespace returned None for '{namespace}' — using defaults")
            return NamespaceQuota(namespace, None, None, None, None, None, None)

        return self._parse_namespace_quota(namespace, raw)

    async def get_all_hpa(self, namespace: str) -> list[HPAStatus]:
        """
        Uses Tool 4 — list_resources with resource="horizontalpodautoscalers".
        Returns all HPAs in the namespace.
        """
        log.debug(f"[OCP] get_all_hpa → tool: list_resources  resource=horizontalpodautoscalers  ns={namespace}")

        raw = await self._call(
            tool="list_resources",
            extra_args={
                "namespace": namespace,
                "resource":  "horizontalpodautoscalers",
            },
        )

        if raw is None:
            log.warning(f"[OCP] list_resources(hpa) returned None for ns='{namespace}'")
            return []

        items = self._extract_items(raw, "HPA")
        result = self._parse_hpa_items(namespace, items)

        log.info(f"[OCP] HPAs found in '{namespace}': {[h.name for h in result]}")
        if not result:
            log.warning(
                f"[OCP] 0 HPAs in '{namespace}'. "
                f"Verify in Copilot: 'list HPAs in namespace {namespace}'"
            )
        return result

    async def get_deployment_resources(self, namespace: str) -> dict:
        """
        Uses Tool 4 — list_resources with resource="deployments".
        Returns CPU/memory limits per deployment.
        """
        log.debug(f"[OCP] get_deployment_resources → tool: list_resources  resource=deployments  ns={namespace}")

        raw = await self._call(
            tool="list_resources",
            extra_args={
                "namespace": namespace,
                "resource":  "deployments",
            },
        )

        if raw is None:
            log.warning(f"[OCP] list_resources(deployments) returned None for ns='{namespace}'")
            return {}

        items = self._extract_items(raw, "Deployment")
        result = self._parse_deployment_items(items)

        log.info(f"[OCP] Deployments found in '{namespace}': {list(result.keys())}")
        return result

    async def verify_connectivity(self) -> bool:
        """
        Uses Tool 2 — verify_connectivity.
        Run this before collect_all to confirm cluster+token are valid.
        """
        log.info(f"[OCP] verify_connectivity: cluster={self.cluster}")
        raw = await self._call(tool="verify_connectivity", extra_args={})
        if raw is None:
            log.error("[OCP] verify_connectivity failed — check OCP_API_URL and OCP_TOKEN")
            return False
        log.info(f"[OCP] verify_connectivity: {str(raw)[:200]}")
        return True

    async def list_namespaces(self) -> list[str]:
        """Uses Tool 3 — list_namespaces."""
        log.debug("[OCP] list_namespaces")
        raw = await self._call(tool="list_namespaces", extra_args={})
        if raw is None:
            return []
        # Your tool likely returns list of namespace objects or names
        if isinstance(raw, list):
            return [
                (item.get("metadata", {}).get("name") if isinstance(item, dict) else str(item))
                for item in raw
            ]
        items = self._extract_items(raw, "Namespace")
        return [item.get("metadata", {}).get("name", "") for item in items]

    # ── Routing: MCP vs Direct ────────────────────────────────────────

    async def _call(self, tool: str, extra_args: dict) -> Optional[dict]:
        """Route to MCP or direct REST based on mode."""
        args = {**self._cluster_args, **extra_args}
        log.debug(f"[OCP] _call: tool={tool}  args_keys={list(args.keys())}")

        if self.mode == "ocp_mcp":
            return await self._mcp_call(tool, args)
        return await self._direct_call(tool, extra_args)

    # ── MCP path ──────────────────────────────────────────────────────

    async def _mcp_call(self, tool: str, args: dict) -> Optional[dict]:
        """
        Call your server.py tool via VS Code MCP client.
        mcp_client.call_tool(server, tool, args) is injected by capacity_agent.
        """
        if self.mcp_client is None:
            log.error(
                "[OCP] mcp_client is None — cannot call MCP tools. "
                "Use mode='direct' for standalone use."
            )
            return None

        log.debug(f"[OCP] MCP call → server={self.mcp_server}  tool={tool}")
        try:
            result = await self.mcp_client.call_tool(
                server=self.mcp_server,
                tool=tool,
                args=args,
            )
            log.debug(f"[OCP] MCP response for {tool}: {str(result)[:300]}")
            return result
        except Exception as e:
            log.error(f"[OCP] MCP call failed: tool={tool}  error={type(e).__name__}: {e}")
            return None

    # ── Direct REST path (standalone / testing) ───────────────────────

    async def _direct_call(self, tool: str, extra_args: dict) -> Optional[dict]:
        """
        Hit OCP REST API directly — mirrors what your server.py does internally.
        Used for: standalone CLI testing, health checks, no MCP needed.
        """
        namespace = extra_args.get("namespace", "")

        _API_PATHS = {
            "pods":                      "/api/v1",
            "services":                  "/api/v1",
            "configmaps":                "/api/v1",
            "secrets":                   "/api/v1",
            "events":                    "/api/v1",
            "namespaces":                "/api/v1",
            "deployments":               "/apis/apps/v1",
            "statefulsets":              "/apis/apps/v1",
            "replicasets":               "/apis/apps/v1",
            "routes":                    "/apis/route.openshift.io/v1",
            "horizontalpodautoscalers":  "/apis/autoscaling/v2",
        }

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept":        "application/json",
            "Content-Type":  "application/json",
        }

        try:
            async with httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=self.timeout,
            ) as client:

                if tool == "verify_connectivity":
                    url = f"{self.cluster}/api/v1"
                    r   = await client.get(url, headers=headers)
                    log.debug(f"[OCP] direct verify_connectivity: HTTP {r.status_code}")
                    return {"status": r.status_code, "ok": r.status_code == 200}

                if tool == "list_namespaces":
                    url = f"{self.cluster}/api/v1/namespaces"
                    r   = await client.get(url, headers=headers)
                    self._log_response(r, tool)
                    return r.json() if r.status_code == 200 else None

                if tool == "list_resources":
                    resource = extra_args.get("resource", "")
                    prefix   = _API_PATHS.get(resource)
                    if not prefix:
                        log.error(f"[OCP] Unknown resource type: '{resource}'")
                        return None
                    url = (
                        f"{self.cluster}{prefix}/namespaces/{namespace}/{resource}"
                        if namespace else
                        f"{self.cluster}{prefix}/{resource}"
                    )
                    log.debug(f"[OCP] direct list_resources: GET {url}")
                    r = await client.get(url, headers=headers)
                    self._log_response(r, tool)
                    return r.json() if r.status_code == 200 else None

                if tool == "describe_resource":
                    resource = extra_args.get("resource", "")
                    name     = extra_args.get("name", "")
                    prefix   = _API_PATHS.get(resource)
                    if not prefix:
                        log.error(f"[OCP] Unknown resource type: '{resource}'")
                        return None
                    url = f"{self.cluster}{prefix}/namespaces/{namespace}/{resource}/{name}"
                    log.debug(f"[OCP] direct describe_resource: GET {url}")
                    r = await client.get(url, headers=headers)
                    self._log_response(r, tool)
                    return r.json() if r.status_code == 200 else None

                if tool == "describe_namespace":
                    # GET namespace object + ResourceQuotas
                    ns_url    = f"{self.cluster}/api/v1/namespaces/{namespace}"
                    quota_url = f"{self.cluster}/api/v1/namespaces/{namespace}/resourcequotas"
                    ns_r, quota_r = await asyncio.gather(
                        client.get(ns_url,    headers=headers),
                        client.get(quota_url, headers=headers),
                    )
                    log.debug(
                        f"[OCP] direct describe_namespace: "
                        f"ns={ns_r.status_code}  quota={quota_r.status_code}"
                    )
                    ns_data    = ns_r.json()    if ns_r.status_code    == 200 else {}
                    quota_data = quota_r.json() if quota_r.status_code == 200 else {}
                    return {"namespace": ns_data, "resourceQuotas": quota_data}

                log.error(f"[OCP] direct mode: unknown tool '{tool}'")
                return None

        except httpx.ConnectError as e:
            log.error(
                f"[OCP] Cannot connect to {self.cluster}: {e}\n"
                f"  Check OCP_API_URL='{self.cluster}' is reachable"
            )
            return None
        except httpx.InvalidURL as e:
            log.error(
                f"[OCP] Invalid OCP URL '{self.cluster}': {e}\n"
                f"  OCP_API_URL must be https://api.cluster.example.com:6443"
            )
            return None
        except Exception as e:
            log.error(f"[OCP] direct call failed: tool={tool}  {type(e).__name__}: {e}")
            return None

    # ── Response logging ──────────────────────────────────────────────

    def _log_response(self, r: httpx.Response, tool: str):
        if r.status_code == 200:
            log.debug(f"[OCP] {tool} HTTP 200  body_len={len(r.text)}")
        elif r.status_code == 401:
            log.error(
                f"[OCP] 401 Unauthorized for {tool}\n"
                f"  → OCP_TOKEN is missing or expired\n"
                f"  → Get a new token: oc whoami -t"
            )
        elif r.status_code == 403:
            log.error(
                f"[OCP] 403 Forbidden for {tool}\n"
                f"  → Service account lacks RBAC permissions\n"
                f"  → Run: oc adm policy add-role-to-user view <sa> -n <ns>"
            )
        elif r.status_code == 404:
            log.warning(f"[OCP] 404 Not Found for {tool}  url={r.url}")
        else:
            log.error(f"[OCP] {tool} HTTP {r.status_code}: {r.text[:200]}")

    # ── Parsers ───────────────────────────────────────────────────────

    def _extract_items(self, raw: dict, kind: str) -> list:
        """
        Handle both list response  { kind: "...List", items: [...] }
        and single object response { kind: "...", metadata: {...} }.
        """
        if not raw:
            return []

        # Standard Kubernetes list format
        if "items" in raw:
            items = raw["items"]
            log.debug(f"[OCP] _extract_items({kind}): {len(items)} items")
            return items

        # Your MCP server might return a plain list
        if isinstance(raw, list):
            log.debug(f"[OCP] _extract_items({kind}): list of {len(raw)}")
            return raw

        # Single object wrapped in a dict by your MCP server
        if raw.get("kind") and not raw.get("kind", "").endswith("List"):
            log.debug(f"[OCP] _extract_items({kind}): single object")
            return [raw]

        log.warning(f"[OCP] _extract_items({kind}): unexpected format — keys={list(raw.keys())[:8]}")
        return []

    def _parse_namespace_quota(self, namespace: str, raw: dict) -> NamespaceQuota:
        """
        Parse describe_namespace response.
        Your tool returns { "namespace": {...}, "resourceQuotas": { items: [...] } }
        OR the direct Kubernetes ResourceQuota list format.
        """
        cpu_hard = cpu_used = mem_hard = mem_used = None
        pods_hard = pods_used = 0

        # Handle your MCP server's describe_namespace wrapper
        quota_data = raw.get("resourceQuotas", raw)
        items = (
            quota_data.get("items", [])
            if isinstance(quota_data, dict)
            else quota_data if isinstance(quota_data, list)
            else []
        )

        log.debug(f"[OCP] Parsing quota: {len(items)} ResourceQuota object(s) found")

        for item in items:
            status = item.get("status", {})
            hard   = status.get("hard", {})
            used   = status.get("used", {})

            log.debug(f"[OCP] Quota hard: {hard}")
            log.debug(f"[OCP] Quota used: {used}")

            cpu_hard  = _cpu(hard.get("limits.cpu")    or hard.get("cpu"))
            cpu_used  = _cpu(used.get("limits.cpu")    or used.get("cpu"))
            mem_hard  = _mem(hard.get("limits.memory") or hard.get("memory"))
            mem_used  = _mem(used.get("limits.memory") or used.get("memory"))
            pods_hard = int(hard.get("pods", 0) or 0)
            pods_used = int(used.get("pods", 0) or 0)

        if cpu_hard:
            log.info(
                f"[OCP] Quota for '{namespace}': "
                f"CPU={cpu_used}/{cpu_hard} cores  "
                f"Mem={_fmt_mem(mem_used)}/{_fmt_mem(mem_hard)}  "
                f"Pods={pods_used}/{pods_hard}"
            )
        else:
            log.warning(
                f"[OCP] No ResourceQuota found for '{namespace}' — "
                f"predictor will use defaults (16 CPU cores, 32 GB RAM)"
            )

        return NamespaceQuota(
            namespace, cpu_hard, cpu_used,
            mem_hard, mem_used, pods_hard, pods_used,
        )

    def _parse_hpa_items(self, namespace: str, items: list) -> list[HPAStatus]:
        result = []
        for item in items:
            try:
                name   = item["metadata"]["name"]
                spec   = item.get("spec", {})
                status = item.get("status", {})
                max_r  = int(spec.get("maxReplicas", 1))
                curr   = int(status.get("currentReplicas", 0))
                des    = int(status.get("desiredReplicas", curr))
                min_r  = int(spec.get("minReplicas", 1))
                h = HPAStatus(
                    name=name, namespace=namespace,
                    current_replicas=curr, desired_replicas=des,
                    max_replicas=max_r, min_replicas=min_r,
                )
                log.debug(
                    f"[OCP] HPA: {name}  current={curr}  desired={des}  "
                    f"max={max_r}  util={h.utilization_percent}%"
                )
                result.append(h)
            except (KeyError, TypeError) as e:
                log.warning(f"[OCP] Skipping malformed HPA item: {e}  item={str(item)[:100]}")
        return result

    def _parse_deployment_items(self, items: list) -> dict:
        out = {}
        for item in items:
            try:
                name       = item["metadata"]["name"]
                containers = (
                    item.get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("containers", [])
                )
                cpu_lim = sum(
                    _cpu(c.get("resources", {}).get("limits", {}).get("cpu", "0"))
                    for c in containers
                )
                mem_lim = sum(
                    _mem(c.get("resources", {}).get("limits", {}).get("memory", "0"))
                    for c in containers
                )
                out[name] = {
                    "cpu_limit_cores":    round(cpu_lim, 3),
                    "memory_limit_bytes": mem_lim,
                }
                log.debug(
                    f"[OCP] Deployment: {name}  "
                    f"cpu_limit={cpu_lim:.3f}  mem_limit={_fmt_mem(mem_lim)}"
                )
            except (KeyError, TypeError) as e:
                log.warning(f"[OCP] Skipping malformed Deployment: {e}")
        return out


# ── Unit helpers ────────────────────────────────────────────────────────

def _cpu(v) -> float:
    if not v:
        return 0.0
    v = str(v).strip()
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)

def _mem(v) -> float:
    if not v:
        return 0.0
    v = str(v).strip()
    for s, m in [
        ("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024),
        ("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3),
    ]:
        if v.endswith(s):
            return float(v[:-len(s)]) * m
    return float(v)

def _fmt_mem(b) -> str:
    if not b:
        return "0"
    b = float(b)
    if b >= 1024**3:
        return f"{b/1024**3:.1f}Gi"
    if b >= 1024**2:
        return f"{b/1024**2:.0f}Mi"
    return f"{b:.0f}"


# ── CLI smoke-test ───────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser(
        description="OCP Collector smoke-test — direct REST mode (no MCP needed)"
    )
    p.add_argument("--namespace", default=os.getenv("OCP_NAMESPACE", "alprc-prod"))
    p.add_argument("--cluster",   default=os.getenv("OCP_API_URL",   ""),
                   help="OCP API URL e.g. https://api.cluster.example.com:6443")
    p.add_argument("--token",     default=os.getenv("OCP_TOKEN",     ""),
                   help="Bearer token from: oc whoami -t")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip SSL verification (common in enterprise OCP)")
    p.add_argument("--debug",     action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(name)-42s %(levelname)-8s  %(message)s",
    )

    if not args.cluster:
        print("ERROR: --cluster is required (or set OCP_API_URL env var)")
        print("  Example: https://api.mycluster.example.com:6443")
        return
    if not args.token:
        print("ERROR: --token is required (or set OCP_TOKEN env var)")
        print("  Get it with: oc whoami -t")
        return

    c = OCPCollector(
        cluster=args.cluster,
        token=args.token,
        mode="direct",
        verify_ssl=not args.no_verify,
    )

    print(f"\nCluster    : {c.cluster}")
    print(f"Namespace  : {args.namespace}")
    print(f"SSL verify : {not args.no_verify}\n")

    # Step 0 — connectivity
    print("── Connectivity ─────────────────────────────────────────────")
    ok = await c.verify_connectivity()
    print(f"  {'✅ Connected' if ok else '❌ FAILED — check OCP_API_URL and OCP_TOKEN'}\n")
    if not ok:
        return

    # Step 1 — collect all
    print(f"── Collecting namespace: {args.namespace} ──────────────────")
    data = await c.collect_all(args.namespace)
    q    = data["quota"]

    print(f"\nResourceQuota:")
    if q.cpu_hard_cores:
        print(f"  CPU    : {q.cpu_used_cores:.2f} / {q.cpu_hard_cores:.2f} cores  ({q.cpu_used_percent}%)")
        print(f"  Memory : {_fmt_mem(q.memory_used_bytes)} / {_fmt_mem(q.memory_hard_bytes)}  ({q.memory_used_percent}%)")
        print(f"  Pods   : {q.pods_used} / {q.pods_hard}")
    else:
        print("  No ResourceQuota found — predictor uses defaults")

    print(f"\nHPAs ({len(data['hpa'])}):")
    for name, hpa in data["hpa"].items():
        risk = "  ⚠ AT RISK" if hpa.is_at_risk else ""
        print(
            f"  {name:<40} {hpa.current_replicas}/{hpa.max_replicas} replicas  "
            f"({hpa.utilization_percent}%){risk}"
        )

    print(f"\nDeployments ({len(data['deployments'])}):")
    for name, d in data["deployments"].items():
        print(
            f"  {name:<40} "
            f"cpu_limit={d['cpu_limit_cores']}  "
            f"mem_limit={_fmt_mem(d['memory_limit_bytes'])}"
        )

if __name__ == "__main__":
    asyncio.run(_cli())
