"""
agent/capacity_agent.py

Orchestrates the full capacity analysis pipeline:
  1. Start all 3 MCP servers (grafana-mcp, splunk-mcp, ocp-mcp)
  2. Collect metrics via MCP tool calls
  3. Run linear regression predictions
  4. Call LLM for interpretation
  5. Save report

Run:
    python agent/capacity_agent.py --namespace alprc-prod
    python agent/capacity_agent.py --namespace alprc-prod --dry-run
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

from agent.mcp_client                        import MCPClient
from agent.lm_client                         import LMClient
from agent.skill_loader                      import SkillLoader
from scripts.collectors.prometheus_collector import PrometheusCollector
from scripts.collectors.splunk_collector     import SplunkCollector
from scripts.collectors.ocp_collector        import OCPCollector
from scripts.calculators.capacity_predictor  import CapacityPredictor
from scripts.calculators.hpa_analyzer        import HPAAnalyzer
from scripts.reporters.snapshot_store        import SnapshotStore


def configure_logging(debug: bool = False):
    level  = logging.DEBUG if debug else logging.INFO
    fmt    = "%(asctime)s.%(msecs)03d  %(name)-38s %(levelname)-8s  %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("__main__")


async def run(
    namespace:   str,
    days:        int  = 30,
    dry_run:     bool = False,
    services:    list = None,
    debug:       bool = False,
):
    configure_logging(debug)
    log.info(f"Starting capacity analysis: namespace={namespace}  days={days}")

    async with MCPClient() as mcp:

        # ── Collectors ────────────────────────────────────────────────
        prom_c   = PrometheusCollector(mcp)
        splunk_c = SplunkCollector(mcp)
        ocp_c    = OCPCollector(mcp)

        # ── Step 1: Collect (all in parallel) ─────────────────────────
        log.info("Step 1: Collecting metrics...")
        prom_data, splunk_data, ocp_data = await asyncio.gather(
            prom_c.collect_all(namespace, days),
            splunk_c.collect_api_trends(namespace, days),
            ocp_c.collect_all(namespace),
        )

        target_svcs = services or [k for k in prom_data if not k.startswith("__")]
        log.info(
            f"  Prometheus: {len(target_svcs)} services  "
            f"Splunk: {len(splunk_data)} services  "
            f"OCP: {len(ocp_data['hpa'])} HPAs"
        )

        # ── Step 2: Predict ───────────────────────────────────────────
        log.info("Step 2: Computing predictions...")
        predictor = CapacityPredictor()
        hpa_an    = HPAAnalyzer()
        quota     = ocp_data["quota"]

        ns_quota = {
            "cpu":    quota.cpu_hard_cores    or 16.0,
            "memory": quota.memory_hard_bytes or 32e9,
        }

        all_preds   = []
        svc_reports = {}

        for svc in target_svcs:
            pd   = prom_data.get(svc, {})
            api  = splunk_data.get(svc, [])

            cpu_series = pd.get("cpu",    {}).get("usage", [])
            mem_series = pd.get("memory", {}).get("usage", [])
            cpu_limit  = pd.get("cpu",    {}).get("limit") or ns_quota["cpu"]    / max(len(target_svcs),1)
            mem_limit  = pd.get("memory", {}).get("limit") or ns_quota["memory"] / max(len(target_svcs),1)

            cpu_pred = predictor.predict_resource(svc, "cpu",    namespace, cpu_series, cpu_limit, api)
            mem_pred = predictor.predict_resource(svc, "memory", namespace, mem_series, mem_limit, api)
            all_preds.extend([cpu_pred, mem_pred])

            hpa_obj  = ocp_data["hpa"].get(svc)
            hpa_pred = hpa_an.analyze(
                service=svc, namespace=namespace,
                current_replicas=hpa_obj.current_replicas if hpa_obj else 0,
                desired_replicas=hpa_obj.desired_replicas if hpa_obj else 0,
                max_replicas=hpa_obj.max_replicas if hpa_obj else 1,
                min_replicas=hpa_obj.min_replicas if hpa_obj else 1,
            ) if hpa_obj else None

            svc_reports[svc] = {
                "cpu":    cpu_pred.to_dict(),
                "memory": mem_pred.to_dict(),
                "hpa":    hpa_pred.to_dict() if hpa_pred else None,
            }

            log.info(
                f"  {svc:<35} "
                f"CPU={cpu_pred.current_percent:.1f}%({cpu_pred.days_to_exhaustion}d) "
                f"Mem={mem_pred.current_percent:.1f}%({mem_pred.days_to_exhaustion}d) "
                f"[{cpu_pred.severity}/{mem_pred.severity}]"
            )

        ns_pred = predictor.aggregate_namespace(
            all_preds, ns_quota,
            prom_data.get("__namespace__", {}).get("cpu",    {}).get("usage", []),
            prom_data.get("__namespace__", {}).get("memory", {}).get("usage", []),
        )
        log.info(
            f"  Namespace: CPU={ns_pred.total_cpu_used_percent}%  "
            f"Mem={ns_pred.total_memory_used_percent}%  "
            f"Severity={ns_pred.overall_severity}"
        )

        # ── Step 3: Save snapshots ────────────────────────────────────
        log.info("Step 3: Saving snapshots...")
        store = SnapshotStore()
        store.save_predictions(all_preds, namespace)

        full_report = {
            "generated_at":    datetime.utcnow().isoformat(),
            "namespace":       namespace,
            "services":        svc_reports,
            "namespace_summary": ns_pred.to_dict(),
        }

        # ── Step 4: LLM ───────────────────────────────────────────────
        narrative = ""
        if not dry_run:
            log.info("Step 4: LLM analysis...")
            skills   = SkillLoader(ROOT / "skills")
            skill    = skills.load("capacity/capacity_analysis.md")
            profiles = skills.load_service_profiles(target_svcs)
            prompt   = f"{skill}\n\n## Service Context\n{profiles}\n\n## Data\n```json\n{json.dumps(full_report, indent=2)}\n```"
            narrative = await LMClient().call(prompt)
        else:
            log.info("Step 4: Skipped (--dry-run)")

        # ── Step 5: Report ────────────────────────────────────────────
        log.info("Step 5: Saving report...")
        report_path = store.save_report(namespace, full_report, narrative)
        log.info(f"  Saved: {report_path}")

        # ── Print ──────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        if narrative:
            print(narrative)
        else:
            _print_table(svc_reports, ns_pred)
        print(f"{'='*60}\n")

        return {**full_report, "llm_narrative": narrative}


def _print_table(svc_reports, ns_pred):
    print(f"\n{'Service':<35} {'CPU%':>6} {'CPU days':>9} {'Mem%':>6} {'Mem days':>9} Severity")
    print("-" * 72)
    for svc, r in svc_reports.items():
        c, m = r["cpu"], r["memory"]
        print(
            f"{svc:<35} {c['current_percent']:>6.1f} {c['days_to_exhaustion']:>9} "
            f"{m['current_percent']:>6.1f} {m['days_to_exhaustion']:>9} "
            f"{c['severity']}/{m['severity']}"
        )
    print(f"\nNamespace: {ns_pred.overall_severity}")


async def main():
    import argparse
    p = argparse.ArgumentParser(description="SRE Capacity Agent")
    p.add_argument("--namespace", default=os.getenv("OCP_NAMESPACE", "alprc-prod"))
    p.add_argument("--service",   default=None)
    p.add_argument("--days",      type=int, default=30)
    p.add_argument("--dry-run",   action="store_true")
    p.add_argument("--debug",     action="store_true")
    a = p.parse_args()

    services = [a.service] if a.service else None
    await run(a.namespace, a.days, a.dry_run, services, a.debug)

if __name__ == "__main__":
    asyncio.run(main())
