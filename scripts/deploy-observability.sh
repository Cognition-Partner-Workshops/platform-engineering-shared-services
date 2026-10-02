#!/usr/bin/env bash
################################################################################
# Install / upgrade the shared observability stack in the `monitoring` namespace.
# Idempotent: safe to re-run; only the Helm releases and Secrets below are touched.
#
#   - kube-prometheus-stack (release "prometheus"): Prometheus, Alertmanager,
#     Grafana, node-exporter, kube-state-metrics
#   - Alertmanager routing rendered from
#     helm-releases/monitoring/alertmanager/alertmanager.yaml.tpl
#   - alert-sink: in-cluster placeholder webhook receiver
#   - Jaeger (all-in-one, in-memory) + OpenTelemetry Collector
#
# Environment (all optional):
#   DEVIN_WEBHOOK_URL       Devin Automation incoming-webhook URL for page=devin alerts
#                           (defaults to http://alert-sink.monitoring.svc:8080/...)
#   DEVIN_WEBHOOK_SECRET    the Automation's webhook secret (sent as X-Webhook-Secret)
#   SLACK_WEBHOOK_URL       Slack incoming-webhook URL for page=devin alerts; when
#                           unset the slack-oncall route is omitted entirely
#   SLACK_CHANNEL           Slack channel for slack-oncall (default: #oncall)
#   GRAFANA_ADMIN_PASSWORD  Grafana admin password (generated on first run if unset)
#   OBS_BASIC_AUTH_USER     Basic-auth user for prometheus/alertmanager/jaeger ingresses (default: ops)
#   OBS_BASIC_AUTH_PASSWORD Basic-auth password (generated on first run if unset)
#   KUBE_CONTEXT            kubectl context to use (default: current context)
#
# Usage:
#   DEVIN_WEBHOOK_URL=https://... SLACK_WEBHOOK_URL=https://hooks.slack.com/... \
#     ./scripts/deploy-observability.sh
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VALUES_DIR="$REPO_ROOT/helm-releases/monitoring"
NAMESPACE="monitoring"

KPS_CHART_VERSION="${KPS_CHART_VERSION:-91.5.1}"
JAEGER_CHART_VERSION="${JAEGER_CHART_VERSION:-4.14.0}"
OTEL_CHART_VERSION="${OTEL_CHART_VERSION:-0.173.1}"

SINK_BASE="http://alert-sink.${NAMESPACE}.svc.cluster.local:8080"
export DEVIN_WEBHOOK_URL="${DEVIN_WEBHOOK_URL:-${SINK_BASE}/devin-automation}"
export DEVIN_WEBHOOK_SECRET="${DEVIN_WEBHOOK_SECRET:-unset}"
export SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
export SLACK_CHANNEL="${SLACK_CHANNEL:-#oncall}"

KUBECTL=(kubectl)
HELM=(helm)
if [[ -n "${KUBE_CONTEXT:-}" ]]; then
  KUBECTL+=(--context "$KUBE_CONTEXT")
  HELM+=(--kube-context "$KUBE_CONTEXT")
fi

log() { echo "==> $*"; }

random_secret() {
  # 32 url-safe chars
  openssl rand -base64 64 | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 32
}

for bin in kubectl helm envsubst openssl jq; do
  command -v "$bin" >/dev/null || { echo "missing required tool: $bin" >&2; exit 1; }
done

echo "=========================================="
echo "  Shared observability stack — ${NAMESPACE}"
echo "  context: $("${KUBECTL[@]}" config current-context)"
echo "=========================================="

################################################################################
# Guard: ingress-nginx must be the only LoadBalancer Service on the cluster.
# Nothing in this stack may create one.
################################################################################
log "Checking LoadBalancer Services..."
OTHER_LBS="$("${KUBECTL[@]}" get svc -A \
  -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' \
  | grep -v '^ingress-nginx/' || true)"
if [[ -n "$OTHER_LBS" ]]; then
  echo "ERROR: unexpected LoadBalancer Services present (only ingress-nginx is allowed):" >&2
  echo "$OTHER_LBS" >&2
  exit 1
fi

################################################################################
# Namespace + Secrets
################################################################################
log "Ensuring namespace ${NAMESPACE}..."
"${KUBECTL[@]}" create namespace "$NAMESPACE" --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
# Tenant NetworkPolicies allow scrape traffic from
# namespaceSelector kubernetes.io/metadata.name=monitoring (set automatically by
# the API server); the label below is an explicit, selectable marker as well.
"${KUBECTL[@]}" label namespace "$NAMESPACE" \
  app.kubernetes.io/part-of=observability platform/team=platform --overwrite >/dev/null

log "Rendering Alertmanager config Secret (alertmanager-config)..."
# The Slack notifier treats any reply other than Slack's own `ok` as a failed
# send, so the slack-oncall route is only rendered when a real webhook is set.
if [[ -n "$SLACK_WEBHOOK_URL" ]]; then
  SLACK_ROUTE="$(<"$VALUES_DIR/alertmanager/slack-route.yaml.frag")"
  # shellcheck disable=SC2016  # envsubst takes the literal variable list
  SLACK_RECEIVER="$(envsubst '${SLACK_WEBHOOK_URL} ${SLACK_CHANNEL}' <"$VALUES_DIR/alertmanager/slack-receiver.yaml.frag")"
else
  SLACK_ROUTE=""
  SLACK_RECEIVER=""
fi
export SLACK_ROUTE SLACK_RECEIVER
# shellcheck disable=SC2016  # envsubst takes the literal variable list
envsubst '${DEVIN_WEBHOOK_URL} ${DEVIN_WEBHOOK_SECRET} ${SLACK_ROUTE} ${SLACK_RECEIVER}' \
  <"$VALUES_DIR/alertmanager/alertmanager.yaml.tpl" \
  | "${KUBECTL[@]}" -n "$NAMESPACE" create secret generic alertmanager-config \
      --from-file=alertmanager.yaml=/dev/stdin --dry-run=client -o yaml \
  | "${KUBECTL[@]}" apply -f -
[[ "$DEVIN_WEBHOOK_URL" == "$SINK_BASE"* ]] && echo "    DEVIN_WEBHOOK_URL unset -> routing devin-automation to alert-sink"
[[ -z "$SLACK_WEBHOOK_URL" ]] && echo "    SLACK_WEBHOOK_URL unset -> slack-oncall route omitted"

if ! "${KUBECTL[@]}" -n "$NAMESPACE" get secret grafana-admin >/dev/null 2>&1 || [[ -n "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
  log "Writing Grafana admin Secret (grafana-admin)..."
  "${KUBECTL[@]}" -n "$NAMESPACE" create secret generic grafana-admin \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="${GRAFANA_ADMIN_PASSWORD:-$(random_secret)}" \
    --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
fi

if ! "${KUBECTL[@]}" -n "$NAMESPACE" get secret observability-basic-auth >/dev/null 2>&1 || [[ -n "${OBS_BASIC_AUTH_PASSWORD:-}" ]]; then
  log "Writing ingress basic-auth Secret (observability-basic-auth)..."
  BA_USER="${OBS_BASIC_AUTH_USER:-ops}"
  BA_PASS="${OBS_BASIC_AUTH_PASSWORD:-$(random_secret)}"
  # ingress-nginx expects an htpasswd file under the key "auth"
  HTPASSWD="${BA_USER}:$(openssl passwd -apr1 "$BA_PASS")"
  "${KUBECTL[@]}" -n "$NAMESPACE" create secret generic observability-basic-auth \
    --from-literal=auth="$HTPASSWD" \
    --from-literal=username="$BA_USER" \
    --from-literal=password="$BA_PASS" \
    --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -
fi

log "Applying alert-sink..."
"${KUBECTL[@]}" apply -f "$VALUES_DIR/alertmanager/alert-sink.yaml"

################################################################################
# external-dns: the deployment on the cluster restricts record creation with an
# explicit --domain-filter per hostname, so the observability hosts must be
# added or their ingresses never get DNS records.
################################################################################
OBS_HOSTS=(grafana.otterworks.app prometheus.otterworks.app alertmanager.otterworks.app jaeger.otterworks.app)
if "${KUBECTL[@]}" -n external-dns get deploy external-dns >/dev/null 2>&1; then
  log "Ensuring external-dns domain filters cover observability hosts..."
  CUR_ARGS="$("${KUBECTL[@]}" -n external-dns get deploy external-dns -o jsonpath='{.spec.template.spec.containers[0].args}')"
  MISSING=()
  for h in "${OBS_HOSTS[@]}"; do
    grep -q "\"--domain-filter=${h}\"" <<<"$CUR_ARGS" || MISSING+=("--domain-filter=${h}")
  done
  if ((${#MISSING[@]})); then
    PATCH="$(printf '%s\n' "${MISSING[@]}" | jq -Rc '{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":.}' | jq -sc .)"
    "${KUBECTL[@]}" -n external-dns patch deploy external-dns --type=json -p "$PATCH"
    "${KUBECTL[@]}" -n external-dns rollout status deploy/external-dns --timeout=2m
  else
    echo "    already present"
  fi
fi

################################################################################
# Helm releases
################################################################################
log "Adding Helm repos..."
"${HELM[@]}" repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
"${HELM[@]}" repo add jaegertracing https://jaegertracing.github.io/helm-charts >/dev/null 2>&1 || true
"${HELM[@]}" repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
"${HELM[@]}" repo update >/dev/null

log "Installing kube-prometheus-stack ${KPS_CHART_VERSION} (release: prometheus)..."
"${HELM[@]}" upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --version "$KPS_CHART_VERSION" \
  -f "$VALUES_DIR/prometheus/values.yaml" \
  -f "$VALUES_DIR/grafana/values.yaml" \
  -n "$NAMESPACE" --wait --timeout 10m

log "Installing Jaeger ${JAEGER_CHART_VERSION} (release: jaeger)..."
"${HELM[@]}" upgrade --install jaeger jaegertracing/jaeger \
  --version "$JAEGER_CHART_VERSION" \
  -f "$VALUES_DIR/jaeger/values.yaml" \
  -n "$NAMESPACE" --wait --timeout 5m

log "Installing OpenTelemetry Collector ${OTEL_CHART_VERSION} (release: otel-collector)..."
"${HELM[@]}" upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  --version "$OTEL_CHART_VERSION" \
  -f "$VALUES_DIR/otel-collector/values.yaml" \
  -n "$NAMESPACE" --wait --timeout 5m

################################################################################
# Summary
################################################################################
echo ""
echo "=========================================="
echo "  Observability stack deployed"
echo "=========================================="
"${HELM[@]}" ls -n "$NAMESPACE"
echo ""
echo "Hosts (TLS via the ingress-nginx wildcard certificate):"
echo "  https://grafana.otterworks.app       (Grafana login)"
echo "  https://prometheus.otterworks.app    (basic auth)"
echo "  https://alertmanager.otterworks.app  (basic auth)"
echo "  https://jaeger.otterworks.app        (basic auth)"
echo ""
echo "OTLP endpoint for tenants:"
echo "  OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.${NAMESPACE}.svc.cluster.local:4318"
echo ""
echo "Credentials:"
echo "  kubectl -n ${NAMESPACE} get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo"
echo "  kubectl -n ${NAMESPACE} get secret observability-basic-auth -o jsonpath='{.data.password}' | base64 -d; echo"
