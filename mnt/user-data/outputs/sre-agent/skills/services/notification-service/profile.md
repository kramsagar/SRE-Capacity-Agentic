# Service Profile: notification-service

## Overview
- **Criticality**: LOW
- **Team**: Payments Team
- **Namespace**: payments-prod

## Known Traffic Patterns
- Triggered by events from payment-service and order-service
- Traffic is always a fraction of payment-service volume
- Low baseline; spikes are short (fire-and-forget emails/SMS)

## Resource Profile
- Baseline: ~0.3 CPU cores, ~512 MB memory
- Very lightweight; rarely the capacity bottleneck

## Notes
- Good candidate for namespace split if contention risk is raised
- If it IS growing unexpectedly, check for retry storms from upstream
