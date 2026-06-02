"""
agent/capacity_agent.py

Main orchestrator.  Two usage patterns:

1. Standalone CLI (collectors talk to systems directly via HTTP / oc CLI):
   python agent/capacity_agent.py --namespace payments-prod --dry-run

2. Inside VS Code GitHub Copilot Agent (MCP mode — collectors call MCP tools):
   The MCPClient class below wraps the injected mcp context so every
   collector calls mcp_client.call_tool(server, tool, args) instead of
   making its own network calls.  VS Code starts all three MCP servers
   from .vscode/mcp.json before this script runs.

Pipeline (same in both modes):
  Step 1  collect  — Prometheus + Splunk + OCP in parallel
  Step 2  predict  — linear regression, exhaustion dates
  Step 3  snapshot — save JSON to data/snapshots/
  Step 4  LLM      — interpret + recommend  (skipped with --dry-run)
  Step 5  report   — save JSON to data/reports/
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── logging setup (call configure_logging() before anything else) ───────

def configure_logging(debug: bool = False):
    """
    Set up rich logging for the whole agent pipeline.
    DEBUG  → every HTTP call, every query, every parse step
    INFO   → collection progress, prediction results, step banners
    """
    level  = logging.DEBUG if debug else logging.INFO
    fmt    = "%(asctime)s.%(msecs)03d  %(name)-42s %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)
    # Quiet noisy third-party libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.INFO)

log = logging.getLogger("__main__")

from scripts.collectors.prometheus_collector import PrometheusCollector
from scripts.collectors.splunk_collector     import SplunkCollector
from scripts.collectors.ocp_collector        import OCPCollector
from scripts.calculators.capacity_predictor  import CapacityPredictor
from scripts.calculators.hpa_analyzer        import HPAAnalyzer
from scripts.reporters.snapshot_store        import SnapshotStore
from agent.lm_client                         import LMClient
from agent.skill_loader                      import SkillLoader


# ── config (override with env vars) ────────────────────────────────────

PROMETHEUS_URL    = os.getenv("GRAFANA_URL",                     "http://localhost:3000")
GRAFANA_TOKEN     = os.getenv("GRAFANA_TOKEN",               "") \
                 or os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN","")
GRAFANA_DS_UID    = os.getenv("GRAFANA_DS_UID",               "")   # e.g. hyaV9HiVz
PROMETHEUS_DIRECT = os.getenv("PROMETHEUS_DIRECT",            "false")
SPLUNK_URL     = os.getenv("SPLUNK_HOST",    "https://localhost:8089")
SPLUNK_TOKEN   = os.getenv("SPLUNK_TOKEN",   "")
SPLUNK_APP     = os.getenv("SPLUNK_APP",     "search")   # e.g. wf_ui_app_ctdlr
SPLUNK_OWNER   = os.getenv("SPLUNK_OWNER",   "nobody")
PROM_MODE      = os.getenv("PROM_MODE",      "http")     # "http" | "grafana_mcp"
SPLUNK_MODE    = os.getenv("SPLUNK_MODE",    "http")     # "http" | "splunk_mcp"
OCP_MODE       = os.getenv("OCP_MODE",       "cli")      # "cli"  | "ocp_mcp"


# ── MCP client shim (used when running inside VS Code Copilot) ──────────
# When GitHub Copilot calls this agent it can inject a real MCP context.
# For standalone use this class is never instantiated.

class MCPClient:
    """
    Thin wrapper around whatever MCP context VS Code provides.
    Replace the body of call_tool() to match the actual Copilot MCP API
    once Microsoft documents the Python interface.
    Currently documented API (JS): vscode.lm.callTool(name, input)
    """

    def __init__(self, context=None):
        self._context = context   # injected VS Code MCP context object

    async def call_tool(self, server: str, tool: str, args: dict) -> dict:
        """
        Route a tool call to the named MCP server.
        VS Code starts servers defined in .vscode/mcp.json.
        """
        if self._context:
            # Real VS Code context — adapt to actual API when available
            result = await self._context.call_tool(
                f"{server}/{tool}", arguments=args
            )
            return result
        # Fallback: not inside VS Code — return empty so callers degrade gracefully
        return {}


# ── agent ──────────────────────────────────────────────────────────────

class CapacityAgent:

    def __init__(
        self,
        prometheus_url:  str = PROMETHEUS_URL,
        grafana_token:   str = GRAFANA_TOKEN,
        grafana_ds_uid:  str = GRAFANA_DS_UID,
        splunk_url:      str = SPLUNK_URL,
        splunk_token:    str = SPLUNK_TOKEN,
        splunk_app:      str = SPLUNK_APP,
        splunk_owner:    str = SPLUNK_OWNER,
        ocp_cluster:     str = None,
        ocp_token:       str = None,
        ocp_mcp_server:  str = "ocp-mcp",
        prom_mode:       str = PROM_MODE,
        splunk_mode:     str = SPLUNK_MODE,
        ocp_mode:        str = OCP_MODE,
        mcp_client             = None,
    ):
        # Build collectors — inject mcp_client when in MCP mode
        self.prom_collector = PrometheusCollector(
            base_url=prometheus_url,
            token=grafana_token,
            ds_uid=grafana_ds_uid,
            mode=prom_mode,
            mcp_client=mcp_client,
        )
        self.splunk_collector = SplunkCollector(
            base_url=splunk_url,
            token=splunk_token,
            splunk_app=splunk_app,
            splunk_owner=splunk_owner,
            mode=splunk_mode,
            mcp_client=mcp_client,
        )
        self.ocp_collector = OCPCollector(
            cluster=ocp_cluster    or os.getenv("OCP_API_URL", ""),
            token=ocp_token        or os.getenv("OCP_TOKEN",   ""),
            mode=ocp_mode,
            mcp_client=mcp_client,
            mcp_server=ocp_mcp_server,
            verify_ssl=os.getenv("OCP_VERIFY_SSL","false").lower() != "true",
        )

        self.predictor  = CapacityPredictor()
        self.hpa        = HPAAnalyzer()
        self.store      = SnapshotStore()
        self.lm         = LMClient()
        self.skills     = SkillLoader(ROOT / "skills")

    # ── main entry point ───────────────────────────────────────────────

    async def run(
        self,
        namespace:    str,
        services:     list[str] = None,
        lookback_days: int = 30,
        dry_run:      bool = False,
    ) -> dict:

        _h("Capacity Analysis: " + namespace)

        # ── 1. Collect ────────────────────────────────────────────────
        _step("1", "Collecting metrics (Prometheus + Splunk + OCP in parallel)...")
        prom_data, splunk_data, ocp_data = await asyncio.gather(
            self.prom_collector.collect_all(namespace, lookback_days),
            self.splunk_collector.collect_api_trends(namespace, lookback_days),
            self.ocp_collector.collect_all(namespace),
        )
        target_services = services or [k for k in prom_data if not k.startswith("__")]
        _ok(f"Prometheus: {len(target_services)} services | "
            f"Splunk: {len(splunk_data)} services | "
            f"OCP: {len(ocp_data['hpa'])} HPAs")

        # ── 2. Predict ────────────────────────────────────────────────
        _step("2", "Computing predictions...")
        ns_quota      = _extract_quota(ocp_data["quota"])
        all_preds     = []
        hpa_preds     = []
        svc_reports   = {}

        for svc in target_services:
            svc_prom   = prom_data.get(svc, {})
            api_calls  = splunk_data.get(svc, [])
            cpu_series = svc_prom.get("cpu",    {}).get("usage", [])
            mem_series = svc_prom.get("memory", {}).get("usage", [])
            cpu_limit  = svc_prom.get("cpu",    {}).get("limit") or \
                         ns_quota["cpu"] / max(len(target_services), 1)
            mem_limit  = svc_prom.get("memory", {}).get("limit") or \
                         ns_quota["memory"] / max(len(target_services), 1)

            cpu_pred = self.predictor.predict_resource(
                svc, "cpu",    namespace, cpu_series, cpu_limit, api_calls)
            mem_pred = self.predictor.predict_resource(
                svc, "memory", namespace, mem_series, mem_limit, api_calls)
            all_preds.extend([cpu_pred, mem_pred])

            hpa_obj  = ocp_data["hpa"].get(svc)
            hpa_pred = None
            if hpa_obj:
                hpa_pred = self.hpa.analyze(
                    service=svc, namespace=namespace,
                    current_replicas=hpa_obj.current_replicas,
                    desired_replicas=hpa_obj.desired_replicas,
                    max_replicas=hpa_obj.max_replicas,
                    min_replicas=hpa_obj.min_replicas,
                )
                hpa_preds.append(hpa_pred)

            svc_reports[svc] = {
                "cpu":                   cpu_pred.to_dict(),
                "memory":                mem_pred.to_dict(),
                "hpa":                   hpa_pred.to_dict() if hpa_pred else None,
                "api_growth_rate_per_day": _growth_rate(api_calls),
            }
            print(f"  {svc:30s}  CPU={cpu_pred.current_percent:5.1f}%({cpu_pred.days_to_exhaustion}d)"
                  f"  Mem={mem_pred.current_percent:5.1f}%({mem_pred.days_to_exhaustion}d)"
                  f"  [{cpu_pred.severity}/{mem_pred.severity}]")

        ns_summary = self.predictor.aggregate_namespace(
            all_preds, ns_quota,
            prom_data.get("__namespace__", {}).get("cpu",    {}).get("usage", []),
            prom_data.get("__namespace__", {}).get("memory", {}).get("usage", []),
        )
        print(f"\n  Namespace CPU={ns_summary.total_cpu_used_percent}%  "
              f"Mem={ns_summary.total_memory_used_percent}%  "
              f"Severity={ns_summary.overall_severity}")

        # ── 3. Snapshot ───────────────────────────────────────────────
        _step("3", "Saving snapshots...")
        self.store.save_predictions(all_preds, namespace)
        history = self.store.get_trend_history(namespace, target_services[0], "cpu", days=14) \
                  if target_services else []
        _ok(f"{len(all_preds)} predictions written to data/snapshots/")

        # ── 4. LLM ───────────────────────────────────────────────────
        full_report = {
            "generated_at":    datetime.utcnow().isoformat(),
            "namespace":       namespace,
            "services":        svc_reports,
            "namespace_summary": ns_summary.to_dict(),
            "snapshot_history":  history,
        }
        narrative = ""
        if not dry_run:
            _step("4", "Calling LLM for analysis...")
            skill    = self.skills.load("capacity/capacity_analysis.md")
            profiles = self.skills.load_service_profiles(target_services)
            prompt   = _build_prompt(skill, profiles, full_report)
            narrative = await self.lm.call(prompt)
            _ok("LLM narrative ready")
        else:
            _step("4", "Dry-run — skipping LLM")
            narrative = "[dry-run]"

        # ── 5. Report ─────────────────────────────────────────────────
        _step("5", "Saving report...")
        rpath = self.store.save_report(namespace, full_report, narrative)
        _ok(f"Saved → {rpath}")

        # ── Print summary ─────────────────────────────────────────────
        print("\n" + "=" * 60)
        if narrative and narrative != "[dry-run]":
            print(narrative)
        else:
            _print_table(svc_reports, ns_summary)

        return {**full_report, "llm_narrative": narrative}


# ── helpers ─────────────────────────────────────────────────────────────

def _extract_quota(quota) -> dict:
    return {
        "cpu":    quota.cpu_hard_cores    or 16.0,
        "memory": quota.memory_hard_bytes or 32e9,
    }

def _growth_rate(series: list) -> float:
    if not series or len(series) < 2:
        return 0.0
    import numpy as np
    from scipy import stats
    vals = [float(v) for _, v in series]
    if all(v == 0 for v in vals):
        return 0.0
    slope, *_ = stats.linregress(range(len(vals)), vals)
    avg = np.mean(vals) or 1
    return round(slope / avg * 100, 2)

def _build_prompt(skill, profiles, report) -> str:
    return (f"{skill}\n\n## Service Context\n{profiles}\n\n"
            f"## Prediction Data\n```json\n{json.dumps(report, indent=2)}\n```\n"
            "Provide analysis in the exact format from the skill.")

def _print_table(svc_reports, ns_summary):
    print(f"{'Service':<30} {'CPU%':>6} {'CPU days':>9} {'Mem%':>6} {'Mem days':>9} {'Severity'}")
    print("-" * 70)
    for svc, r in svc_reports.items():
        c, m = r["cpu"], r["memory"]
        sev  = f"{c['severity']}/{m['severity']}"
        print(f"{svc:<30} {c['current_percent']:>6.1f} {c['days_to_exhaustion']:>9} "
              f"{m['current_percent']:>6.1f} {m['days_to_exhaustion']:>9} {sev}")
    print(f"\nNamespace overall: {ns_summary.overall_severity}")

def _h(msg):   print(f"\n{'='*60}\n{msg}\n{'='*60}\n")
def _step(n, msg): print(f"\n📋 Step {n}: {msg}")
def _ok(msg):  print(f"   ✅ {msg}")


# ── CLI entry point ──────────────────────────────────────────────────────

async def main():
    import argparse
    p = argparse.ArgumentParser(description="SRE Capacity Agent")
    p.add_argument("--namespace",      default=os.getenv("OCP_NAMESPACE",  "alprc-prod"))
    p.add_argument("--service",        default=None)
    p.add_argument("--days",           type=int, default=30)
    p.add_argument("--dry-run",        action="store_true")
    p.add_argument("--skip-health",    action="store_true",
                   help="Skip pre-flight connectivity checks")
    p.add_argument("--debug",          action="store_true",
                   help="Enable DEBUG logging (very verbose)")
    # ── Grafana ───────────────────────────────────────────────────────
    p.add_argument("--grafana-url",    default=os.getenv("GRAFANA_URL",   PROMETHEUS_URL),
                   help="Grafana base URL e.g. https://prod1-grafana.wellsfargo.net")
    p.add_argument("--grafana-token",  default=os.getenv("GRAFANA_TOKEN", GRAFANA_TOKEN) or
                                               os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN",""),
                   help="Grafana service account token (glsa_... or GRAFANA_TOKEN env var)")
    p.add_argument("--grafana-ds-uid", default=os.getenv("GRAFANA_DS_UID", GRAFANA_DS_UID),
                   help="Prometheus datasource UID e.g. hyaV9HiVz")
    p.add_argument("--prom-mode",      default=os.getenv("PROM_MODE",     PROM_MODE))
    # ── Splunk ────────────────────────────────────────────────────────
    p.add_argument("--splunk-url",      default=os.getenv("SPLUNK_HOST",     SPLUNK_URL))
    p.add_argument("--splunk-token",    default=os.getenv("SPLUNK_TOKEN",    SPLUNK_TOKEN))
    p.add_argument("--splunk-app",      default=os.getenv("SPLUNK_APP",      SPLUNK_APP),
                   help="Splunk app ID e.g. wf_ui_app_ctdlr")
    p.add_argument("--splunk-owner",    default=os.getenv("SPLUNK_OWNER",    SPLUNK_OWNER))
    p.add_argument("--splunk-mode",     default=os.getenv("SPLUNK_MODE",     SPLUNK_MODE))
    # ── OCP ───────────────────────────────────────────────────────────
    p.add_argument("--ocp-cluster",     default=os.getenv("OCP_API_URL",     ""),
                   help="OCP API URL e.g. https://api.cluster.example.com:6443")
    p.add_argument("--ocp-token",       default=os.getenv("OCP_TOKEN",       ""),
                   help="OCP bearer token (oc whoami -t)")
    p.add_argument("--ocp-mcp-server",  default=os.getenv("OCP_MCP_SERVER",  "ocp-mcp"))
    p.add_argument("--ocp-mode",        default=os.getenv("OCP_MODE",        OCP_MODE))
    a = p.parse_args()

    # ── configure logging first ─────────────────────────────────────────
    configure_logging(debug=a.debug)
    log.info(f"SRE Capacity Agent starting  namespace={a.namespace}  days={a.days}  "
             f"dry_run={a.dry_run}  debug={a.debug}")

    # ── pre-flight health checks ────────────────────────────────────────
    if not a.skip_health and a.prom_mode == "http":
        from scripts.health_check import run_all_checks
        log.info("Running pre-flight health checks...")
        ok = await run_all_checks(
            grafana_url      = a.grafana_url,
            grafana_token    = a.grafana_token,
            grafana_ds_uid   = a.grafana_ds_uid,
            splunk_url       = a.splunk_url,
            splunk_token     = a.splunk_token,
            splunk_app       = a.splunk_app,
            splunk_owner     = a.splunk_owner,
            namespace        = a.namespace,
            oc_bin           = os.getenv("OC_PATH", "oc"),
            verify_ssl       = False,
        )
        if not ok:
            log.error("Health checks failed — fix the issues above then re-run.")
            log.error("To skip checks: add --skip-health flag")
            sys.exit(1)

    # ── run agent ───────────────────────────────────────────────────────
    agent = CapacityAgent(
        prometheus_url   = a.grafana_url,
        grafana_token    = a.grafana_token,
        grafana_ds_uid   = a.grafana_ds_uid,
        splunk_url       = a.splunk_url,
        splunk_token     = a.splunk_token,
        splunk_app       = a.splunk_app,
        splunk_owner     = a.splunk_owner,
        ocp_cluster      = a.ocp_cluster,
        ocp_token        = a.ocp_token,
        ocp_mcp_server   = a.ocp_mcp_server,
        prom_mode        = a.prom_mode,
        splunk_mode      = a.splunk_mode,
        ocp_mode         = a.ocp_mode,
    )
    services = [a.service] if a.service else None
    await agent.run(a.namespace, services, a.days, a.dry_run)

if __name__ == "__main__":
    asyncio.run(main())
