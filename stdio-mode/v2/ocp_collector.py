"""
scripts/collectors/ocp_collector.py

Calls ocp-mcp tools to get namespace quota, HPA, deployments.
Your ocp-mcp/server.py handles auth and OCP API calls.

Tools used (from your server.py):
  Tool 4  list_resources   → HPAs, Deployments
  Tool 8  describe_namespace → ResourceQuota
  Tool 2  verify_connectivity → health check
"""

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class HPAStatus:
    name:             str
    namespace:        str
    current_replicas: int
    desired_replicas: int
    max_replicas:     int
    min_replicas:     int

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


class OCPCollector:
    """
    Fetches OCP resources via ocp-mcp.

    Tools called:
      describe_namespace  → ResourceQuota (hard/used limits)
      list_resources      → HPAs and Deployments
      verify_connectivity → connectivity check

    Every tool in your server.py requires: cluster + token
    These come from OCP_API_URL and OCP_TOKEN env vars,
    which your ocp-mcp/server.py reads at startup.

    mcp: MCPClient instance (already started)
    """

    def __init__(self, mcp, cluster: str = "", token: str = ""):
        import os
        self.mcp     = mcp
        self.cluster = cluster or os.getenv("OCP_API_URL", "")
        self.token   = token   or os.getenv("OCP_TOKEN",   "")
        log.info(
            f"[OCP] OCPCollector: cluster={self.cluster}  "
            f"token={'set' if self.token else 'NOT SET'}"
        )

    @property
    def _base(self) -> dict:
        """cluster + token required by every tool in your server.py."""
        return {"cluster": self.cluster, "token": self.token}

    async def collect_all(self, namespace: str) -> dict:
        import asyncio
        log.info(f"[OCP] collect_all: namespace={namespace}")

        quota, hpa_list, deployments = await asyncio.gather(
            self.get_namespace_quota(namespace),
            self.get_all_hpa(namespace),
            self.get_deployment_resources(namespace),
        )

        log.info(
            f"[OCP] done: quota={'ok' if quota.cpu_hard_cores else 'defaults'}  "
            f"HPAs={len(hpa_list)}  deployments={len(deployments)}"
        )
        return {
            "quota":       quota,
            "hpa":         {h.name: h for h in hpa_list},
            "deployments": deployments,
        }

    async def get_namespace_quota(self, namespace: str) -> NamespaceQuota:
        log.debug(f"[OCP] describe_namespace: {namespace}")
        try:
            raw = await self.mcp.call(
                "ocp-mcp",
                "describe_namespace",
                {**self._base, "namespace": namespace},
            )
            return self._parse_quota(namespace, raw)
        except Exception as e:
            log.error(f"[OCP] describe_namespace failed: {e}")
            return NamespaceQuota(namespace, None, None, None, None, None, None)

    async def get_all_hpa(self, namespace: str) -> list[HPAStatus]:
        log.debug(f"[OCP] list_resources HPAs: {namespace}")
        try:
            raw = await self.mcp.call(
                "ocp-mcp",
                "list_resources",
                {**self._base, "namespace": namespace, "resource": "horizontalpodautoscalers"},
            )
            items  = self._items(raw)
            result = [self._parse_hpa(namespace, item) for item in items]
            log.info(f"[OCP] HPAs: {[h.name for h in result]}")
            return result
        except Exception as e:
            log.error(f"[OCP] list_resources(hpa) failed: {e}")
            return []

    async def get_deployment_resources(self, namespace: str) -> dict:
        log.debug(f"[OCP] list_resources deployments: {namespace}")
        try:
            raw = await self.mcp.call(
                "ocp-mcp",
                "list_resources",
                {**self._base, "namespace": namespace, "resource": "deployments"},
            )
            items  = self._items(raw)
            result = {
                item["metadata"]["name"]: self._parse_deployment(item)
                for item in items
            }
            log.info(f"[OCP] Deployments: {list(result.keys())}")
            return result
        except Exception as e:
            log.error(f"[OCP] list_resources(deployments) failed: {e}")
            return {}

    # ── Parsers ───────────────────────────────────────────────────────

    def _items(self, raw) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("items", [])
        return []

    def _parse_quota(self, namespace: str, raw) -> NamespaceQuota:
        items = []
        if isinstance(raw, dict):
            # describe_namespace returns {"namespace":{...}, "resourceQuotas":{items:[...]}}
            quota_data = raw.get("resourceQuotas", raw)
            items = quota_data.get("items", []) if isinstance(quota_data, dict) else []

        cpu_hard = cpu_used = mem_hard = mem_used = None
        pods_hard = pods_used = 0

        for item in items:
            hard = item.get("status", {}).get("hard", {})
            used = item.get("status", {}).get("used", {})
            cpu_hard  = _cpu(hard.get("limits.cpu")    or hard.get("cpu"))
            cpu_used  = _cpu(used.get("limits.cpu")    or used.get("cpu"))
            mem_hard  = _mem(hard.get("limits.memory") or hard.get("memory"))
            mem_used  = _mem(used.get("limits.memory") or used.get("memory"))
            pods_hard = int(hard.get("pods", 0) or 0)
            pods_used = int(used.get("pods", 0) or 0)

        if cpu_hard:
            log.info(
                f"[OCP] Quota: CPU={cpu_used}/{cpu_hard}cores  "
                f"Mem={_fmt(mem_used)}/{_fmt(mem_hard)}  Pods={pods_used}/{pods_hard}"
            )
        else:
            log.warning(f"[OCP] No ResourceQuota in '{namespace}' — predictor uses defaults")

        return NamespaceQuota(namespace, cpu_hard, cpu_used,
                              mem_hard, mem_used, pods_hard, pods_used)

    def _parse_hpa(self, namespace: str, item: dict) -> HPAStatus:
        spec   = item.get("spec", {})
        status = item.get("status", {})
        return HPAStatus(
            name             = item["metadata"]["name"],
            namespace        = namespace,
            current_replicas = int(status.get("currentReplicas", 0)),
            desired_replicas = int(status.get("desiredReplicas", 0)),
            max_replicas     = int(spec.get("maxReplicas", 1)),
            min_replicas     = int(spec.get("minReplicas", 1)),
        )

    def _parse_deployment(self, item: dict) -> dict:
        containers = (item.get("spec", {}).get("template", {})
                         .get("spec", {}).get("containers", []))
        cpu_lim = sum(_cpu(c.get("resources",{}).get("limits",{}).get("cpu",    "0")) for c in containers)
        mem_lim = sum(_mem(c.get("resources",{}).get("limits",{}).get("memory", "0")) for c in containers)
        return {"cpu_limit_cores": round(cpu_lim, 3), "memory_limit_bytes": mem_lim}


# ── unit helpers ─────────────────────────────────────────────────────

def _cpu(v) -> float:
    if not v: return 0.0
    v = str(v).strip()
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)

def _mem(v) -> float:
    if not v: return 0.0
    v = str(v).strip()
    for s, m in [("Ti",1024**4),("Gi",1024**3),("Mi",1024**2),("Ki",1024),
                 ("T",1e12),("G",1e9),("M",1e6),("K",1e3)]:
        if v.endswith(s): return float(v[:-len(s)]) * m
    return float(v)

def _fmt(b) -> str:
    if not b: return "0"
    b = float(b)
    if b >= 1024**3: return f"{b/1024**3:.1f}Gi"
    if b >= 1024**2: return f"{b/1024**2:.0f}Mi"
    return f"{b:.0f}"
