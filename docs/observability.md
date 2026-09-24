# Shared observability stack (`monitoring` namespace)

The shared observability stack gives every tenant namespace (`otterworks-<id>`)
metrics, alerting, dashboards and distributed tracing without installing
anything cluster-scoped themselves.

## What is deployed

| Component | Helm chart / manifest | Release | Notes |
|---|---|---|---|
| Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics, Prometheus Operator | `prometheus-community/kube-prometheus-stack` | `prometheus` | values: `helm-releases/monitoring/prometheus/values.yaml` + `helm-releases/monitoring/grafana/values.yaml` |
| Jaeger (all-in-one, in-memory) | `jaegertracing/jaeger` | `jaeger` | values: `helm-releases/monitoring/jaeger/values.yaml` |
| OpenTelemetry Collector (deployment mode) | `open-telemetry/opentelemetry-collector` | `otel-collector` | values: `helm-releases/monitoring/otel-collector/values.yaml` |
| Alertmanager config | Secret `alertmanager-config`, rendered from `helm-releases/monitoring/alertmanager/alertmanager.yaml.tpl` | n/a | webhook URLs injected at apply time |
| `alert-sink` | `helm-releases/monitoring/alertmanager/alert-sink.yaml` | n/a | ClusterIP HTTP echo server used as the default webhook target |

Chart versions are pinned in `scripts/deploy-observability.sh`
(`KPS_CHART_VERSION`, `JAEGER_CHART_VERSION`, `OTEL_CHART_VERSION`).

All Services are `ClusterIP`. The only `LoadBalancer` on the cluster remains
`ingress-nginx/ingress-nginx-controller`; the deploy script aborts if any other
one exists. Control-plane scrapers that do not work on EKS (`kubeEtcd`,
`kubeControllerManager`, `kubeScheduler`, `kubeProxy`) are disabled.

## Deploying / updating

```bash
# whole stack, idempotent
./scripts/deploy-observability.sh

# with real webhook targets
DEVIN_WEBHOOK_URL='https://.../api/webhooks/automations/<org>/<automation>' \
DEVIN_WEBHOOK_SECRET='<secret returned when the Automation was created>' \
SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...' \
./scripts/deploy-observability.sh
```

`scripts/deploy-dev.sh` calls the same script, so a fresh cluster gets the stack
in one run.

Environment variables understood by the script:

| Variable | Default | Purpose |
|---|---|---|
| `DEVIN_WEBHOOK_URL` | `http://alert-sink.monitoring.svc.cluster.local:8080/devin-automation` | `devin-automation` receiver |
| `DEVIN_WEBHOOK_SECRET` | `unset` | sent as `X-Webhook-Secret` on every `devin-automation` delivery (Devin Automations reject deliveries without it) |
| `SLACK_WEBHOOK_URL` | unset | `slack-oncall` receiver (`slack_configs.api_url`). When unset the `slack-oncall` route and receiver are omitted from the rendered config: the Slack notifier treats any reply other than Slack's `ok` as a failed send, so pointing it at the echo sink would fail 100% of deliveries and page `AlertmanagerFailedToSendAlerts`. |
| `SLACK_CHANNEL` | `#oncall` | channel for the `slack-oncall` receiver |
| `GRAFANA_ADMIN_PASSWORD` | random, generated once | Grafana `admin` password (Secret `grafana-admin`) |
| `OBS_BASIC_AUTH_USER` / `OBS_BASIC_AUTH_PASSWORD` | `ops` / random, generated once | nginx basic auth for Prometheus, Alertmanager, Jaeger (Secret `observability-basic-auth`) |

Secrets are only (re)written when missing or when the corresponding variable is
set, so re-running the script never rotates credentials by accident. Nothing
secret is committed to the repo.

## Hostnames

All ingresses use class `nginx` and the existing wildcard certificate
(`*.otterworks.app`, issued by cert-manager, default certificate of ingress-nginx).
DNS records are created by external-dns. The external-dns deployment on the
cluster whitelists hostnames with explicit `--domain-filter=<host>` args, so the
deploy script appends one filter per observability host (idempotent, only when
missing) and waits for the rollout.

| URL | Auth |
|---|---|
| https://grafana.otterworks.app | Grafana login (`admin` / Secret `grafana-admin`) |
| https://prometheus.otterworks.app | nginx basic auth (Secret `observability-basic-auth`) |
| https://alertmanager.otterworks.app | nginx basic auth |
| https://jaeger.otterworks.app | nginx basic auth |

Retrieve credentials:

```bash
kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl -n monitoring get secret observability-basic-auth -o jsonpath='{.data.password}' | base64 -d; echo
```

Port-forward alternative (no ingress/auth involved):

```bash
kubectl -n monitoring port-forward svc/prometheus-grafana 3000:80
kubectl -n monitoring port-forward svc/prometheus-prometheus 9090:9090
kubectl -n monitoring port-forward svc/prometheus-alertmanager 9093:9093
kubectl -n monitoring port-forward svc/jaeger 16686:16686
```

## How a tenant opts in

Prometheus is configured with
`serviceMonitorSelectorNilUsesHelmValues: false`,
`podMonitorSelectorNilUsesHelmValues: false`,
`ruleSelectorNilUsesHelmValues: false` and empty namespace selectors, so **any**
`ServiceMonitor`, `PodMonitor` or `PrometheusRule` in **any** namespace is picked
up — no special label is needed.

### Metrics — `ServiceMonitor`

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: document-service
  namespace: otterworks-<id>
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: document-service
      app.kubernetes.io/instance: <release>
  endpoints:
    - port: http
      path: /metrics
      interval: 30s
```

The OtterWorks `document-service` chart already ships this ServiceMonitor
(`monitoring.enabled: true`, path `/metrics`); it carries only the standard
`app.kubernetes.io/name` / `app.kubernetes.io/instance` labels, which is fine
because no selector label is required.

### Alerts — `PrometheusRule`

Alerts labelled `page: devin` are routed to the `devin-automation` webhook and,
when `SLACK_WEBHOOK_URL` is set, **also** to the `slack-oncall` receiver
(`continue: true`). Everything else goes to the `null` receiver. Grouping: `alertname,namespace`; `group_wait: 10s`,
`group_interval: 30s`, `repeat_interval: 1h`.

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: document-service
  namespace: otterworks-<id>
spec:
  groups:
    - name: document-service
      rules:
        - alert: DocumentServiceHighErrorRate
          expr: sum(rate(http_requests_total{namespace="otterworks-<id>",status=~"5.."}[5m])) > 0.1
          for: 2m
          labels:
            severity: critical
            page: devin
          annotations:
            summary: "document-service 5xx rate above threshold"
```

Fire a test alert by hand:

```bash
kubectl -n monitoring port-forward svc/prometheus-alertmanager 9093:9093 &
curl -sS -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{
  "labels": {"alertname":"ManualTest","namespace":"otterworks-main","page":"devin","severity":"critical"},
  "annotations": {"summary":"manual test"}}]'
kubectl -n monitoring logs deploy/alert-sink --tail=20   # when the sink is the target
```

### Dashboards — ConfigMap

Grafana's sidecar watches **all namespaces** for ConfigMaps labelled
`grafana_dashboard: "1"`. Optional annotation `grafana_folder` sets the folder.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: document-service-dashboard
  namespace: otterworks-<id>
  labels:
    grafana_dashboard: "1"
  annotations:
    grafana_folder: otterworks-<id>
data:
  document-service.json: |
    { ...grafana dashboard JSON... }
```

Provisioned datasources: `Prometheus` (default), `Alertmanager`, `Jaeger`.

### Traces — OTLP endpoint

```yaml
env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: http://otel-collector.monitoring.svc.cluster.local:4318   # OTLP/HTTP
  # gRPC: otel-collector.monitoring.svc.cluster.local:4317
  - name: OTEL_SERVICE_NAME
    value: document-service
```

The collector batches and forwards traces to Jaeger over OTLP gRPC. Jaeger keeps
the most recent 50k traces in memory (lost on restart).

Jaeger v2 exposes the query API under `/api/v3` (the v1 `/api/traces` path is not
served):

```bash
kubectl -n monitoring port-forward svc/jaeger 16686:16686 &
curl -s 'localhost:16686/api/v3/services'
curl -s "localhost:16686/api/v3/traces?query.service_name=document-service&query.start_time_min=$(date -u -d '-15 min' +%FT%TZ)&query.start_time_max=$(date -u +%FT%TZ)"
```

### Network policy

Tenant NetworkPolicies allow ingress from namespaces labelled
`kubernetes.io/metadata.name: monitoring` (and `ingress-nginx`). Kubernetes sets
that label automatically; the deploy script additionally labels the namespace
`app.kubernetes.io/part-of=platform` and `otterworks.app/component=observability`.
Nothing is required on the tenant side beyond the existing policy.

## Alertmanager configuration mechanism

`alertmanager.alertmanagerSpec.configSecret: alertmanager-config` is used rather
than `AlertmanagerConfig` CRDs or the chart's inline `alertmanager.config`:

* the full config is a single file, so `page="devin"` routing and the
  `continue: true` fan-out are explicit and reviewable in
  `helm-releases/monitoring/alertmanager/alertmanager.yaml.tpl`;
* the Secret is rendered with `envsubst` at apply time, so the webhook URLs
  never appear in git or in Helm release history;
* `AlertmanagerConfig` CRDs are namespaced and get their matchers rewritten with
  a `namespace` label, which would break a cluster-wide `page` route.

## Rotating the webhook URLs

```bash
DEVIN_WEBHOOK_URL='https://.../new' DEVIN_WEBHOOK_SECRET='...' SLACK_WEBHOOK_URL='https://hooks.slack.com/...' \
  ./scripts/deploy-observability.sh
```

or only the Secret, without touching Helm releases:

```bash
export DEVIN_WEBHOOK_URL='https://.../new' DEVIN_WEBHOOK_SECRET='...' SLACK_WEBHOOK_URL='https://hooks.slack.com/...' SLACK_CHANNEL='#oncall'
export SLACK_ROUTE="$(<helm-releases/monitoring/alertmanager/slack-route.yaml.frag)"   # "" to omit Slack
export SLACK_RECEIVER="$(envsubst '${SLACK_WEBHOOK_URL} ${SLACK_CHANNEL}' <helm-releases/monitoring/alertmanager/slack-receiver.yaml.frag)"
envsubst '${DEVIN_WEBHOOK_URL} ${DEVIN_WEBHOOK_SECRET} ${SLACK_ROUTE} ${SLACK_RECEIVER}' \
  < helm-releases/monitoring/alertmanager/alertmanager.yaml.tpl \
  | kubectl -n monitoring create secret generic alertmanager-config \
      --from-file=alertmanager.yaml=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f -
```

The config-reloader sidecar picks the change up within ~1 minute; verify with
`kubectl -n monitoring exec alertmanager-prometheus-alertmanager-0 -c alertmanager -- amtool check-config /etc/alertmanager/config_out/alertmanager.env.yaml`.

## Resource footprint (requests / limits)

| Pod | CPU | Memory | Storage |
|---|---|---|---|
| prometheus | 200m / 1000m | 768Mi / 1536Mi | 20Gi PVC, 7d retention |
| alertmanager | 25m / 200m | 64Mi / 128Mi | emptyDir |
| grafana | 50m / 500m | 192Mi / 512Mi | 5Gi PVC |
| prometheus-operator | 50m / 200m | 64Mi / 128Mi | – |
| kube-state-metrics | 20m / 100m | 48Mi / 128Mi | – |
| node-exporter (per node) | 20m / 100m | 32Mi / 64Mi | – |
| jaeger | 50m / 500m | 128Mi / 512Mi | in-memory |
| otel-collector | 50m / 500m | 128Mi / 256Mi | – |
| alert-sink | 10m / 50m | 16Mi / 32Mi | – |

Total requests ≈ 0.5 vCPU / 1.4Gi (single replicas everywhere). No node-group
changes were needed on `otterworks-dev`.

## Removal

```bash
helm -n monitoring uninstall otel-collector jaeger prometheus
kubectl -n monitoring delete -f helm-releases/monitoring/alertmanager/alert-sink.yaml
kubectl delete ns monitoring          # also deletes the PVCs and Secrets
kubectl delete crd -l app.kubernetes.io/name=kube-prometheus-stack-prometheus-operator   # optional, removes monitoring.coreos.com CRDs
```

Removing the CRDs deletes every tenant `ServiceMonitor`/`PrometheusRule`, so
leave them in place unless the whole platform is being torn down.
