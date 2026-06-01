# Quickstart — 5 minutes to first report

## 1. Install

```bash
pip install httpx pyyaml numpy scipy anthropic
```

## 2. Tell it where your systems are

Edit **one file**: `references/namespace_inventory.yaml`

```yaml
namespaces:
  your-namespace:           # ← your actual OCP namespace
    microservices:
      - payment-service     # ← your actual service names
      - order-service
```

Then set environment variables:

```bash
export PROMETHEUS_URL=http://your-prometheus:9090
export SPLUNK_URL=https://your-splunk:8089
export SPLUNK_TOKEN=your-token
export ANTHROPIC_API_KEY=sk-ant-...    # for LLM interpretation
```

## 3. Run

```bash
# Full analysis (collects data + predicts + LLM report)
python agent/capacity_agent.py --namespace your-namespace

# Just the math, no LLM (fastest, no API key needed)
python agent/capacity_agent.py --namespace your-namespace --dry-run

# One service only
python agent/capacity_agent.py --namespace your-namespace --service payment-service
```

## 4. Read the output

Results appear in the console **and** saved to `data/reports/`.

```
payment-service: CPU=42.1% (87d), Mem=71.3% (18d), Severity=OK/WARN
order-service:   CPU=38.5% (120d), Mem=55.1% (45d), Severity=OK/OK
...
Namespace: CPU=65%, Mem=78%, Overall=WARN
```

Then the LLM gives you:
- Why memory is growing (leak vs organic?)
- Which service will blow up first
- Top 3 actions with priority

---

## Using your VS Code MCP servers

You already have OCP MCP, Grafana MCP, and Splunk MCP in VS Code.
Switch the collectors to use them instead of direct HTTP:

```bash
python agent/capacity_agent.py \
  --namespace your-namespace \
  --prom-mode grafana_mcp \
  --splunk-mode splunk_mcp \
  --ocp-mode ocp_mcp
```

Then in each collector file, find `_call_mcp()` and update the command name
to match your VS Code MCP server config (check `.vscode/settings.json` for
the `mcpServers` entry names).

---

## Add a new service

1. Add it to `references/namespace_inventory.yaml`
2. Add its PromQL queries to `references/prometheus_queries.yaml` (copy an existing service block, change the pod selector)
3. Add its Splunk query to `references/splunk_queries.yaml` (copy and change service name)
4. Optionally create `skills/services/your-service/profile.md` for LLM context

That's it. Run again.

---

## Add a new namespace

Add a block to `references/namespace_inventory.yaml` and run with `--namespace new-namespace`.

---

## What gets stored

All output goes to `data/` — plain JSON files, nothing else needed.

```
data/
  snapshots/   ← daily prediction files (one JSON per namespace per day)
  reports/     ← full reports per run
```
