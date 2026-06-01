"""
scripts/calculators/hpa_analyzer.py

FULLY DETERMINISTIC — analyzes HPA scaling headroom and predicts
when a service will hit its max replica ceiling.

Combines:
  - OCP HPA current/max/desired replicas
  - Prometheus replica count history
  - Thresholds from references/thresholds.yaml
"""

import numpy as np
from scipy import stats
from dataclasses import dataclass, asdict
from typing import Optional
import yaml
from pathlib import Path


THRESHOLDS_PATH = Path(__file__).parent.parent.parent / "references" / "thresholds.yaml"


@dataclass
class HPAPrediction:
    service: str
    namespace: str
    current_replicas: int
    desired_replicas: int
    max_replicas: int
    min_replicas: int

    # Computed
    headroom_replicas: int        # max - current
    headroom_percent: float       # (max - current) / max * 100
    utilization_percent: float    # current / max * 100

    # Trend
    replica_slope_per_day: float  # rate replicas are growing
    days_to_max_replicas: int     # predicted days until max is hit

    # Severity
    severity: str                 # OK | WARN | CRITICAL

    def to_dict(self) -> dict:
        return asdict(self)


class HPAAnalyzer:
    """
    Analyzes HPA status and predicts when services will hit scaling ceiling.
    """

    def __init__(self):
        self.thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())

    def analyze(
        self,
        service: str,
        namespace: str,
        current_replicas: int,
        desired_replicas: int,
        max_replicas: int,
        min_replicas: int,
        replica_history: Optional[list[tuple[float, float]]] = None,
        # [(unix_ts, replica_count), ...]  — from Prometheus kube_hpa metric
    ) -> HPAPrediction:
        """Analyze HPA status and predict time to ceiling."""

        thresholds = self.thresholds["hpa"]
        headroom = max_replicas - current_replicas
        headroom_pct = (headroom / max_replicas * 100) if max_replicas > 0 else 100.0
        utilization = (current_replicas / max_replicas * 100) if max_replicas > 0 else 0.0

        # Predict days to max replicas from replica history
        slope = 0.0
        days_to_max = 9999

        if replica_history and len(replica_history) >= 5:
            timestamps = np.array([x[0] for x in replica_history])
            replicas   = np.array([x[1] for x in replica_history])
            days = (timestamps - timestamps[0]) / 86400

            slope, intercept, r_value, _, _ = stats.linregress(days, replicas)
            slope = float(slope)

            if slope > 0 and current_replicas < max_replicas:
                days_to_max = max(0, int((max_replicas - current_replicas) / slope))

        # Severity
        crit_thresh = thresholds["scale_headroom_critical"]
        warn_thresh = thresholds["scale_headroom_warn"]
        util_ratio = current_replicas / max_replicas if max_replicas > 0 else 0

        if util_ratio >= crit_thresh or days_to_max <= 7:
            severity = "CRITICAL"
        elif util_ratio >= warn_thresh or days_to_max <= 14:
            severity = "WARN"
        else:
            severity = "OK"

        return HPAPrediction(
            service=service,
            namespace=namespace,
            current_replicas=current_replicas,
            desired_replicas=desired_replicas,
            max_replicas=max_replicas,
            min_replicas=min_replicas,
            headroom_replicas=headroom,
            headroom_percent=round(headroom_pct, 1),
            utilization_percent=round(utilization, 1),
            replica_slope_per_day=round(slope, 3),
            days_to_max_replicas=days_to_max,
            severity=severity,
        )

    def analyze_all(
        self,
        namespace: str,
        hpa_data: dict,   # { "service-name": HPAStatus } from ocp_collector
        replica_histories: dict = None,  # { "service-name": [(ts, replicas), ...] }
    ) -> list[HPAPrediction]:
        """Analyze all HPAs in a namespace."""
        results = []
        for service_name, hpa in hpa_data.items():
            history = (replica_histories or {}).get(service_name)
            pred = self.analyze(
                service=service_name,
                namespace=namespace,
                current_replicas=hpa.current_replicas,
                desired_replicas=hpa.desired_replicas,
                max_replicas=hpa.max_replicas,
                min_replicas=hpa.min_replicas,
                replica_history=history,
            )
            results.append(pred)
        return results
