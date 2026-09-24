# Alertmanager configuration template.
#
# Rendered by scripts/deploy-observability.sh with envsubst into the Secret
# `alertmanager-config` (key: alertmanager.yaml) in the monitoring namespace,
# referenced by alertmanager.alertmanagerSpec.configSecret in
# helm-releases/monitoring/prometheus/values.yaml.
#
# Variables (from the environment at apply time, never committed):
#   DEVIN_WEBHOOK_URL     Devin Automation incoming-webhook URL
#   DEVIN_WEBHOOK_SECRET  the Automation's one-time webhook secret, sent as X-Webhook-Secret
#   SLACK_WEBHOOK_URL     Slack incoming-webhook URL
# When unset, both URLs default to the in-cluster alert-sink so the config still
# validates and deliveries can be observed in `kubectl logs deploy/alert-sink`.
global:
  resolve_timeout: 5m

route:
  receiver: "null"
  group_by: ["alertname", "namespace"]
  group_wait: 10s
  group_interval: 30s
  repeat_interval: 1h
  routes:
    - receiver: devin-automation
      matchers:
        - page = "devin"
      continue: true
    - receiver: slack-oncall
      matchers:
        - page = "devin"
      continue: true

receivers:
  - name: "null"
  - name: devin-automation
    webhook_configs:
      - url: "${DEVIN_WEBHOOK_URL}"
        # firing only: a resolved notification must never start a responder session
        send_resolved: false
        max_alerts: 0
        http_config:
          http_headers:
            X-Webhook-Secret:
              secrets: ["${DEVIN_WEBHOOK_SECRET}"]
  - name: slack-oncall
    slack_configs:
      - api_url: "${SLACK_WEBHOOK_URL}"
        channel: "#oncall"
        send_resolved: true
        title: '[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }} ({{ .CommonLabels.namespace }})'
        text: >-
          {{ range .Alerts }}*{{ .Labels.severity | default "info" }}* {{ .Annotations.summary | default .Labels.alertname }}
          {{ .Annotations.description }}
          {{ end }}

inhibit_rules:
  - source_matchers:
      - severity = "critical"
    target_matchers:
      - severity = "warning"
    equal: ["alertname", "namespace"]
