"""
scripts/collectors/ocp_collector.py

Fetches namespace quota, HPA status, and deployment resource limits from OCP.

Two modes:
  "cli"     — runs `oc get ... -o json` as subprocess
  "ocp_mcp" — calls kubernetes MCP server VS Code started

COMMON REASONS FOR 0 HPAs / empty results:
  1. Not logged in:  run  oc login https://your-cluster --token=...
  2. Wrong namespace: oc project alprc-prod
  3. No HPAs exist in that namespace: oc get hpa -n alprc-prod
  4. oc not in PATH on Windows — use full path or add to PATH
"""

import asyncio
import json
import logging
import shutil
import os
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

log = logging.getLogger(__name__)


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
        return round((self.max_replicas - self.current_replicas) / max(self.max_replicas, 1) * 100, 1)

    @property
    def utilization_percent(self) -> float:
        return round(self.current_replicas / max(self.max_replicas, 1) * 100, 1)

    @property
    def is_at_risk(self) -> bool:
        return self.utilization_percent >= 80.0


@dataclass
class NamespaceQuota:
    namespace: str
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
    Collect namespace quota + HPA status from OCP/Kubernetes.

    Args:
        mode:       "cli" | "ocp_mcp"
        mcp_client: injected by capacity_agent when mode="ocp_mcp"
        kubeconfig: optional explicit path to kubeconfig file
        oc_path:    full path to oc binary if not in PATH
                    e.g. r"C:\\Users\\ABC\\bin\\oc.exe"
    """

    def __init__(
        self,
        mode:       str  = "cli",
        mcp_client         = None,
        kubeconfig: str  = None,
        oc_path:    str  = None,
        timeout:    int  = 30,
    ):
        self.mode       = mode
        self.mcp_client = mcp_client
        self.kubeconfig = kubeconfig or os.getenv("KUBECONFIG")
        self.timeout    = timeout

        # Resolve oc binary path
        if oc_path:
            self.oc_bin = oc_path
        else:
            # Try env var, then PATH
            self.oc_bin = os.getenv("OC_PATH") or shutil.which("oc") or "oc"

        log.info(f"[OCP] mode={mode}  oc_bin={self.oc_bin}  kubeconfig={self.kubeconfig}")

    # ── public ───────────────────────────────────────────────────────

    async def collect_all(self, namespace: str) -> dict:
        log.info(f"[OCP] collect_all: namespace={namespace}, mode={self.mode}")
        quota, hpa_list, deployments = await asyncio.gather(
            self.get_namespace_quota(namespace),
            self.get_all_hpa(namespace),
            self.get_deployment_resources(namespace),
        )
        log.info(f"[OCP] Collected: quota={'ok' if quota else 'none'}  "
                 f"HPAs={len(hpa_list)}  deployments={len(deployments)}")
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

    # ── CLI backend ───────────────────────────────────────────────────

    async def _oc(self, *args) -> Optional[dict]:
        """
        Run an oc command and return parsed JSON.
        Returns None on failure (with detailed error log) so callers can
        degrade gracefully instead of crashing the whole pipeline.
        """
        cmd = [self.oc_bin] + list(args) + ["-o", "json"]
        if self.kubeconfig:
            cmd = [self.oc_bin, f"--kubeconfig={self.kubeconfig}"] + list(args) + ["-o", "json"]

        log.debug(f"[OCP] Running: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except FileNotFoundError:
            log.error(
                f"[OCP] '{self.oc_bin}' not found.\n"
                f"  Fix 1: Add oc to PATH\n"
                f"  Fix 2: Set OC_PATH=C:\\path\\to\\oc.exe in your environment\n"
                f"  Fix 3: Pass oc_path= to OCPCollector()"
            )
            return None
        except asyncio.TimeoutError:
            log.error(f"[OCP] Command timed out after {self.timeout}s: {' '.join(cmd)}")
            return None

        if proc.returncode != 0:
            err = stderr.decode().strip()
            # Decode common oc error messages
            if "unauthorized" in err.lower() or "401" in err:
                log.error(
                    f"[OCP] Not logged in to cluster.\n"
                    f"  Run: oc login https://your-ocp-api --token=<your-token>\n"
                    f"  Or:  oc login https://your-ocp-api -u <user> -p <pass>"
                )
            elif "not found" in err.lower() or "no resources found" in err.lower():
                log.warning(f"[OCP] No resources found for: {' '.join(args)}")
                # Return empty list — not an error, namespace just has no HPAs etc.
                return {"items": []}
            elif "forbidden" in err.lower() or "403" in err:
                log.error(
                    f"[OCP] Permission denied for namespace '{args[-1] if args else '?'}'.\n"
                    f"  Check your service account has view access to this namespace."
                )
            else:
                log.error(f"[OCP] Command failed (rc={proc.returncode}): {err[:400]}")
            return None

        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError as e:
            log.error(f"[OCP] Could not parse JSON output: {e}")
            return None

    async def _cli_get_quota(self, namespace: str) -> NamespaceQuota:
        data = await self._oc("get", "resourcequota", "-n", namespace)
        if data is None:
            log.warning(f"[OCP] No quota data for namespace '{namespace}' — using defaults")
            return NamespaceQuota(namespace, None, None, None, None, None, None)
        items = data.get("items", [data] if data.get("status") else [])
        return self._parse_quota(namespace, items)

    async def _cli_get_hpa(self, namespace: str) -> list[HPAStatus]:
        data = await self._oc("get", "hpa", "-n", namespace)
        if data is None:
            return []
        items = data.get("items", [])
        if not items:
            log.warning(
                f"[OCP] No HPAs found in namespace '{namespace}'.\n"
                f"  Verify: oc get hpa -n {namespace}\n"
                f"  If HPAs exist, check oc login is current."
            )
        return self._parse_hpa_items(namespace, items)

    async def _cli_get_deployments(self, namespace: str) -> dict:
        data = await self._oc("get", "deployments", "-n", namespace)
        if data is None:
            return {}
        return self._parse_deployment_items(data.get("items", []))

    # ── OCP MCP backend ───────────────────────────────────────────────

    async def _mcp_get_quota(self, namespace: str) -> NamespaceQuota:
        raw = await self.mcp_client.call_tool(
            server="kubernetes", tool="list_resources",
            args={"kind": "ResourceQuota", "namespace": namespace},
        )
        items = raw.get("items", [])
        return self._parse_quota(namespace, items)

    async def _mcp_get_hpa(self, namespace: str) -> list[HPAStatus]:
        raw = await self.mcp_client.call_tool(
            server="kubernetes", tool="list_resources",
            args={"kind": "HorizontalPodAutoscaler", "namespace": namespace},
        )
        return self._parse_hpa_items(namespace, raw.get("items", []))

    async def _mcp_get_deployments(self, namespace: str) -> dict:
        raw = await self.mcp_client.call_tool(
            server="kubernetes", tool="list_resources",
            args={"kind": "Deployment", "namespace": namespace},
        )
        return self._parse_deployment_items(raw.get("items", []))

    # ── parsers ───────────────────────────────────────────────────────

    def _parse_quota(self, namespace: str, items: list) -> NamespaceQuota:
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
        return NamespaceQuota(namespace, cpu_hard, cpu_used, mem_hard, mem_used,
                              pods_hard, pods_used)

    def _parse_hpa_items(self, namespace: str, items: list) -> list[HPAStatus]:
        out = []
        for item in items:
            name   = item["metadata"]["name"]
            spec   = item.get("spec", {})
            status = item.get("status", {})
            max_r  = int(spec.get("maxReplicas", 1))
            out.append(HPAStatus(
                name=name, namespace=namespace,
                current_replicas=int(status.get("currentReplicas", 0)),
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
            cpu_lim = sum(_cpu(c.get("resources", {}).get("limits", {}).get("cpu",    "0")) for c in containers)
            mem_lim = sum(_mem(c.get("resources", {}).get("limits", {}).get("memory", "0")) for c in containers)
            out[name] = {"cpu_limit_cores": round(cpu_lim, 3), "memory_limit_bytes": mem_lim}
        return out


# ── unit helpers ────────────────────────────────────────────────────────

def _cpu(v) -> float:
    if not v: return 0.0
    v = str(v).strip()
    return float(v[:-1]) / 1000 if v.endswith("m") else float(v)

def _mem(v) -> float:
    if not v: return 0.0
    v = str(v).strip()
    for s, m in [("Ti",1024**4),("Gi",1024**3),("Mi",1024**2),("Ki",1024),
                 ("T",1e12),("G",1e9),("M",1e6),("K",1e3)]:
        if v.endswith(s):
            return float(v[:-len(s)]) * m
    return float(v)


# ── CLI smoke-test ───────────────────────────────────────────────────────

async def _cli():
    import argparse
    p = argparse.ArgumentParser(description="OCP Collector — oc CLI test")
    p.add_argument("--namespace", default="alprc-prod")
    p.add_argument("--mode",      default="cli", choices=["cli","ocp_mcp"])
    p.add_argument("--oc-path",   default=os.getenv("OC_PATH", ""))
    p.add_argument("--debug",     action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(name)-40s %(levelname)s  %(message)s",
    )

    c    = OCPCollector(mode=args.mode, oc_path=args.oc_path or None)
    data = await c.collect_all(args.namespace)
    q    = data["quota"]

    print(f"\nNamespace  : {args.namespace}")
    if q.cpu_hard_cores:
        print(f"CPU        : {q.cpu_used_cores:.2f} / {q.cpu_hard_cores:.2f} cores  ({q.cpu_used_percent}%)")
        print(f"Memory     : {(q.memory_used_bytes or 0)/1e9:.1f} / {(q.memory_hard_bytes or 0)/1e9:.1f} GB  ({q.memory_used_percent}%)")
        print(f"Pods       : {q.pods_used} / {q.pods_hard}")
    else:
        print("Quota      : not found (no ResourceQuota in this namespace, or login required)")

    print(f"\nHPAs found : {len(data['hpa'])}")
    for name, hpa in data["hpa"].items():
        risk = "  ⚠ AT RISK" if hpa.is_at_risk else ""
        print(f"  {name:<40} {hpa.current_replicas}/{hpa.max_replicas} replicas  ({hpa.utilization_percent}%){risk}")

    print(f"\nDeployments: {len(data['deployments'])}")
    for name in list(data["deployments"])[:5]:
        print(f"  {name}")

if __name__ == "__main__":
    asyncio.run(_cli())
