"""
scripts/health_check.py

Run this BEFORE capacity_agent.py to verify all three systems are reachable
and credentials are valid.

Usage:
    python scripts/health_check.py
    python scripts/health_check.py --namespace alprc-prod --debug

What it checks:
  Grafana   /api/health            → server alive?
            /api/user              → token valid?
            /api/datasources       → Prometheus datasource exists?
            /api/ds/query (1 pt)   → can actually run a PromQL?

  Splunk    /services/server/info  → server alive?
            /services/auth/login   → token valid?
            export search (1 row)  → can actually run a search?

  OCP       oc whoami              → logged in?
            oc project <ns>        → namespace accessible?
            oc get hpa -n <ns>     → can read HPAs?
            oc get resourcequota   → can read quota?

Exit code:  0 = all green,  1 = one or more checks failed
"""

import asyncio
import httpx
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("health_check")

# ── result model ──────────────────────────────────────────────────────

CHECK_PASS = "✅ PASS"
CHECK_FAIL = "❌ FAIL"
CHECK_WARN = "⚠️  WARN"
CHECK_SKIP = "⏭  SKIP"

@dataclass
class CheckResult:
    name:    str
    status:  str        # PASS / FAIL / WARN / SKIP
    detail:  str = ""
    fix:     str = ""

    def passed(self) -> bool:
        return self.status == "PASS"

    def __str__(self):
        icon = {"PASS": CHECK_PASS, "FAIL": CHECK_FAIL,
                "WARN": CHECK_WARN, "SKIP": CHECK_SKIP}[self.status]
        line = f"  {icon}  {self.name:<45} {self.detail}"
        if self.status == "FAIL" and self.fix:
            line += f"\n          FIX → {self.fix}"
        return line


# ══════════════════════════════════════════════════════════════════════
# Grafana checks
# ══════════════════════════════════════════════════════════════════════

async def check_grafana(
    base_url: str,
    token:    str,
    namespace: str,
    timeout:  int = 10,
) -> list[CheckResult]:
    results = []

    def _hdrs():
        h = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async with httpx.AsyncClient(timeout=timeout) as client:

        # 1 — /api/health
        try:
            r = await client.get(f"{base_url}/api/health", headers=_hdrs())
            if r.status_code == 200:
                db = r.json().get("database", "unknown")
                results.append(CheckResult("Grafana: server reachable", "PASS",
                                           f"database={db}"))
            else:
                results.append(CheckResult("Grafana: server reachable", "FAIL",
                                           f"HTTP {r.status_code}",
                                           f"Check GRAFANA_URL={base_url}"))
                return results   # no point continuing
        except httpx.ConnectError as e:
            results.append(CheckResult("Grafana: server reachable", "FAIL",
                                       str(e)[:80],
                                       f"Check GRAFANA_URL={base_url} is correct and reachable"))
            return results
        except httpx.InvalidURL as e:
            results.append(CheckResult("Grafana: server reachable", "FAIL",
                                       f"Invalid URL: {e}",
                                       "GRAFANA_URL must be https://host[:port] — no path after port"))
            return results

        # 2 — /api/user (token validity)
        try:
            r = await client.get(f"{base_url}/api/user", headers=_hdrs())
            if r.status_code == 200:
                user  = r.json()
                login = user.get("login", "?")
                role  = user.get("orgRole", "?")
                results.append(CheckResult("Grafana: token valid", "PASS",
                                           f"login={login}  role={role}"))
            elif r.status_code == 401:
                results.append(CheckResult("Grafana: token valid", "FAIL",
                                           "401 Unauthorized",
                                           "GRAFANA_SERVICE_ACCOUNT_TOKEN is missing or expired"))
            elif r.status_code == 403:
                results.append(CheckResult("Grafana: token valid", "FAIL",
                                           "403 Forbidden — token lacks permissions",
                                           "Service account needs Viewer role minimum"))
            else:
                results.append(CheckResult("Grafana: token valid", "WARN",
                                           f"HTTP {r.status_code}: {r.text[:80]}"))
        except Exception as e:
            results.append(CheckResult("Grafana: token valid", "WARN", str(e)[:80]))

        # 3 — /api/datasources (find Prometheus)
        ds_uid = ""
        try:
            r = await client.get(f"{base_url}/api/datasources", headers=_hdrs())
            if r.status_code == 200:
                datasources = r.json()
                prom_ds = [ds for ds in datasources if ds.get("type") == "prometheus"]
                if prom_ds:
                    ds       = prom_ds[0]
                    ds_uid   = ds.get("uid", "")
                    ds_name  = ds.get("name", "?")
                    ds_url   = ds.get("url", "?")
                    all_names = [d.get("name") for d in datasources]
                    results.append(CheckResult("Grafana: Prometheus datasource", "PASS",
                                               f"name='{ds_name}'  uid={ds_uid}  url={ds_url}"))
                    log.info(f"[Health] All datasources: {all_names}")
                    # Print hint for env var
                    log.info(f"[Health] Set GRAFANA_DS_UID={ds_uid} to skip auto-discovery")
                else:
                    names = [d.get("name") for d in datasources]
                    results.append(CheckResult("Grafana: Prometheus datasource", "FAIL",
                                               f"No Prometheus datasource found. Available: {names}",
                                               "Add a Prometheus datasource in Grafana → Connections → Data sources"))
            else:
                results.append(CheckResult("Grafana: Prometheus datasource", "WARN",
                                           f"HTTP {r.status_code} from /api/datasources"))
        except Exception as e:
            results.append(CheckResult("Grafana: Prometheus datasource", "WARN", str(e)[:80]))

        # 4 — /api/ds/query test (run a simple PromQL)
        try:
            now   = datetime.now(timezone.utc)
            start = now - timedelta(minutes=5)
            body  = {
                "from":  str(int(start.timestamp() * 1000)),
                "to":    str(int(now.timestamp() * 1000)),
                "queries": [{
                    "refId": "A",
                    "datasource": {"type": "prometheus"},
                    "expr": "up",
                    "range": True, "instant": False,
                    "intervalMs": 60000, "maxDataPoints": 10,
                }],
            }
            r = await client.post(f"{base_url}/api/ds/query",
                                  headers={**_hdrs(), "Content-Type": "application/json"},
                                  json=body)
            if r.status_code == 200:
                frames = (r.json().get("results", {}).get("A", {}).get("frames", []))
                results.append(CheckResult("Grafana: PromQL execution (up)", "PASS",
                                           f"{len(frames)} frame(s) returned"))
            elif r.status_code == 404 and ds_uid:
                # Fallback to proxy
                proxy_url = f"{base_url}/api/datasources/proxy/uid/{ds_uid}/api/v1/query"
                r2 = await client.get(proxy_url, headers=_hdrs(), params={"query": "up"})
                if r2.status_code == 200:
                    count = len(r2.json().get("data", {}).get("result", []))
                    results.append(CheckResult("Grafana: PromQL via proxy (up)", "PASS",
                                               f"{count} time series returned"))
                else:
                    results.append(CheckResult("Grafana: PromQL execution", "FAIL",
                                               f"proxy HTTP {r2.status_code}: {r2.text[:120]}"))
            else:
                results.append(CheckResult("Grafana: PromQL execution", "FAIL",
                                           f"HTTP {r.status_code}: {r.text[:120]}"))
        except Exception as e:
            results.append(CheckResult("Grafana: PromQL execution", "FAIL", str(e)[:80]))

    return results


# ══════════════════════════════════════════════════════════════════════
# Splunk checks
# ══════════════════════════════════════════════════════════════════════

async def check_splunk(
    base_url:   str,
    token:      str,
    splunk_app: str,
    splunk_owner: str = "nobody",
    verify_ssl: bool = False,
    timeout:    int  = 15,
) -> list[CheckResult]:
    results = []

    def _hdrs():
        h = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:

        # 1 — /services/server/info
        try:
            r = await client.get(f"{base_url}/services/server/info",
                                 headers=_hdrs(), params={"output_mode": "json"})
            if r.status_code == 200:
                entry   = r.json().get("entry", [{}])[0].get("content", {})
                version = entry.get("version", "?")
                product = entry.get("product_type", "?")
                results.append(CheckResult("Splunk: server reachable", "PASS",
                                           f"version={version}  product={product}"))
            elif r.status_code == 401:
                results.append(CheckResult("Splunk: server reachable", "FAIL",
                                           "401 — server alive but token not accepted here",
                                           "Try /services/auth/current (some Splunk Cloud versions differ)"))
                # server IS reachable — continue checks
            else:
                results.append(CheckResult("Splunk: server reachable", "FAIL",
                                           f"HTTP {r.status_code}"))
                return results
        except httpx.ConnectError as e:
            results.append(CheckResult("Splunk: server reachable", "FAIL", str(e)[:80],
                                       f"Check SPLUNK_HOST={base_url}"))
            return results
        except httpx.InvalidURL as e:
            results.append(CheckResult("Splunk: server reachable", "FAIL",
                                       f"InvalidURL: {e}",
                                       "SPLUNK_HOST must be https://host:port — no path after port"))
            return results

        # 2 — token validity via /services/authentication/current-context
        try:
            r = await client.get(
                f"{base_url}/services/authentication/current-context",
                headers=_hdrs(), params={"output_mode": "json"},
            )
            if r.status_code == 200:
                content  = r.json().get("entry", [{}])[0].get("content", {})
                username = content.get("username", "?")
                roles    = content.get("roles", [])
                results.append(CheckResult("Splunk: token valid", "PASS",
                                           f"username={username}  roles={roles}"))
            elif r.status_code == 401:
                results.append(CheckResult("Splunk: token valid", "FAIL",
                                           "401 — SPLUNK_TOKEN invalid or expired",
                                           "Generate a new token: Splunk Web → Settings → Tokens"))
            else:
                results.append(CheckResult("Splunk: token valid", "WARN",
                                           f"HTTP {r.status_code} (token may still work for searches)"))
        except Exception as e:
            results.append(CheckResult("Splunk: token valid", "WARN", str(e)[:80]))

        # 3 — app exists
        try:
            r = await client.get(
                f"{base_url}/servicesNS/{splunk_owner}/{splunk_app}/apps/local/{splunk_app}",
                headers=_hdrs(), params={"output_mode": "json"},
            )
            if r.status_code == 200:
                content   = r.json().get("entry", [{}])[0].get("content", {})
                label     = content.get("label", splunk_app)
                disabled  = content.get("disabled", False)
                status    = "WARN" if disabled else "PASS"
                results.append(CheckResult(f"Splunk: app '{splunk_app}' exists", status,
                                           f"label='{label}'  disabled={disabled}"))
            elif r.status_code == 404:
                results.append(CheckResult(f"Splunk: app '{splunk_app}' exists", "FAIL",
                                           f"App not found",
                                           f"Check SPLUNK_APP={splunk_app} — run: curl {base_url}/services/apps/local"))
            else:
                results.append(CheckResult(f"Splunk: app '{splunk_app}' exists", "WARN",
                                           f"HTTP {r.status_code}"))
        except Exception as e:
            results.append(CheckResult(f"Splunk: app '{splunk_app}' exists", "WARN", str(e)[:80]))

        # 4 — test export search (1 result, last 5 min)
        try:
            export_url = (
                f"{base_url}/servicesNS/{splunk_owner}/{splunk_app}/search/jobs/export"
            )
            r = await client.post(
                export_url,
                headers=_hdrs(),
                data={
                    "search":        "search index=* | head 1",
                    "earliest_time": "-5m",
                    "latest_time":   "now",
                    "output_mode":   "json",
                    "search_mode":   "normal",
                },
            )
            if r.status_code == 200:
                lines = [l for l in r.text.splitlines() if l.strip()]
                results.append(CheckResult("Splunk: export search works", "PASS",
                                           f"{len(lines)} lines in response"))
            elif r.status_code == 401:
                results.append(CheckResult("Splunk: export search works", "FAIL",
                                           "401 — token rejected by export endpoint"))
            elif r.status_code == 403:
                results.append(CheckResult("Splunk: export search works", "FAIL",
                                           f"403 Forbidden — check token has search capability in app '{splunk_app}'"))
            else:
                results.append(CheckResult("Splunk: export search works", "FAIL",
                                           f"HTTP {r.status_code}: {r.text[:150]}"))
        except Exception as e:
            results.append(CheckResult("Splunk: export search works", "FAIL", str(e)[:80]))

    return results


# ══════════════════════════════════════════════════════════════════════
# OCP checks
# ══════════════════════════════════════════════════════════════════════

def _run_oc(*args, oc_bin: str = "oc", timeout: int = 15) -> tuple[int, str, str]:
    """Run oc command synchronously, return (returncode, stdout, stderr)."""
    cmd = [oc_bin] + list(args)
    log.debug(f"[OCP] Running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return -1, "", f"'{oc_bin}' not found in PATH"
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"


def check_ocp(namespace: str, oc_bin: str = "oc") -> list[CheckResult]:
    results = []

    # 1 — oc in PATH
    oc_path = shutil.which(oc_bin) or os.getenv("OC_PATH", "")
    if oc_path:
        results.append(CheckResult("OCP: oc binary found", "PASS", f"path={oc_path}"))
        oc_bin = oc_path
    else:
        results.append(CheckResult("OCP: oc binary found", "FAIL",
                                   f"'{oc_bin}' not in PATH",
                                   "Install oc CLI or set OC_PATH=C:\\path\\to\\oc.exe"))
        return results   # can't do any oc checks

    # 2 — oc whoami (login check)
    rc, stdout, stderr = _run_oc("whoami", oc_bin=oc_bin)
    if rc == 0:
        results.append(CheckResult("OCP: logged in", "PASS", f"user={stdout}"))
    else:
        hint = stderr
        if "unauthorized" in stderr.lower() or "login" in stderr.lower():
            hint = "Run: oc login https://your-ocp-api --token=<token>"
        elif "expired" in stderr.lower():
            hint = "Token expired — run: oc login again"
        results.append(CheckResult("OCP: logged in", "FAIL", stderr[:80], hint))
        return results   # no point checking namespace if not logged in

    # 3 — oc project / namespace access
    rc, stdout, stderr = _run_oc("project", namespace, oc_bin=oc_bin)
    if rc == 0:
        results.append(CheckResult(f"OCP: namespace '{namespace}' accessible", "PASS",
                                   stdout[:80]))
    else:
        results.append(CheckResult(f"OCP: namespace '{namespace}' accessible", "FAIL",
                                   stderr[:80],
                                   f"Run: oc get projects | grep {namespace}"))

    # 4 — oc get hpa
    rc, stdout, stderr = _run_oc("get", "hpa", "-n", namespace, oc_bin=oc_bin)
    if rc == 0:
        lines = [l for l in stdout.splitlines() if l.strip() and not l.startswith("NAME")]
        count = len(lines)
        if count > 0:
            results.append(CheckResult(f"OCP: HPAs in '{namespace}'", "PASS",
                                       f"{count} HPA(s) found: {[l.split()[0] for l in lines]}"))
        else:
            results.append(CheckResult(f"OCP: HPAs in '{namespace}'", "WARN",
                                       "0 HPAs found — collector will return empty HPA data"))
    else:
        if "no resources found" in stderr.lower():
            results.append(CheckResult(f"OCP: HPAs in '{namespace}'", "WARN",
                                       "No HPAs exist in this namespace"))
        else:
            results.append(CheckResult(f"OCP: HPAs in '{namespace}'", "FAIL",
                                       stderr[:80],
                                       "Check namespace name and RBAC permissions"))

    # 5 — oc get resourcequota
    rc, stdout, stderr = _run_oc("get", "resourcequota", "-n", namespace, oc_bin=oc_bin)
    if rc == 0:
        lines = [l for l in stdout.splitlines() if l.strip() and not l.startswith("NAME")]
        count = len(lines)
        if count > 0:
            results.append(CheckResult(f"OCP: ResourceQuota in '{namespace}'", "PASS",
                                       f"{count} quota object(s)"))
        else:
            results.append(CheckResult(f"OCP: ResourceQuota in '{namespace}'", "WARN",
                                       "No ResourceQuota — collector will use defaults (16 CPU, 32 GB)"))
    else:
        if "no resources found" in stderr.lower():
            results.append(CheckResult(f"OCP: ResourceQuota in '{namespace}'", "WARN",
                                       "No ResourceQuota — collector will use defaults"))
        else:
            results.append(CheckResult(f"OCP: ResourceQuota in '{namespace}'", "FAIL",
                                       stderr[:80]))

    # 6 — oc get deployments
    rc, stdout, stderr = _run_oc("get", "deployments", "-n", namespace, oc_bin=oc_bin)
    if rc == 0:
        lines = [l for l in stdout.splitlines() if l.strip() and not l.startswith("NAME")]
        count = len(lines)
        results.append(CheckResult(f"OCP: Deployments in '{namespace}'", "PASS",
                                   f"{count} deployment(s) found"))
    else:
        results.append(CheckResult(f"OCP: Deployments in '{namespace}'", "FAIL",
                                   stderr[:80]))

    return results


# ══════════════════════════════════════════════════════════════════════
# Main runner
# ══════════════════════════════════════════════════════════════════════

async def run_all_checks(
    grafana_url:   str,
    grafana_token: str,
    splunk_url:    str,
    splunk_token:  str,
    splunk_app:    str,
    splunk_owner:  str,
    namespace:     str,
    oc_bin:        str = "oc",
    verify_ssl:    bool = False,
) -> bool:
    """Run all health checks. Returns True if all critical checks pass."""

    print(f"\n{'='*65}")
    print(f"  SRE Capacity Agent — Health Check")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}\n")

    all_results: list[CheckResult] = []

    # Grafana
    print("── Grafana / Prometheus ─────────────────────────────────────")
    grafana_results = await check_grafana(grafana_url, grafana_token, namespace)
    for r in grafana_results:
        print(r)
    all_results.extend(grafana_results)

    # Splunk
    print("\n── Splunk ───────────────────────────────────────────────────")
    splunk_results = await check_splunk(
        splunk_url, splunk_token, splunk_app, splunk_owner, verify_ssl
    )
    for r in splunk_results:
        print(r)
    all_results.extend(splunk_results)

    # OCP
    print("\n── OCP / Kubernetes ─────────────────────────────────────────")
    ocp_results = check_ocp(namespace, oc_bin=oc_bin)
    for r in ocp_results:
        print(r)
    all_results.extend(ocp_results)

    # Summary
    passed = [r for r in all_results if r.status == "PASS"]
    warned = [r for r in all_results if r.status == "WARN"]
    failed = [r for r in all_results if r.status == "FAIL"]

    print(f"\n{'='*65}")
    print(f"  SUMMARY:  {len(passed)} passed  |  {len(warned)} warnings  |  {len(failed)} failed")
    print(f"{'='*65}")

    if failed:
        print(f"\n{CHECK_FAIL} FAILED CHECKS:")
        for r in failed:
            print(f"  • {r.name}")
            if r.fix:
                print(f"    Fix: {r.fix}")
        print(f"\nFix the above before running capacity_agent.py\n")
        return False

    if warned:
        print(f"\n{CHECK_WARN} WARNINGS (non-blocking):")
        for r in warned:
            print(f"  • {r.name}: {r.detail}")

    print(f"\n{CHECK_PASS} All critical checks passed — safe to run capacity_agent.py\n")
    return True


# ── CLI ─────────────────────────────────────────────────────────────────

async def main():
    import argparse
    p = argparse.ArgumentParser(description="SRE Agent health check")
    p.add_argument("--namespace",     default=os.getenv("OCP_NAMESPACE",  "alprc-prod"))
    p.add_argument("--grafana-url",   default=os.getenv("GRAFANA_URL",    "http://localhost:3000"))
    p.add_argument("--grafana-token", default=os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN", ""))
    p.add_argument("--splunk-url",    default=os.getenv("SPLUNK_HOST",    "https://localhost:8089"))
    p.add_argument("--splunk-token",  default=os.getenv("SPLUNK_TOKEN",   ""))
    p.add_argument("--splunk-app",    default=os.getenv("SPLUNK_APP",     "search"))
    p.add_argument("--splunk-owner",  default=os.getenv("SPLUNK_OWNER",   "nobody"))
    p.add_argument("--oc-path",       default=os.getenv("OC_PATH",        "oc"))
    p.add_argument("--no-ssl-verify", action="store_true")
    p.add_argument("--debug",         action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    ok = await run_all_checks(
        grafana_url   = args.grafana_url,
        grafana_token = args.grafana_token,
        splunk_url    = args.splunk_url,
        splunk_token  = args.splunk_token,
        splunk_app    = args.splunk_app,
        splunk_owner  = args.splunk_owner,
        namespace     = args.namespace,
        oc_bin        = args.oc_path,
        verify_ssl    = not args.no_ssl_verify,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
