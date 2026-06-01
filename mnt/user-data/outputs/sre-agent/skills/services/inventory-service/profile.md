# Service Profile: inventory-service

## Overview
- **Criticality**: MEDIUM
- **Team**: Payments Team
- **Namespace**: payments-prod

## Known Traffic Patterns
- Read-heavy: 90% GET requests
- Bulk sync jobs run at 2 AM IST — causes CPU spike for ~15 min
- Traffic roughly proportional to order-service

## Resource Profile
- Baseline: ~0.8 CPU cores, ~1.5 GB memory
- Memory stable; CPU spikes are short-lived (sync jobs)

## Known Issues
- Sync job CPU spike is expected and benign — don't flag as anomaly
- If memory grows without API call growth, check cache eviction config
