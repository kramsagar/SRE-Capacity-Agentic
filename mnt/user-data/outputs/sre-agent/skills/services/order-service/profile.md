# Service Profile: order-service

## Overview
- **Criticality**: HIGH
- **Team**: Payments Team
- **Namespace**: payments-prod

## Known Traffic Patterns
- Peaks mirror payment-service (checkout flow)
- Order creation spikes during promotions/campaigns
- Low traffic 11 PM – 6 AM IST

## Resource Profile
- Baseline: ~1.5 CPU cores, ~2 GB memory
- Stateless; scales well horizontally

## Known Issues
- DB connection pool leak observed in v1.8 (fixed in v1.9)
- Memory grows if order event queue backs up
