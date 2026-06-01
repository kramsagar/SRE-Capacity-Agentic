# SRE Capacity Management Agent

Full-fledged capacity prediction and analysis for OCP microservices.
Covers: CPU, memory, HPA scaling headroom, API call growth, namespace contention.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     DETERMINISTIC LAYER                      │
│                     (Scripts — No LLM)                       │
├─────────────────┬─────────────────┬──────────────────────────┤
│  Prometheus     │  Splunk         │  OCP / oc CLI            │
│  Collector      │  Collector      │  Collector               │
│  (CPU, Mem,     │  (API calls     │  (Quotas, HPA,           │
│   HPA history)  │   per service)  │   Deployments)           │
└────────┬────────┴────────┬────────┴──────────┬───────────────┘
         │                 │                   │
         └────────────────▼───────────────────┘
                          │
                   ┌──────▼──────┐
                   │  Capacity   │
                   │  Predictor  │  ← Linear Regression
                   │  (scipy)    │  ← Days to exhaustion
                   └──────┬──────┘
                          │
                   ┌──────▼──────┐
                   │  HPA        │
                   │  Analyzer   │  ← Scaling ceiling prediction
                   └──────┬──────┘
                          │
                   ┌──────▼──────┐
                   │  Snapshot   │
                   │  Store      │  ← SQLite persistence
                   │  (SQLite)   │
                   └──────┬──────┘
                          │
┌─────────────────────────▼───────────────────────────────────┐
│                       LLM LAYER                              │
├─────────────────────────────────────────────────────────────┤
│  Skill: capacity_analysis.md  ← Interpretation rules        │
│  Skill: hpa_recommendations.md                              │
│  Skill: namespace_sharing.md                                │
│  Profile: payment-service/profile.md  ← Service context     │
│                                                             │
│  LM Client (Anthropic API or VS Code LM API)                │
│  → Executive summary, root cause, top 3 actions             │
└─────────────────────────────────────────────────────────────┘
```

---

## Folder Structure

```
sre-agent/
├── agent/
│   ├── capacity_agent.py      ← Main orchestrator (run this)
│   ├── lm_client.py           ← LLM backend wrapper
│   └── skill_loader.py        ← Loads .md skill files
│
├── scripts/
│   ├── collectors/
│   │   ├── prometheus_collector.py   ← CPU, Memory, HPA metrics
│   │   ├── splunk_collector.py       ← API call growth
│   │   └── ocp_collector.py          ← Quotas, HPA status
│   ├── calculators/
│   │   ├── capacity_predictor.py     ← Linear regression + exhaustion dates
│   │   └── hpa_analyzer.py           ← HPA scaling ceiling prediction
│   └── reporters/
│       └── snapshot_store.py         ← SQLite persistence
│
├── skills/                    ← LLM context files (pure markdown)
│   ├── capacity/
│   │   ├── capacity_analysis.md      ← Main interpretation skill
│   │   ├── hpa_recommendations.md
│   │   └── namespace_sharing.md
│   └── services/
│       ├── payment-service/profile.md
│       ├── order-service/profile.md
│       └── ...
│
├── references/                ← Static config (no LLM, no code)
│   ├── prometheus_queries.yaml
│   ├── splunk_queries.yaml
│   ├── thresholds.yaml
│   └── namespace_inventory.yaml
│
├── data/                      ← Generated at runtime
│   ├── snapshots/capacity.db  ← SQLite
│   └── reports/               ← JSON reports per run
│
└── .vscode/tasks.json         ← VS Code task runner
```

---

## What's Deterministic vs LLM

### ✅ Deterministic (scripts/ — pure Python/math)

| Task | Where |
|------|-------|
| Fetch CPU/memory time series | prometheus_collector.py |
| Fetch API call counts | splunk_collector.py |
| Fetch namespace quota | ocp_collector.py |
| Fetch HPA current/max replicas | ocp_collector.py |
| Linear regression on trends | capacity_predictor.py |
| Days to warn/critical/exhaustion | capacity_predictor.py |
| HPA scaling ceiling prediction | hpa_analyzer.py |
| Namespace aggregate utilization | capacity_predictor.aggregate_namespace() |
| Memory leak detection signal | capacity_predictor._detect_leak() |
| Historical snapshot storage | snapshot_store.py |
| Trend-over-trend comparison | snapshot_store.get_trend_history() |

### 🤖 LLM (agent/ + skills/)

| Task | Skill file |
|------|-----------|
| WHY is this resource spiking? | capacity_analysis.md |
| Correlate API growth → resource impact | capacity_analysis.md |
| Is this a memory leak or organic growth? | capacity_analysis.md |
| Namespace contention blast radius | namespace_sharing.md |
| HPA right-sizing recommendations | hpa_recommendations.md |
| Executive summary + prioritized actions | capacity_analysis.md |
| Service-specific context (known patterns) | services/*/profile.md |

---

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export PROMETHEUS_URL=http://your-prometheus:9090
export SPLUNK_URL=https://your-splunk:8089
export SPLUNK_TOKEN=your-splunk-token
export ANTHROPIC_API_KEY=sk-ant-...   # or use VS Code LM

# Run full analysis
python agent/capacity_agent.py --namespace payments-prod --days 30

# Dry run (no LLM, just deterministic predictions)
python agent/capacity_agent.py --namespace payments-prod --dry-run

# Single service
python agent/capacity_agent.py --namespace payments-prod --service payment-service

# Test individual collectors
python scripts/collectors/prometheus_collector.py --namespace payments-prod
python scripts/collectors/ocp_collector.py --namespace payments-prod
python scripts/collectors/splunk_collector.py --namespace payments-prod
```

---

## MCP Mode (VS Code stdio)

If you're using OCP MCP, Grafana MCP, and Splunk MCP in VS Code:

```bash
# Switch collectors to MCP mode
python agent/capacity_agent.py \
  --namespace payments-prod \
  --prom-mode grafana_mcp \
  --splunk-mode splunk_mcp \
  --ocp-mode ocp_mcp
```

Or set environment variables:
```bash
export PROM_MODE=grafana_mcp
export SPLUNK_MODE=splunk_mcp
export OCP_MODE=ocp_mcp
```

Adjust the MCP command names in each collector's `_call_mcp()` method to match
your VS Code MCP server configuration.

---

## Adding a New Service

1. Add to `references/namespace_inventory.yaml`
2. Add PromQL queries to `references/prometheus_queries.yaml`
3. Add Splunk query to `references/splunk_queries.yaml`
4. Create `skills/services/<service-name>/profile.md`

---

## Adding a New Namespace

1. Add to `references/namespace_inventory.yaml`
2. Run: `python agent/capacity_agent.py --namespace <new-namespace>`

---

## Output

Each run produces:
- Console summary with per-service severity
- JSON report in `data/reports/`
- SQLite snapshot in `data/snapshots/capacity.db`
- LLM narrative (executive summary + recommendations)
