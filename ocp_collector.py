"""
scripts/collectors/ocp_collector.py

Fetches namespace quota, HPA status, and deployment resource limits from OCP.

Two modes:
  "cli"     — runs `oc get ... -o json` as a subprocess (standalone use)
  "ocp_mcp" — calls the kubernetes MCP server VS Code started.
               mcp_client injected by capacity_agent.py.

Kubernetes MCP tools used:
  get_resource   → ResourceQuota, HPA, Deployment objects
  list_resources → list all HPAs or Deployments in a namespace
  docs: https://github.com/containers/kubernetes-mcp-server
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Optional
from pathlib import Path


@dataclass
class HPAStatus:
    name: str
    namespace: str
    current_replicas: int
    desired_replicas: int
    max_replicas: int
    min_replicas: int

    @property
    def headroom_percent(self) -> float:
        if self.max_replicas == 0:
            return 100.0
        return round((self.max_replicas - self.current_replicas) / self.max_replicas * 100, 1)

    @property
    def utilization_percent(self) -> float:
        if self.max_replicas == 0:
            return 0.0
        return round(self.current_replicas / self.max_replicas * 100, 1)

    @property
    def is_at_risk(self) -> bool:
        return self.utilization_percent >= 80.0


@dataclass
class NamespaceQuota:
    namespace: str
    cpu_hard_cores: Optional[float]
    cpu_used_cores: Optional[float]
    memory_hard_bytes: Optional[float]
    memory_used_bytes: Optional[float]
    pods_hard: Optional[int]
    pods_used: Optional[int]

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
    Collect namespace quota + HPA status from OCP/Kubernetes.

    Args:
        mode:       "cli" | "ocp_mcp"
        mcp_client: injected by capacity_agent when mode="ocp_mcp"
        kubeconfig: optional path override for CLI mode
    """

    def __init__(
        self,
        mode:       str = "cli",
        mcp_client=None,
        kubeconfig: str = None,
        timeout:    int = 30,
    ):
        self.mode       = mode
        self.mcp_client = mcp_client
        self.kubeconfig = kubeconfig
        self.timeout    = timeout

    # ── public ───────────────────────────────────────────────────────

    async def collect_all(self, namespace: str) -> dict:
        quota, hpa_list, deployments = await asyncio.gather(
            self.get_namespace_quota(namespace),
            self.get_all_hpa(namespace),
            self.get_deployment_resources(namespace),
        )
        return {
            "quota":       quota,
            "hpa":         {h.name: h for h in hpa_list},
            "deployments": deployments,
        }

    async def get_namespace_quota(self, namespace: str) -> NamespaceQuota:
        if self.mode == "ocp_mcp":
            return await self._mcp_get_quota(namespace)
        return await self._cli_get_quota(namespace)

    async def get_all_hpa(self, namespace: str) -> list[HPAStatus]:
        if self.mode == "ocp_mcp":
            return await self._mcp_get_hpa(namespace)
        return await self._cli_get_hpa(namespace)

    async def get_deployment_resources(self, namespace: str) -> dict:
        if self.mode == "ocp_mcp":
            return await self._mcp_get_deployments(namespace)
        return await self._cli_get_deployments(namespace)

    # ── Kubernetes MCP backend ────────────────────────────────────────
    # Tool: "get_resource"   args: { kind, namespace, name? }
    # Tool: "list_resources" args: { kind, namespace }
    # Both return raw Kubernetes JSON objects.

    async def _mcp_get_quota(self, namespace: str) -> NamespaceQuota:
        raw = await self.mcp_client.call_tool(
            server="kubernetes",
            tool="list_resources",
            args={"kind": "ResourceQuota", "namespace": namespace},
        )
        items = raw.get("items", [raw] if "status" in raw else [])
        return self._parse_quota(namespace, items)

    async def _mcp_get_hpa(self, namespace: str) -> list[HPAStatus]:
        raw = await self.mcp_client.call_tool(
            server="kubernetes",
            tool="list_resources",
            args={"kind": "HorizontalPodAutoscaler", "namespace": namespace},
        )
        return self._parse_hpa_items(namespace, raw.get("items", []))

    async def _mcp_get_deployments(self, namespace: str) -> dict:
        raw = await self.mcp_client.call_tool(
            server="kubernetes",
            tool="list_resources",
            args={"kind": "Deployment", "namespace": namespace},
        )
        return self._parse_deployment_items(raw.get("items", []))

    # ── CLI backend ───────────────────────────────────────────────────

    async def _oc(self, *args) -> dict:
        cmd = ["oc"] + list(args) + ["-o", "json"]
        if self.kubeconfig:
            cmd = ["oc", f"--kubeconfig={self.kubeconfig}"] + list(args) + ["-o", "json"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"oc failed: {stderr.decode()}")
        return json.loads(stdout.decode())

    async def _cli_get_quota(self, namespace: str) -> NamespaceQuota:
        data  = await self._oc("get", "resourcequota", "-n", namespace)
        items = data.get("items", [data])
        return self._parse_quota(namespace, items)

    async def _cli_get_hpa(self, namespace: str) -> list[HPAStatus]:
        data = await self._oc("get", "hpa", "-n", namespace)
        return self._parse_hpa_items(namespace, data.get("items", []))

    async def _cli_get_deployments(self, namespace: str) -> dict:
        data = await self._oc("get", "deployments", "-n", namespace)
        return self._parse_deployment_items(data.get("items", []))

    # ── parsers ───────────────────────────────────────────────────────

    def _parse_quota(self, namespace: str, items: list) -> NamespaceQuota:
        cpu_hard = cpu_used = mem_hard = mem_used = None
        pods_hard = pods_used = 0
        for item in items:
            hard = item.get("status", {}).get("hard", {})
            used = item.get("status", {}).get("used", {})
            cpu_hard  = _cpu(hard.get("limits.cpu") or hard.get("cpu"))
            cpu_used  = _cpu(used.get("limits.cpu") or used.get("cpu"))
            mem_hard  = _mem(hard.get("limits.memory") or hard.get("memory"))
            mem_used  = _mem(used.get("limits.memory") or used.get("memory"))
            pods_hard = int(hard.get("pods", 0) or 0)
            pods_used = int(used.get("pods", 0) or 0)
        return NamespaceQuota(namespace, cpu_hard, cpu_used, mem_hard, mem_used,
                              pods_hard, pods_used)

    def _parse_hpa_items(self, namespace: str, items: list) -> list[HPAStatus]:
        out = []
        for item in items:
            name    = item["metadata"]["name"]
            spec    = item.get("spec", {})
            status  = item.get("status", {})
            current = int(status.get("currentReplicas", 0))
            max_r   = int(spec.get("maxReplicas", 1))
            out.append(HPAStatus(
                name=name, namespace=namespace,
                current_replicas=current,
                desired_replicas=int(status.get("desiredReplicas", 0)),
                max_replicas=max_r,
                min_replicas=int(spec.get("minReplicas", 1)),
            ))
        return out

    def _parse_deployment_items(self, items: list) -> dict:
        out = {}
        for item in items:
            name       = item["metadata"]["name"]
            containers = (item.get("spec", {}).get("template", {})
                             .get("spec", {}).get("containers", []))
            cpu_lim = sum(_cpu(c.get("resources", {}).get("limits", {}).get("cpu", "0"))
                         for c in containers)
            mem_lim = sum(_mem(c.get("resources", {}).get("limits", {}).get("memory", "0"))
                         for c in containers)
            out[name] = {
                "cpu_limit_cores":   round(cpu_lim, 3),
                "memory_limit_bytes": mem_lim,
            }
        return out


# ── unit helpers ───────────────────────────────────────────────────────

def _cpu(v) -> float:
    if not v:
        return 0.0
    v = str(v).strip()
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)


def _mem(v) -> float:
    if not v:
        return 0.0
    v = str(v).strip()
    for suffix, mult in [("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2),
                         ("Ki", 1024), ("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)]:
        if v.endswith(suffix):
            return float(v[:-len(suffix)]) * mult
    return float(v)


# ── CLI ─────────────────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="payments-prod")
    p.add_argument("--mode", default="cli", choices=["cli", "ocp_mcp"])
    args = p.parse_args()

    c    = OCPCollector(mode=args.mode)
    data = await c.collect_all(args.namespace)
    q    = data["quota"]
    print(f"Namespace : {args.namespace}")
    print(f"CPU       : {q.cpu_used_cores:.2f} / {q.cpu_hard_cores} cores  ({q.cpu_used_percent}%)")
    print(f"Memory    : {(q.memory_used_bytes or 0)/1e9:.1f} / {(q.memory_hard_bytes or 0)/1e9:.1f} GB  ({q.memory_used_percent}%)")
    print(f"Pods      : {q.pods_used} / {q.pods_hard}")
    print("\nHPA:")
    for name, hpa in data["hpa"].items():
        risk = "  ⚠ AT RISK" if hpa.is_at_risk else ""
        print(f"  {name}: {hpa.current_replicas}/{hpa.max_replicas} replicas  ({hpa.utilization_percent}%){risk}")

if __name__ == "__main__":
    asyncio.run(_cli())
