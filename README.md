# SRE Capacity Agent — Complete Guide

> One document covering everything: what every file does, where to place it, how to set up VS Code MCP, and how to run the agent.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Complete Directory Layout — Where Every File Goes](#2-complete-directory-layout--where-every-file-goes)
3. [Step-by-Step Setup](#3-step-by-step-setup)
4. [Every File — What It Is, What It Does, What to Edit](#4-every-file--what-it-is-what-it-does-what-to-edit)
   - [.vscode/mcp.json](#vscode--mcpjson)
   - [.vscode/tasks.json](#vscode--tasksjson)
   - [.env.example](#envexample)
   - [.gitignore](#gitignore)
   - [requirements.txt](#requirementstxt)
   - [agent/capacity\_agent.py](#agentcapacity_agentpy)
   - [agent/lm\_client.py](#agentlm_clientpy)
   - [agent/skill\_loader.py](#agentskill_loaderpy)
   - [scripts/collectors/prometheus\_collector.py](#scriptscollectorsprometheus_collectorpy)
   - [scripts/collectors/splunk\_collector.py](#scriptscollectorssplunk_collectorpy)
   - [scripts/collectors/ocp\_collector.py](#scriptscollectorsocp_collectorpy)
   - [scripts/calculators/capacity\_predictor.py](#scriptscalculatorscapacity_predictorpy)
   - [scripts/calculators/hpa\_analyzer.py](#scriptscalculatorshpa_analyzerpy)
   - [scripts/reporters/snapshot\_store.py](#scriptsreporterssnapshot_storepy)
   - [skills/capacity/capacity\_analysis.md](#skillscapacitycapacity_analysismd)
   - [skills/capacity/hpa\_recommendations.md](#skillscapacityhpa_recommendationsmd)
   - [skills/capacity/namespace\_sharing.md](#skillscapacitynamespace_sharingmd)
   - [skills/services/\*/profile.md](#skillsservicesprofilemd)
   - [references/prometheus\_queries.yaml](#referencesprometheus_queriesyaml)
   - [references/splunk\_queries.yaml](#referencessplunk_queriesyaml)
   - [references/thresholds.yaml](#referencesthresholdsyaml)
   - [references/namespace\_inventory.yaml](#referencesnamespace_inventoryyaml)
5. [How to Run](#5-how-to-run)
6. [Using GitHub Copilot Agent Mode](#6-using-github-copilot-agent-mode)
7. [Adding a New Service or Namespace](#7-adding-a-new-service-or-namespace)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Project Overview

This agent monitors 4 microservices sharing one OCP namespace and answers:

- **How many days until CPU or memory runs out** — per service and per namespace
- **Is a service leaking memory or just growing organically** — via API call correlation
- **Which service will hit its HPA replica ceiling first** — scaling headroom analysis
- **What to do about it** — prioritised recommendations from the LLM

It uses three data sources:

| Source | What it provides | MCP server |
|--------|-----------------|-----------|
| Prometheus (via Grafana) | CPU/memory 30-day trends | `grafana` |
| Splunk | API call counts per day | `splunk` |
| OCP / Kubernetes | Namespace quota, HPA status | `kubernetes` |

The agent is split into two clean layers:

- **Deterministic layer** (`scripts/`) — collects data, runs linear regression, produces numbers. No LLM, fully testable.
- **LLM layer** (`agent/` + `skills/`) — interprets the numbers, identifies root causes, writes recommendations.

---

## 2. Complete Directory Layout — Where Every File Goes

Create this exact structure on your machine. Every file listed below is provided.

```
sre-agent/                          ← your project root, open this in VS Code
│
├── .vscode/
│   ├── mcp.json                    ← VS Code reads this to start MCP servers
│   └── tasks.json                  ← Ctrl+Shift+P shortcut tasks
│
├── .env.example                    ← template — copy to .env and fill in
├── .env                            ← YOUR credentials (never commit — gitignored)
├── .gitignore                      ← keeps .env and data/ out of git
├── requirements.txt                ← pip install -r requirements.txt
├── QUICKSTART.md                   ← 5-minute start
├── README.md                       ← full reference doc
│
├── agent/
│   ├── capacity_agent.py           ← main entry point — run this
│   ├── lm_client.py                ← LLM backend (Anthropic API)
│   └── skill_loader.py             ← loads .md skill files into prompts
│
├── scripts/
│   ├── collectors/
│   │   ├── prometheus_collector.py ← fetches CPU/memory trends from Prometheus
│   │   ├── splunk_collector.py     ← fetches API call growth from Splunk
│   │   └── ocp_collector.py        ← fetches quota + HPA from OCP
│   ├── calculators/
│   │   ├── capacity_predictor.py   ← linear regression → days to exhaustion
│   │   └── hpa_analyzer.py         ← HPA scaling ceiling prediction
│   └── reporters/
│       └── snapshot_store.py       ← saves predictions as daily JSON files
│
├── skills/                         ← markdown context files for the LLM
│   ├── capacity/
│   │   ├── capacity_analysis.md    ← interpretation rules
│   │   ├── hpa_recommendations.md  ← HPA tuning rules
│   │   └── namespace_sharing.md    ← multi-service contention rules
│   └── services/
│       ├── payment-service/
│       │   └── profile.md          ← service-specific context
│       ├── order-service/
│       │   └── profile.md
│       ├── inventory-service/
│       │   └── profile.md
│       └── notification-service/
│           └── profile.md
│
├── references/                     ← static YAML config — only files you edit
│   ├── prometheus_queries.yaml     ← all PromQL queries, parameterised
│   ├── splunk_queries.yaml         ← all SPL queries, parameterised
│   ├── thresholds.yaml             ← warn/critical/exhaustion thresholds
│   └── namespace_inventory.yaml    ← which namespaces contain which services
│
└── data/                           ← created at runtime — gitignored
    ├── snapshots/                  ← daily prediction JSON files
    └── reports/                    ← full reports per run
```

### How to create this structure on your machine

```bash
# Create all folders
mkdir -p sre-agent/.vscode
mkdir -p sre-agent/agent
mkdir -p sre-agent/scripts/collectors
mkdir -p sre-agent/scripts/calculators
mkdir -p sre-agent/scripts/reporters
mkdir -p sre-agent/skills/capacity
mkdir -p sre-agent/skills/services/payment-service
mkdir -p sre-agent/skills/services/order-service
mkdir -p sre-agent/skills/services/inventory-service
mkdir -p sre-agent/skills/services/notification-service
mkdir -p sre-agent/references
mkdir -p sre-agent/data/snapshots
mkdir -p sre-agent/data/reports
```

Then copy each file from the downloads into the matching path shown above.

---

## 3. Step-by-Step Setup

### Prerequisites

| Tool | Minimum version | Install |
|------|----------------|---------|
| VS Code | 1.99 | [code.visualstudio.com](https://code.visualstudio.com) |
| GitHub Copilot extension | latest | VS Code Extensions panel |
| Node.js | 18+ | `winget install OpenJS.NodeJS.LTS` or `brew install node` |
| Python | 3.11+ | `winget install Python.Python.3.11` or `brew install python` |
| uv (Python runner) | any | `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| oc CLI | any | from your OCP cluster download page |

Check everything is installed:
```bash
node --version      # should be v18+
npx --version
python --version    # should be 3.11+
uvx --version
oc version
```

---

### Step 1 — Copy files into the folder structure

Follow the layout in Section 2. Every file you downloaded goes into the exact path shown.

---

### Step 2 — Install Python dependencies

```bash
cd sre-agent
pip install -r requirements.txt
```

This installs: `httpx`, `pyyaml`, `numpy`, `scipy`, `anthropic`

---

### Step 3 — Create your .env file

```bash
cp .env.example .env
```

Open `.env` and fill in your real values:

```env
# Grafana (Prometheus access)
GRAFANA_URL=http://your-grafana-host:3000
GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_xxxxxxxxxxxxxxxxxxxx

# Splunk
SPLUNK_HOST=https://your-splunk-host:8089
SPLUNK_TOKEN=your-splunk-bearer-token
SPLUNK_VERIFY_SSL=false

# OCP — path to your kubeconfig
KUBECONFIG=~/.kube/config
OCP_NAMESPACE=payments-prod

# LLM (for full analysis with recommendations)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx
```

**How to get a Grafana service account token:**
1. Grafana → Administration → Service Accounts → Add service account
2. Role: Viewer
3. Click "Add service account token" → copy the `glsa_...` value

**How to get a Splunk token:**
Splunk Web → Settings → Tokens → New Token

`.env` is listed in `.gitignore` — it will never be committed to git.

---

### Step 4 — Open in VS Code

```bash
code sre-agent/
```

VS Code automatically reads `.vscode/mcp.json` on startup and registers the three MCP servers.

---

### Step 5 — Switch Copilot to Agent mode

1. Open Copilot Chat: `Ctrl+Shift+I`
2. Click the mode dropdown at the top of the chat panel
3. Select **Agent**

MCP tools only work in Agent mode. Ask mode and Edit mode will not trigger them.

---

### Step 6 — Verify MCP servers are running

Click the **Tools icon** (⚙) at the bottom of the Copilot chat input.

You should see:
```
✅ grafana     — query_prometheus, query_prometheus_range, search_dashboards ...
✅ splunk      — search, get_indexes, get_saved_searches ...
✅ kubernetes  — list_resources, get_resource, list_pods, describe_resource ...
```

The first time each server starts, VS Code will prompt you for credentials (Grafana URL, Grafana token, Splunk host, Splunk token). It stores these securely and will not ask again.

---

## 4. Every File — What It Is, What It Does, What to Edit

---

### `.vscode/` — `mcp.json`

**What it is:** The VS Code MCP configuration file. VS Code reads this automatically when you open the project and starts the three MCP servers as local subprocesses.

**What it does:** Defines three stdio MCP servers:

| Server name | npm/pip package | Talks to |
|-------------|----------------|---------|
| `grafana` | `@leval/mcp-grafana` (npm, auto-downloaded) | Your Grafana instance → Prometheus |
| `splunk` | `splunk-mcp` (Python, auto-downloaded via uvx) | Your Splunk instance |
| `kubernetes` | `kubernetes-mcp-server@latest` (npm, auto-downloaded) | Your OCP cluster via `~/.kube/config` |

**How it works:**
```json
"servers": {
  "grafana": {
    "type": "stdio",              ← local subprocess, not a network connection
    "command": "npx",             ← how to start it
    "args": ["-y", "@leval/mcp-grafana"],
    "env": {
      "GRAFANA_URL": "${input:grafana-url}",          ← VS Code prompts you once
      "GRAFANA_SERVICE_ACCOUNT_TOKEN": "${input:grafana-token}"
    }
  }
}
```

`${input:id}` — VS Code prompts you for this value once on first run, then stores it in encrypted secret storage. You are never prompted again unless you clear secrets.

`${env:VAR}` — reads from your system environment variable (used for KUBECONFIG).

**What to edit:** The `default` values in the `inputs` array if your Grafana URL is always the same. Everything else is handled via the credential prompts.

**Critical rule:** Root key is `"servers"` — not `"mcpServers"`. Using `"mcpServers"` is the Cursor/Claude Desktop syntax and will silently fail in VS Code.

---

### `.vscode/` — `tasks.json`

**What it is:** VS Code task definitions. Accessible via `Ctrl+Shift+P → Tasks: Run Task`.

**What it does:** Provides clickable shortcuts for common commands so you don't need to remember CLI syntax. Defined tasks:

| Task name | What it runs |
|-----------|-------------|
| Install: Python deps | `pip install -r requirements.txt` |
| Capacity: Full Analysis (dry-run) | `python agent/capacity_agent.py --namespace X --dry-run` |
| Capacity: Full Analysis with LLM | `python agent/capacity_agent.py --namespace X` |
| Capacity: Single Service | single service analysis |
| OCP: List Namespaces | `oc get namespaces` |
| OCP: Check HPA | `oc get hpa -n X` |
| OCP: Check Resource Quota | `oc describe resourcequota -n X` |

When you run a task VS Code asks you to type/pick the namespace and service name.

**What to edit:** Add your own service names to the `options` array in the `inputs` section at the bottom of the file.

---

### `.env.example`

**What it is:** A template showing every environment variable the project needs.

**What to do:** Copy it to `.env` and fill in real values. Never edit `.env.example` with real credentials — it is safe to commit, `.env` is not.

```bash
cp .env.example .env
# now edit .env
```

Variables defined:
- `GRAFANA_URL` — full URL to your Grafana instance
- `GRAFANA_SERVICE_ACCOUNT_TOKEN` — Grafana service account token (`glsa_...`)
- `SPLUNK_HOST` — Splunk REST API URL with port (usually 8089)
- `SPLUNK_TOKEN` — Splunk bearer token
- `SPLUNK_VERIFY_SSL` — set `false` if your Splunk uses a self-signed cert
- `KUBECONFIG` — path to your kubeconfig file
- `OCP_NAMESPACE` — default namespace to analyse
- `ANTHROPIC_API_KEY` — only needed for full LLM recommendations

---

### `.gitignore`

**What it is:** Tells git what not to commit.

**What it protects:**
- `.env` — your real credentials
- `data/snapshots/` and `data/reports/` — generated runtime data
- `__pycache__/` and `*.pyc` — Python bytecode

**What to edit:** Nothing. Leave as-is.

---

### `requirements.txt`

**What it is:** Python package list for `pip install -r requirements.txt`.

**Packages and why each is needed:**

| Package | Why |
|---------|-----|
| `httpx` | Async HTTP client for Prometheus REST and Splunk REST API calls |
| `pyyaml` | Reads all the YAML config files in `references/` |
| `numpy` | Array operations for time series data in the predictor |
| `scipy` | `stats.linregress()` — the linear regression that calculates days to exhaustion |
| `anthropic` | Anthropic Python SDK for LLM calls. Only needed if `ANTHROPIC_API_KEY` is set |

**What to edit:** Nothing unless you add new dependencies.

---

### `agent/` — `capacity_agent.py`

**What it is:** The main entry point. Run this file to start a capacity analysis.

**What it does:** Orchestrates the full pipeline in 5 steps:

```
Step 1 — collect    Runs all three collectors in parallel using asyncio.gather()
Step 2 — predict    Calls capacity_predictor.py for each service × {cpu, memory}
Step 3 — snapshot   Saves today's predictions to data/snapshots/
Step 4 — LLM        Loads skill files, builds prompt, calls lm_client.py
Step 5 — report     Saves full JSON report to data/reports/
```

**Two usage modes:**

```
Standalone CLI:     python agent/capacity_agent.py --namespace payments-prod
Inside VS Code:     Copilot injects MCPClient → collectors call mcp_client.call_tool()
```

When run standalone the collectors use direct HTTP (`httpx`) and `oc` CLI subprocess calls.
When run from VS Code Copilot Agent mode the `MCPClient` class routes every data request through the MCP servers that VS Code started.

**Key class: `MCPClient`**
This class wraps whatever MCP context VS Code injects. It exposes one method:
```python
await mcp_client.call_tool(server="grafana", tool="query_prometheus_range", args={...})
```
Each collector receives this object and calls it instead of making its own HTTP requests. This is how the MCP wiring works.

**CLI options:**

```bash
python agent/capacity_agent.py --namespace payments-prod          # full run
python agent/capacity_agent.py --namespace payments-prod --dry-run  # skip LLM
python agent/capacity_agent.py --namespace payments-prod --service payment-service
python agent/capacity_agent.py --namespace payments-prod --days 14
python agent/capacity_agent.py --prom-mode grafana_mcp --splunk-mode splunk_mcp --ocp-mode ocp_mcp
```

**Environment variables it reads:**

| Variable | Default | Effect |
|----------|---------|--------|
| `GRAFANA_URL` | `http://localhost:9090` | Prometheus/Grafana URL |
| `SPLUNK_HOST` | `https://localhost:8089` | Splunk REST URL |
| `SPLUNK_TOKEN` | — | Splunk auth token |
| `PROM_MODE` | `http` | `http` or `grafana_mcp` |
| `SPLUNK_MODE` | `http` | `http` or `splunk_mcp` |
| `OCP_MODE` | `cli` | `cli` or `ocp_mcp` |
| `ANTHROPIC_API_KEY` | — | enables LLM analysis |

**What to edit:** The default values at the top of the file if your infrastructure URLs are different from the defaults. Everything else is controlled by environment variables.

---

### `agent/` — `lm_client.py`

**What it is:** Thin wrapper around the LLM backend.

**What it does:** Automatically detects which LLM backend to use and exposes one method: `await lm.call(prompt) → str`.

**Mode selection:**

| Mode | When | What happens |
|------|------|-------------|
| `anthropic` | `ANTHROPIC_API_KEY` env var is set | Calls `claude-sonnet-4-20250514` |
| `mock` | No API key set | Returns placeholder text — useful for testing the pipeline without LLM cost |

**What to edit:** The model string (`claude-sonnet-4-20250514`) if you want to use a different Anthropic model. Nothing else.

---

### `agent/` — `skill_loader.py`

**What it is:** Reads markdown files from `skills/` and returns them as strings.

**What it does:** The LLM has no memory between runs — it starts fresh every call. The skill loader is what makes the LLM "know" your rules, thresholds, and service history by reading the markdown files and injecting them into the prompt as context.

**Three methods:**

| Method | What it loads |
|--------|--------------|
| `load("capacity/capacity_analysis.md")` | Single skill file by path |
| `load_service_profiles(["payment-service", ...])` | All `profile.md` files for listed services, concatenated |
| `load_all_capacity_skills()` | Everything in `skills/capacity/` at once |

**What to edit:** Nothing — this file just reads other files. Edit the `.md` files in `skills/` to change LLM behaviour.

---

### `scripts/collectors/` — `prometheus_collector.py`

**What it is:** Fetches CPU usage trends, memory usage trends, and HPA replica history from Prometheus.

**What it does:** Returns time series as `[(timestamp_seconds, value), ...]` lists that the predictor can run regression on.

**Two modes:**

| Mode | Command | When to use |
|------|---------|------------|
| `http` | direct `httpx` REST call to Prometheus | standalone CLI |
| `grafana_mcp` | `mcp_client.call_tool("grafana", "query_prometheus_range", {...})` | VS Code MCP mode |

**Grafana MCP tools it calls:**
- `query_prometheus_range` — 30-day time series (one call per service per resource)
- `query_prometheus` — instant value for current limits

**Key method:** `collect_all(namespace, lookback_days)` — returns a dict with one entry per service plus `__namespace__` for aggregate totals.

**What to edit:** Nothing unless your Grafana MCP server returns a different JSON structure than the standard frames format — in that case update `_mcp_range()` and `_mcp_instant()` to parse whatever your server returns.

**Standalone test:**
```bash
python scripts/collectors/prometheus_collector.py --namespace payments-prod --url http://prometheus:9090
```

---

### `scripts/collectors/` — `splunk_collector.py`

**What it is:** Fetches daily API call counts per service from Splunk.

**What it does:** Returns `{ "service-name": [("2025-01-15", 12340.0), ...] }` — daily totals per service that the predictor uses to correlate with resource growth.

**Two modes:**

| Mode | When |
|------|------|
| `http` | direct Splunk REST API — creates a search job, polls for completion, fetches results |
| `splunk_mcp` | `mcp_client.call_tool("splunk", "search", {...})` |

**Splunk MCP tool it calls:**
- `search` — runs an SPL query and returns results as a list of row dicts

**Authentication:** Supports both bearer token (`SPLUNK_TOKEN`) and username/password (`SPLUNK_USERNAME` + `SPLUNK_PASSWORD`). Token is preferred.

**What to edit:** Nothing in this file. Edit `references/splunk_queries.yaml` to change what SPL is run.

**Standalone test:**
```bash
python scripts/collectors/splunk_collector.py --namespace payments-prod --url https://splunk:8089 --token your-token
```

---

### `scripts/collectors/` — `ocp_collector.py`

**What it is:** Fetches three things from OCP: namespace resource quota, HPA status for all deployments, and per-deployment resource limits.

**What it does:**

| What it fetches | How (CLI) | How (MCP) |
|----------------|-----------|-----------|
| ResourceQuota | `oc get resourcequota -n X -o json` | `list_resources(kind=ResourceQuota)` |
| All HPAs | `oc get hpa -n X -o json` | `list_resources(kind=HorizontalPodAutoscaler)` |
| Deployments | `oc get deployments -n X -o json` | `list_resources(kind=Deployment)` |

**Key dataclasses:**

`HPAStatus` — one per HPA:
```python
hpa.current_replicas    # how many pods right now
hpa.max_replicas        # the ceiling
hpa.utilization_percent # current/max * 100
hpa.is_at_risk          # True if > 80% of max
```

`NamespaceQuota` — namespace totals:
```python
quota.cpu_hard_cores     # total CPU budget
quota.cpu_used_cores     # currently requested
quota.cpu_used_percent   # used/hard * 100
quota.memory_hard_bytes  # total memory budget
quota.memory_used_bytes  # currently requested
```

**Unit parsing built-in:**
- `_cpu("500m")` → `0.5` cores
- `_mem("512Mi")` → `536870912` bytes

**What to edit:** Nothing. The `oc` CLI commands and MCP tool names are correct for standard OCP/Kubernetes.

**Standalone test:**
```bash
python scripts/collectors/ocp_collector.py --namespace payments-prod --mode cli
```

---

### `scripts/calculators/` — `capacity_predictor.py`

**What it is:** The mathematical core. Pure Python/numpy/scipy — no network calls, no LLM.

**What it does:** Takes a 30-day usage time series and a hard limit, runs `scipy.stats.linregress`, and computes:
- Current usage as a percentage of limit
- Daily growth rate (slope)
- Days until 70% of limit (warn)
- Days until 85% of limit (critical)
- Days until 100% of limit (exhaustion)
- Whether the trend is growing/stable/declining/high_growth
- Whether memory growth looks like a leak (Pearson correlation with API calls)

**The regression:**
```python
slope, intercept, r_value, _, _ = stats.linregress(days, usage_values)
days_to_exhaustion = (limit - current_usage) / slope
```
`r_squared = r_value²` — if below 0.5 the trend is volatile and the prediction is less reliable.

**Memory leak detection:**
```python
# Suspect leak if:
# 1. Memory slope > 2% per day (normalised)
# 2. Pearson correlation between memory and API calls < 0.5
# 3. API calls are flat or declining
```
This is a signal not a diagnosis — the LLM interprets it.

**Namespace aggregation:** `aggregate_namespace()` sums all service predictions and uses the minimum `days_to_exhaustion` as the namespace-level prediction (weakest link rules — one service running out affects the whole namespace quota).

**Key output dataclass: `PredictionResult`**

```python
PredictionResult(
  service="payment-service",
  resource="memory",
  current_percent=71.3,        # at 71.3% of limit right now
  slope_per_day=52_000_000,    # growing 52MB/day
  r_squared=0.94,              # very linear — prediction reliable
  trend="growing",
  days_to_warn=0,              # already past 70%
  days_to_critical=18,         # 85% in 18 days
  days_to_exhaustion=52,       # 100% in 52 days
  severity="WARN",
  is_memory_leak_suspect=False,
)
```

**Self-test with synthetic data:**
```bash
python scripts/calculators/capacity_predictor.py
```

**What to edit:** Nothing. Edit `references/thresholds.yaml` to change the 70%/85%/100% cutoffs or the leak detection sensitivity.

---

### `scripts/calculators/` — `hpa_analyzer.py`

**What it is:** Predicts when a service will hit its HPA `maxReplicas` ceiling. Same regression approach as `capacity_predictor.py` but applied to replica count history.

**What it does:** Takes current replicas, max replicas, and an optional history of replica counts over time. Returns:
- Headroom replicas (`max - current`)
- Headroom percent
- Utilization percent (`current/max * 100`)
- Predicted days until max replicas is hit
- Severity (CRITICAL if > 90% of max, WARN if > 80%)

**Why this matters:** Once `current_replicas == max_replicas`, new load increases latency instead of triggering more pods. This is a silent risk — the service doesn't crash, it just gets slow.

**What to edit:** Nothing. Edit `references/thresholds.yaml` to change the 80%/90% HPA headroom thresholds.

---

### `scripts/reporters/` — `snapshot_store.py`

**What it is:** Saves and loads prediction history as plain JSON files. No database.

**What it does:**

| Method | What it does |
|--------|-------------|
| `save_predictions(predictions, namespace)` | Appends today's predictions to `data/snapshots/payments-prod_YYYY-MM-DD.json` |
| `save_report(namespace, report, narrative)` | Saves full report to `data/reports/payments-prod_YYYYMMDD_HHMMSS.json` |
| `get_trend_history(namespace, service, resource, days)` | Returns one row per day: `{date, current_percent, days_to_exhaustion, severity}` |
| `get_at_risk_services(namespace, days_threshold)` | Returns services with `days_to_exhaustion < threshold` from latest snapshot |

**File format on disk:**
```json
[
  {
    "timestamp": "2025-01-15T09:12:34",
    "predictions": [
      { "service": "payment-service", "resource": "memory",
        "current_percent": 71.3, "days_to_exhaustion": 18, ... }
    ]
  }
]
```

Multiple runs per day append to the same file. The last entry of the day is used for trend history.

**What to edit:** Nothing. Files go into `data/snapshots/` and `data/reports/` automatically.

---

### `skills/capacity/` — `capacity_analysis.md`

**What it is:** The primary LLM instruction file. Pure markdown — no code.

**What it does:** Tells the LLM:
- What every field in the prediction JSON means
- How to identify root cause (leak vs organic growth vs misconfiguration)
- Decision rules table (`days_to_exhaustion < 14` → CRITICAL, etc.)
- Exactly what format to produce (executive summary → risk table → top 3 actions → anomalies)

**What to edit:** Edit this file to change how the LLM interprets data or formats its output. This is the most impactful file for tuning LLM behaviour. No code change needed.

---

### `skills/capacity/` — `hpa_recommendations.md`

**What it is:** Rules for the LLM about how to recommend HPA configuration changes.

**What it does:** Contains:
- When to increase `maxReplicas` and by how much (`ceil(current_max * 1.5)`)
- When to adjust CPU target utilization
- When to raise `minReplicas` (cold-start latency prevention)
- OCP-specific notes (HPA v2, KEDA option)

**What to edit:** Update the HPA tuning rules if your team uses different scaling strategies.

---

### `skills/capacity/` — `namespace_sharing.md`

**What it is:** Gives the LLM context about the shared-namespace risk.

**What it does:** Explains to the LLM that all 4 services compete for the same OCP ResourceQuota, how CPU starvation and memory OOM happen in this scenario, and what remediation options exist (quota increase, LimitRange caps, namespace split, KEDA).

**What to edit:** Update the contention detection rules if your team has specific policies about namespace sharing.

---

### `skills/services/*/` — `profile.md`

**What it is:** Service-specific context for the LLM. One file per microservice.

**What it does:** Tells the LLM things the data can't tell it:
- Service criticality and SLA
- Known traffic patterns (peak hours, month-end spikes)
- Known historical issues (past memory leaks, CPU incidents)
- Resource baseline at normal load

**Files provided:**

| Service | Key context in profile |
|---------|----------------------|
| `payment-service` | CRITICAL, JVM heap watch, known leak in v2.3.x, month-end 2x spike |
| `order-service` | HIGH, mirrors payment-service traffic, DB pool leak history |
| `inventory-service` | MEDIUM, 2 AM bulk sync causes benign CPU spike, don't flag as anomaly |
| `notification-service` | LOW, lightweight, good candidate for namespace split |

**What to edit:** Update these files when you learn new things about your services (new traffic patterns, resolved issues, changed baselines). The LLM uses this context for every analysis run.

---

### `references/` — `prometheus_queries.yaml`

**What it is:** All PromQL queries for all services and namespace totals, in one file.

**What it does:** Every query uses `{namespace}` as a placeholder which `prometheus_collector.py` substitutes at runtime. Queries are organised by service then by resource type.

**Structure per service:**
```yaml
microservices:
  payment-service:
    cpu:
      usage:   'sum(rate(container_cpu_usage_seconds_total{namespace="{namespace}", pod=~"payment-service.*"}[5m]))'
      limit:   'sum(kube_pod_container_resource_limits{namespace="{namespace}", pod=~"payment-service.*", resource="cpu"})'
    memory:
      usage:   'sum(container_memory_working_set_bytes{namespace="{namespace}", pod=~"payment-service.*"})'
      limit:   'sum(kube_pod_container_resource_limits{namespace="{namespace}", pod=~"payment-service.*", resource="memory"})'
    hpa:
      current_replicas: 'kube_horizontalpodautoscaler_status_current_replicas{...}'
      max_replicas:     'kube_horizontalpodautoscaler_spec_max_replicas{...}'
```

**What to edit:** When adding a new service, copy an existing service block and change `pod=~"payment-service.*"` to match your new service's pod name prefix.

**Key PromQL notes:**
- `rate(...[5m])` — smooths CPU spikes over a 5-minute window
- `container_memory_working_set_bytes` — active memory only, excludes cache, correct metric for OOM risk
- `pod=~"service-name.*"` — regex matches all pods belonging to that deployment

---

### `references/` — `splunk_queries.yaml`

**What it is:** All SPL queries for API call growth data.

**What it does:** Each service has a `timechart span=1d count` query that returns daily API call totals. The `{namespace}` and `{lookback_days}` placeholders are substituted by `splunk_collector.py` at runtime.

**What to edit:**
- Change `index=ocp_access_logs` to your actual Splunk index name
- Change `service="payment-service"` field filter to match how your logs identify services
- Add entries for new services

---

### `references/` — `thresholds.yaml`

**What it is:** All numeric thresholds used by the calculators and referenced in skill files.

**What it does:** Single place to change all severity cutoffs:

```yaml
capacity:
  warn_percent: 70              # usage > 70% → WARN
  critical_percent: 85          # usage > 85% → CRITICAL
  days_to_exhaustion_warn: 30   # less than 30 days → WARN
  days_to_exhaustion_critical: 14

hpa:
  scale_headroom_warn: 0.80     # current/max > 80% → WARN
  scale_headroom_critical: 0.90

memory_leak:
  daily_growth_suspect_percent: 2.0
  api_correlation_threshold: 0.5
```

**What to edit:** Change these values to match your team's risk tolerance. No code change needed — all calculators load this file at runtime.

---

### `references/` — `namespace_inventory.yaml`

**What it is:** Maps namespace names to their microservices and quota values.

**What it does:** Tells the agent which services to analyse when you run `--namespace payments-prod`. Also provides quota fallback values when OCP doesn't return quota data.

```yaml
namespaces:
  payments-prod:
    microservices:
      - payment-service
      - order-service
      - inventory-service
      - notification-service
    quota:
      cpu_cores: 16
      memory_gb: 32
      max_pods: 80
```

**What to edit:** Add new namespaces and services here first before creating their queries and profiles.

---

## 5. How to Run

### First run — dry run (no LLM, just numbers)

```bash
cd sre-agent
python agent/capacity_agent.py --namespace payments-prod --dry-run
```

This collects data from all three sources and prints a prediction table — no LLM API key needed.

Expected output:
```
============================================================
Capacity Analysis: payments-prod
============================================================

📋 Step 1: Collecting metrics (Prometheus + Splunk + OCP in parallel)...
   ✅ Prometheus: 4 services | Splunk: 4 services | OCP: 4 HPAs

📋 Step 2: Computing predictions...
  payment-service        CPU= 42.1%(87d)  Mem= 71.3%(18d)  [OK/WARN]
  order-service          CPU= 38.5%(120d) Mem= 55.1%(45d)  [OK/OK]
  inventory-service      CPU= 61.2%(40d)  Mem= 48.7%(62d)  [OK/OK]
  notification-service   CPU= 22.4%(190d) Mem= 31.2%(110d) [OK/OK]

  Namespace CPU=65%  Mem=78%  Severity=WARN

📋 Step 3: Saving snapshots...
   ✅ 8 predictions written to data/snapshots/

📋 Step 4: Dry-run — skipping LLM

📋 Step 5: Saving report...
   ✅ Saved → data/reports/payments-prod_20250115_143022.json
```

### Full run with LLM recommendations

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python agent/capacity_agent.py --namespace payments-prod
```

The LLM adds to the output:
```
### Executive Summary
payment-service memory will hit critical threshold (85%) in 18 days.
API calls are flat — this is a memory leak pattern. Immediate action needed.

### Per-Service Risk Table
| Service | CPU% | Mem% | Days (Mem) | Key Risk |
...

### Top 3 Recommended Actions
1. [CRITICAL] Investigate payment-service heap — memory growing 52MB/day with flat API traffic.
2. [WARN] Raise payment-service HPA maxReplicas from 8 to 12 before month-end.
3. [WARN] Plan memory limit increase for payment-service within 2 weeks.
```

### Other useful commands

```bash
# Analyse one service only
python agent/capacity_agent.py --namespace payments-prod --service payment-service

# Use last 14 days instead of 30
python agent/capacity_agent.py --namespace payments-prod --days 14

# Use MCP mode (VS Code Copilot already started the servers)
python agent/capacity_agent.py \
  --namespace payments-prod \
  --prom-mode grafana_mcp \
  --splunk-mode splunk_mcp \
  --ocp-mode ocp_mcp

# Test individual collectors
python scripts/collectors/prometheus_collector.py --namespace payments-prod
python scripts/collectors/ocp_collector.py --namespace payments-prod
python scripts/collectors/splunk_collector.py --namespace payments-prod

# Test the math with synthetic data (no connection needed)
python scripts/calculators/capacity_predictor.py
```

### From VS Code task runner

`Ctrl+Shift+P` → `Tasks: Run Task` → pick from the list.

---

## 6. Using GitHub Copilot Agent Mode

Open Copilot Chat (`Ctrl+Shift+I`) → set mode to **Agent** → type naturally.

### Quick prompts to copy-paste

```
Check HPA status for all deployments in namespace payments-prod.
Flag any where current replicas is more than 80% of max replicas.
```

```
Query Grafana for memory usage of payment-service pods in namespace payments-prod
over the last 30 days. Plot the trend and tell me if it's growing linearly.
```

```
Search Splunk for daily API call counts per service in namespace payments-prod
for the last 30 days. Is API call growth correlated with memory growth?
```

```
Get the ResourceQuota for namespace payments-prod.
What percentage of CPU and memory is currently consumed?
```

```
Full capacity analysis for payments-prod:
1. Fetch memory and CPU trends from Grafana (30 days)
2. Get HPA status from Kubernetes
3. Get API call volumes from Splunk (30 days)
4. Tell me which service exhausts its memory limit first, whether it looks
   like a leak or organic growth, and what I should do this week.
```

---

## 7. Adding a New Service or Namespace

### Add a new service to an existing namespace

**1 — `references/namespace_inventory.yaml`**
```yaml
namespaces:
  payments-prod:
    microservices:
      - payment-service
      - your-new-service    # ← add here
```

**2 — `references/prometheus_queries.yaml`**

Copy an existing service block and change the pod regex:
```yaml
microservices:
  your-new-service:
    cpu:
      usage: 'sum(rate(container_cpu_usage_seconds_total{namespace="{namespace}", pod=~"your-new-service.*", container!=""}[5m]))'
      limit: 'sum(kube_pod_container_resource_limits{namespace="{namespace}", pod=~"your-new-service.*", resource="cpu"})'
    memory:
      usage: 'sum(container_memory_working_set_bytes{namespace="{namespace}", pod=~"your-new-service.*", container!=""})'
      limit: 'sum(kube_pod_container_resource_limits{namespace="{namespace}", pod=~"your-new-service.*", resource="memory"})'
    hpa:
      current_replicas: 'kube_horizontalpodautoscaler_status_current_replicas{namespace="{namespace}", horizontalpodautoscaler="your-new-service"}'
      max_replicas:     'kube_horizontalpodautoscaler_spec_max_replicas{namespace="{namespace}", horizontalpodautoscaler="your-new-service"}'
      desired_replicas: 'kube_horizontalpodautoscaler_status_desired_replicas{namespace="{namespace}", horizontalpodautoscaler="your-new-service"}'
```

**3 — `references/splunk_queries.yaml`**
```yaml
api_calls:
  per_service_trend:
    your-new-service: |
      index=ocp_access_logs namespace="{namespace}" service="your-new-service"
      | timechart span=1d count as api_calls
```

**4 — Create `skills/services/your-new-service/profile.md`**
```markdown
# Service Profile: your-new-service

## Overview
- **Criticality**: HIGH / MEDIUM / LOW
- **Team**: your-team

## Known Traffic Patterns
- Peak hours: ...

## Resource Profile
- Baseline: ~X CPU cores, ~Y GB memory
```

Run: `python agent/capacity_agent.py --namespace payments-prod`

### Add a new namespace

Add a block to `namespace_inventory.yaml` with its services and quota, add all the service queries, then run:
```bash
python agent/capacity_agent.py --namespace your-new-namespace
```

---

## 8. Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| MCP server not in Tools list | Wrong VS Code version or not in Agent mode | Update to VS Code 1.99+, switch to Agent mode |
| `servers` key not found in mcp.json | Using `mcpServers` instead of `servers` | The root key in VS Code must be `"servers"` not `"mcpServers"` |
| `npx: command not found` | Node.js not installed | `winget install OpenJS.NodeJS.LTS` or `brew install node` |
| `uvx: command not found` | uv not installed | `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Prometheus returns empty series | Wrong pod selector regex | Check `prometheus_queries.yaml` — the `pod=~"service-name.*"` must match your actual pod names exactly. Run `oc get pods -n payments-prod` to see real names |
| Splunk returns no results | Wrong index name | Run `python scripts/collectors/splunk_collector.py` — it will list available indexes |
| `oc: command not found` | oc CLI not installed or not in PATH | Download from your OCP cluster console → Help → CLI Tools |
| `days_to_exhaustion: 9999` | Slope ≤ 0 (not growing) | This is correct — resource is stable or declining, no action needed |
| LLM returns mock response | `ANTHROPIC_API_KEY` not set | `export ANTHROPIC_API_KEY=sk-ant-...` |
| `ModuleNotFoundError: numpy` | Dependencies not installed | `cd sre-agent && pip install -r requirements.txt` |
| Wrong namespace in output | `OCP_NAMESPACE` env var pointing elsewhere | Pass `--namespace your-namespace` explicitly |
| VS Code credential prompt not appearing | Cached bad value | `Ctrl+Shift+P` → `MCP: Clear Secrets` → restart VS Code |
| All services show OK but you know something is wrong | Thresholds too loose | Lower `warn_percent` in `references/thresholds.yaml` |

---

*All data stays local — nothing leaves your machine except the LLM prompt when `ANTHROPIC_API_KEY` is set.*
