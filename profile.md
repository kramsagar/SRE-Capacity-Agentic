# Service Profile: payment-service
# File: skills/services/payment-service/profile.md

## Overview
- **Team**: Payments Team
- **Criticality**: CRITICAL (revenue-generating)
- **SLA**: 99.9% uptime
- **Namespace**: payments-prod

## Known Traffic Patterns
- Peak hours: 9 AM – 6 PM IST (business hours)
- Month-end spike: last 3 days of month, ~2x normal traffic
- Marketing campaigns cause sudden spikes (not predictable)

## Resource Profile
- CPU: memory-bound rather than CPU-bound
- Memory: JVM heap — watch for heap growth (GC pressure signal)
- Baseline: ~2 CPU cores, ~4 GB memory at normal load

## Known Issues / History
- Memory leak in v2.3.x (fixed in v2.4.0) — watch for similar pattern
- CPU spike on DB connection pool exhaustion

## HPA Config Notes
- maxReplicas should be at least 2x normal to handle month-end spikes
- Target: 70% CPU utilization
