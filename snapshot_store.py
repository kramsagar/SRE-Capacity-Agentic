"""
scripts/reporters/snapshot_store.py

DETERMINISTIC — Stores capacity snapshots as JSON files.
No database required. One JSON file per day per namespace.

Layout on disk:
  data/snapshots/
    payments-prod_2025-01-15.json
    payments-prod_2025-01-16.json
    ...
  data/reports/
    payments-prod_2025-01-15_143022.json
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


DATA_ROOT = Path(__file__).parent.parent.parent / "data"


class SnapshotStore:
    """
    Persists capacity prediction snapshots as JSON files.
    No SQLite, no external dependencies — just files in data/snapshots/.
    """

    def __init__(self, data_root: Path = DATA_ROOT):
        self.snapshots_dir = data_root / "snapshots"
        self.reports_dir   = data_root / "reports"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────
    # Write
    # ──────────────────────────────────────────────

    def save_predictions(self, predictions: list, namespace: str):
        """Append today's predictions to the namespace daily snapshot file."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        path  = self.snapshots_dir / f"{namespace}_{today}.json"

        # Load existing entries for today, or start fresh
        existing = []
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = []

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "predictions": [p.to_dict() for p in predictions],
        }
        existing.append(entry)
        path.write_text(json.dumps(existing, indent=2))

    def save_report(self, namespace: str, report_json: dict, llm_narrative: str = ""):
        """Save a full capacity report as a timestamped JSON file."""
        ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = self.reports_dir / f"{namespace}_{ts}.json"
        path.write_text(json.dumps(
            {**report_json, "llm_narrative": llm_narrative},
            indent=2
        ))
        return path

    # ──────────────────────────────────────────────
    # Read
    # ──────────────────────────────────────────────

    def get_latest_snapshot(self, namespace: str, service: str, resource: str) -> Optional[dict]:
        """Return most recent prediction entry for a service/resource."""
        for path in self._recent_snapshot_files(namespace, days=7):
            entries = self._load_file(path)
            for entry in reversed(entries):
                for p in entry.get("predictions", []):
                    if p.get("service") == service and p.get("resource") == resource:
                        return p
        return None

    def get_trend_history(
        self, namespace: str, service: str, resource: str, days: int = 14
    ) -> list[dict]:
        """Return one data point per day: current_percent + days_to_exhaustion."""
        result = []
        seen_dates = set()

        for path in self._recent_snapshot_files(namespace, days):
            date_str = path.stem.split("_", 1)[1] if "_" in path.stem else ""
            if date_str in seen_dates:
                continue

            entries = self._load_file(path)
            # Use the last entry of the day for that date
            for entry in reversed(entries):
                for p in entry.get("predictions", []):
                    if p.get("service") == service and p.get("resource") == resource:
                        result.append({
                            "date": date_str,
                            "current_percent": p.get("current_percent"),
                            "days_to_exhaustion": p.get("days_to_exhaustion"),
                            "severity": p.get("severity"),
                        })
                        seen_dates.add(date_str)
                        break
                if date_str in seen_dates:
                    break

        return sorted(result, key=lambda r: r["date"])

    def get_at_risk_services(self, namespace: str, days_threshold: int = 30) -> list[dict]:
        """Return services where days_to_exhaustion < threshold, from latest snapshot."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        path  = self.snapshots_dir / f"{namespace}_{today}.json"

        if not path.exists():
            # Fall back to most recent available file
            files = sorted(self.snapshots_dir.glob(f"{namespace}_*.json"))
            if not files:
                return []
            path = files[-1]

        entries = self._load_file(path)
        if not entries:
            return []

        latest = entries[-1]
        at_risk = [
            p for p in latest.get("predictions", [])
            if p.get("days_to_exhaustion", 9999) < days_threshold
        ]
        return sorted(at_risk, key=lambda p: p.get("days_to_exhaustion", 9999))

    def get_recent_reports(self, namespace: str, limit: int = 5) -> list[Path]:
        """Return paths to the N most recent report files for a namespace."""
        files = sorted(self.reports_dir.glob(f"{namespace}_*.json"))
        return files[-limit:]

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    def _recent_snapshot_files(self, namespace: str, days: int) -> list[Path]:
        """Return snapshot files from the last N days, newest first."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        files  = []
        for path in self.snapshots_dir.glob(f"{namespace}_*.json"):
            try:
                date_part = path.stem.split("_", 1)[1]
                file_date = datetime.strptime(date_part, "%Y-%m-%d")
                if file_date >= cutoff:
                    files.append(path)
            except Exception:
                continue
        return sorted(files, reverse=True)

    def _load_file(self, path: Path) -> list:
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
