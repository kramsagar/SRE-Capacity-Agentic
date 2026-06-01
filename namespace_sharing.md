# Skill: Namespace Sharing & Contention
# File: skills/capacity/namespace_sharing.md

## Context

This OCP namespace (`payments-prod`) hosts 4 microservices:
- payment-service
- order-service
- inventory-service
- notification-service

They share a single ResourceQuota. This creates contention risk.

---

## Contention Scenarios

### CPU Starvation
If one service (e.g., payment-service) scales up rapidly, it may consume
CPU quota that other services need. OCP does NOT auto-throttle by service —
the namespace limit is shared.

### Memory OOM Risk
If total memory requests from all pods approach the namespace limit,
new pod scheduling fails (Pending state). This is silent until pods crash.

### Pod Quota
OCP also enforces a pod count quota. With 4 services × HPA max replicas,
the total can exceed the namespace pod limit.

---

## Detection Rules

When analyzing data, flag contention if:
1. Sum of all service CPU limits > namespace CPU quota × 0.85
2. Any service has `days_to_exhaustion` < 14 AND other services are growing
3. Total pod count (sum of HPA max_replicas) > namespace pod quota × 0.80

---

## Recommendations for Contention

1. **Raise namespace quota** (if cluster has headroom)
2. **Limit-range caps** — set per-service resource limits to prevent one service starving others
3. **Namespace split** — move low-priority services (notification-service) to a separate namespace
4. **KEDA** — use event-driven scaling so services only scale when actually needed
