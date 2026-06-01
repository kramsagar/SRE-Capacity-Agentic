"""
scripts/calculators/capacity_predictor.py

FULLY DETERMINISTIC — no LLM involved.
Linear regression on CPU/memory time series to predict exhaustion dates.

Provides:
  - Per-service CPU/memory predictions
  - Growth rate classification
  - Days to warn/critical/exhaustion
  - Memory leak detection signal
  - API call growth correlation
"""

import numpy as np
from scipy import stats
from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import yaml
from pathlib import Path


THRESHOLDS_PATH = Path(__file__).parent.parent.parent / "references" / "thresholds.yaml"


def load_thresholds() -> dict:
    return yaml.safe_load(THRESHOLDS_PATH.read_text())


# ──────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────

@dataclass
class PredictionResult:
    service: str
    resource: str               # "cpu" or "memory"
    namespace: str

    # Current state
    current_value: float        # actual value (cores or bytes)
    limit_value: float          # hard limit
    current_percent: float      # current / limit * 100

    # Regression output
    slope_per_day: float        # rate of change per day (in raw units)
    r_squared: float            # goodness of fit (0-1)
    trend: str                  # "growing" | "stable" | "declining" | "volatile"

    # Predictions
    days_to_warn: int           # days until 70% of limit
    days_to_critical: int       # days until 85% of limit
    days_to_exhaustion: int     # days until 100% of limit

    # Signals
    is_memory_leak_suspect: bool = False
    leak_reason: str = ""

    # Friendly display
    @property
    def current_display(self) -> str:
        if self.resource == "memory":
            return f"{self.current_value / 1e9:.2f} GB"
        return f"{self.current_value:.3f} cores"

    @property
    def limit_display(self) -> str:
        if self.resource == "memory":
            return f"{self.limit_value / 1e9:.2f} GB"
        return f"{self.limit_value:.3f} cores"

    @property
    def severity(self) -> str:
        if self.current_percent >= 85 or self.days_to_exhaustion <= 14:
            return "CRITICAL"
        if self.current_percent >= 70 or self.days_to_exhaustion <= 30:
            return "WARN"
        return "OK"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["current_display"] = self.current_display
        d["limit_display"] = self.limit_display
        d["severity"] = self.severity
        return d


@dataclass
class NamespacePrediction:
    namespace: str
    service_predictions: list[PredictionResult]

    # Namespace-level summary
    total_cpu_used_percent: float
    total_memory_used_percent: float
    days_to_cpu_exhaustion: int
    days_to_memory_exhaustion: int
    top_cpu_consumer: str
    top_memory_consumer: str
    at_risk_services: list[str]
    contention_risk: bool

    @property
    def overall_severity(self) -> str:
        if self.days_to_cpu_exhaustion <= 14 or self.days_to_memory_exhaustion <= 14:
            return "CRITICAL"
        if self.days_to_cpu_exhaustion <= 30 or self.days_to_memory_exhaustion <= 30:
            return "WARN"
        return "OK"

    def to_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "total_cpu_used_percent": self.total_cpu_used_percent,
            "total_memory_used_percent": self.total_memory_used_percent,
            "days_to_cpu_exhaustion": self.days_to_cpu_exhaustion,
            "days_to_memory_exhaustion": self.days_to_memory_exhaustion,
            "top_cpu_consumer": self.top_cpu_consumer,
            "top_memory_consumer": self.top_memory_consumer,
            "at_risk_services": self.at_risk_services,
            "contention_risk": self.contention_risk,
            "overall_severity": self.overall_severity,
            "service_predictions": [p.to_dict() for p in self.service_predictions],
        }


# ──────────────────────────────────────────────────────────────
# Predictor
# ──────────────────────────────────────────────────────────────

class CapacityPredictor:
    """
    Pure deterministic predictor using linear regression.
    No LLM, no network calls.
    """

    def __init__(self):
        self.thresholds = load_thresholds()

    def predict_resource(
        self,
        service: str,
        resource: str,           # "cpu" or "memory"
        namespace: str,
        usage_series: list[tuple[float, float]],  # [(unix_ts, value), ...]
        limit: float,
        api_call_series: Optional[list[tuple[float, float]]] = None,
    ) -> PredictionResult:
        """
        Run linear regression on usage_series and predict exhaustion.

        Args:
            usage_series: [(timestamp_seconds, value), ...] sorted ascending
            limit: hard limit value (same units as usage)
            api_call_series: optional API call data for leak detection
        """
        thresholds = self.thresholds["capacity"]

        if len(usage_series) < 3:
            return self._insufficient_data(service, resource, namespace, usage_series, limit)

        timestamps = np.array([x[0] for x in usage_series])
        values     = np.array([x[1] for x in usage_series])

        # Normalize timestamps to days from first point
        days = (timestamps - timestamps[0]) / 86400

        # Linear regression
        slope, intercept, r_value, p_value, std_err = stats.linregress(days, values)
        r_squared = r_value ** 2

        current = float(values[-1])
        current_pct = (current / limit * 100) if limit > 0 else 0.0

        # Classify trend
        norm_slope = slope / limit if limit > 0 else slope
        trend = self._classify_trend(norm_slope)

        # Days to thresholds
        warn_limit     = limit * (thresholds["warn_percent"] / 100)
        critical_limit = limit * (thresholds["critical_percent"] / 100)

        days_to_warn        = self._days_to_reach(current, slope, warn_limit)
        days_to_critical    = self._days_to_reach(current, slope, critical_limit)
        days_to_exhaustion  = self._days_to_reach(current, slope, limit)

        # Memory leak detection
        is_leak = False
        leak_reason = ""
        if resource == "memory" and api_call_series and len(api_call_series) >= 5:
            is_leak, leak_reason = self._detect_leak(
                usage_series, api_call_series, norm_slope
            )

        return PredictionResult(
            service=service,
            resource=resource,
            namespace=namespace,
            current_value=current,
            limit_value=limit,
            current_percent=round(current_pct, 2),
            slope_per_day=round(slope, 6),
            r_squared=round(r_squared, 4),
            trend=trend,
            days_to_warn=days_to_warn,
            days_to_critical=days_to_critical,
            days_to_exhaustion=days_to_exhaustion,
            is_memory_leak_suspect=is_leak,
            leak_reason=leak_reason,
        )

    def aggregate_namespace(
        self,
        service_predictions: list[PredictionResult],
        ns_quota: dict,           # { "cpu": float_cores, "memory": float_bytes }
        ns_cpu_series: list[tuple[float, float]],
        ns_mem_series: list[tuple[float, float]],
    ) -> NamespacePrediction:
        """
        Compute namespace-level prediction from service predictions.
        """
        thresholds = self.thresholds["capacity"]
        cpu_preds = [p for p in service_predictions if p.resource == "cpu"]
        mem_preds = [p for p in service_predictions if p.resource == "memory"]

        # Namespace total usage = sum of service usages
        total_cpu = sum(p.current_value for p in cpu_preds)
        total_mem = sum(p.current_value for p in mem_preds)

        cpu_quota = ns_quota.get("cpu", 1)
        mem_quota = ns_quota.get("memory", 1)

        total_cpu_pct = round(total_cpu / cpu_quota * 100, 1) if cpu_quota > 0 else 0
        total_mem_pct = round(total_mem / mem_quota * 100, 1) if mem_quota > 0 else 0

        # Namespace exhaustion = minimum days across all services (weakest link)
        days_cpu_exhaustion = min((p.days_to_exhaustion for p in cpu_preds), default=9999)
        days_mem_exhaustion = min((p.days_to_exhaustion for p in mem_preds), default=9999)

        top_cpu = max(cpu_preds, key=lambda p: p.current_percent, default=None)
        top_mem = max(mem_preds, key=lambda p: p.current_percent, default=None)

        at_risk = list({
            p.service for p in service_predictions
            if p.days_to_exhaustion < thresholds["days_to_exhaustion_warn"]
        })

        # Contention: if sum of limits > namespace quota * 0.95
        contention_thresh = self.thresholds["namespace_contention"]["overcommit_warn_factor"]
        sum_cpu_limits = sum(p.limit_value for p in cpu_preds)
        contention_risk = (sum_cpu_limits / cpu_quota) > contention_thresh if cpu_quota > 0 else False

        namespace = service_predictions[0].namespace if service_predictions else "unknown"

        return NamespacePrediction(
            namespace=namespace,
            service_predictions=service_predictions,
            total_cpu_used_percent=total_cpu_pct,
            total_memory_used_percent=total_mem_pct,
            days_to_cpu_exhaustion=days_cpu_exhaustion,
            days_to_memory_exhaustion=days_mem_exhaustion,
            top_cpu_consumer=top_cpu.service if top_cpu else "N/A",
            top_memory_consumer=top_mem.service if top_mem else "N/A",
            at_risk_services=at_risk,
            contention_risk=contention_risk,
        )

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    def _days_to_reach(self, current: float, slope: float, target: float) -> int:
        """Given current value, daily slope, and target, return days until target is reached."""
        if slope <= 0:
            return 9999   # not growing
        if current >= target:
            return 0      # already at/past target
        return int((target - current) / slope)

    def _classify_trend(self, normalized_slope: float) -> str:
        thresholds = self.thresholds["growth_rate"]
        if abs(normalized_slope) < thresholds["stable_slope_threshold"]:
            return "stable"
        if normalized_slope > thresholds["high_growth_slope_threshold"]:
            return "high_growth"
        if normalized_slope > 0:
            return "growing"
        return "declining"

    def _detect_leak(
        self,
        mem_series: list[tuple[float, float]],
        api_series: list[tuple[float, float]],
        mem_norm_slope: float,
    ) -> tuple[bool, str]:
        """
        Memory leak heuristic:
          - Memory is growing (slope > threshold)
          - BUT API calls are flat or declining (no correlation)
        Returns (is_leak, reason_string)
        """
        cfg = self.thresholds["memory_leak"]

        if mem_norm_slope < (cfg["daily_growth_suspect_percent"] / 100):
            return False, ""

        # Compute API call trend
        if len(api_series) < 5:
            return False, ""

        api_ts = np.array([x[0] for x in api_series])
        api_v  = np.array([x[1] for x in api_series])
        api_days = (api_ts - api_ts[0]) / 86400
        api_slope, _, r_api, _, _ = stats.linregress(api_days, api_v)

        # Correlate memory timestamps to api timestamps (align by nearest day)
        mem_ts = np.array([x[0] for x in mem_series])
        mem_v  = np.array([x[1] for x in mem_series])

        # Pearson correlation between memory and api calls over time
        try:
            min_len = min(len(mem_v), len(api_v))
            r_corr, _ = stats.pearsonr(mem_v[-min_len:], api_v[-min_len:])
        except Exception:
            r_corr = 0

        if r_corr < cfg["api_correlation_threshold"] and api_slope <= 0:
            return True, (
                f"Memory growing {mem_norm_slope*100:.1f}%/day but API calls "
                f"flat/declining (correlation={r_corr:.2f})"
            )
        return False, ""

    def _insufficient_data(self, service, resource, namespace, series, limit):
        current = series[-1][1] if series else 0
        return PredictionResult(
            service=service, resource=resource, namespace=namespace,
            current_value=current, limit_value=limit,
            current_percent=round(current / limit * 100, 2) if limit > 0 else 0,
            slope_per_day=0, r_squared=0, trend="insufficient_data",
            days_to_warn=9999, days_to_critical=9999, days_to_exhaustion=9999,
        )


# ──────────────────────────────────────────────────────────────
# CLI entry point for quick testing
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import random, time

    # Synthetic test data: growing memory usage
    predictor = CapacityPredictor()
    base_ts = time.time() - 30 * 86400
    mem_series = [
        (base_ts + i * 86400, 2e9 + i * 0.1e9 + random.gauss(0, 0.05e9))
        for i in range(30)
    ]
    mem_limit = 8e9   # 8 GB

    result = predictor.predict_resource(
        service="payment-service",
        resource="memory",
        namespace="payments-prod",
        usage_series=mem_series,
        limit=mem_limit,
    )

    print(json.dumps(result.to_dict(), indent=2))
