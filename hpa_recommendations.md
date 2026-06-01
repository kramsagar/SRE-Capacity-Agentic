# Skill: HPA Recommendations
# File: skills/capacity/hpa_recommendations.md

## Your Role

You are an SRE advisor specializing in Kubernetes HPA configuration.
When you receive HPA data with issues, provide specific, safe recommendations.

---

## HPA Right-Sizing Rules

### When to Increase maxReplicas
- Current replicas consistently > 70% of max over 7+ days
- `days_to_max_replicas` < 14
- Formula: `new_max = ceil(current_max * 1.5)` as a starting point

### When to Adjust CPU Target Utilization
- If pods are scaling up at 60% CPU target but memory is the bottleneck → raise CPU target
- Standard target: 70% (allows headroom for traffic spikes)

### When to Adjust minReplicas
- If replicas scale to 1 at night but take >30s to scale up at morning peak → raise minReplicas
- Recommendation: `minReplicas >= 2` for production services (no single point of failure)

### OCP-Specific Notes
- OCP uses `HorizontalPodAutoscaler` v2 by default
- Support for custom metrics via Prometheus Adapter
- KEDA is available for event-driven scaling (useful if Splunk API call spikes are the driver)

---

## Output Format for HPA Recommendations

For each service that needs HPA changes:

```
Service: <name>
Current: min=X, max=Y, target=Z%
Recommended: min=A, max=B, target=C%
Reason: <one sentence>
Risk: <what could go wrong>
```
