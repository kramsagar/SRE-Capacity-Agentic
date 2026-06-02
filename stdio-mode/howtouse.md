python scripts/collectors/ocp_collector.py `
  --cluster https://api.your-cluster.example.com:6443 `
  --token sha256~xxxx `
  --namespace xyz-pp1 `
  --no-verify --debug

$env:OCP_API_URL="https://api.your-cluster.example.com:6443"  # oc whoami --show-server
$env:OCP_TOKEN="sha256~xxxxxxxxxxxx"                            # oc whoami -t
$env:OCP_MODE="ocp_mcp"
$env:OCP_MCP_SERVER="ocp-mcp"   # must match key in mcp.json servers block

$env:SPLUNK_HOST="https://xyz.splunkcloud.com:8089"
$env:SPLUNK_TOKEN="eyJraW..."
$env:SPLUNK_APP="appsearch"

# Quickest sanity check — single SPL
python scripts/test_splunk_collector.py --mode http --single-spl "index=igft | head 5"

# Full run — all services, 14 days
python scripts/test_splunk_collector.py --mode http --namespace pp-prod --days 14

# With full debug logging
python scripts/test_splunk_collector.py --mode http --debug

python scripts/test_splunk_collector.py --mode mcp_mock `
  --server-script ..\splunk-mcp\server.py `
  --namespace pp-prod --days 14



python scripts/test_splunk_collector.py --mode show_queries --namespace pp-prod --days 30


$env:GRAFANA_URL="https://xyz.abc.net"
$env:GRAFANA_TOKEN="glsa_LZ..."          # your service account token
$env:GRAFANA_DS_UID="hyaV9HiVz"         # your Prometheus uid

python scripts/health_check.py --namespace alprc-prod
