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
#   SLACK_ROUTE / SLACK_RECEIVER
#                         the slack-oncall route and receiver blocks (from
#                         slack-route.yaml.frag / slack-receiver.yaml.frag);
#                         empty when SLACK_WEBHOOK_URL is unset, because the
#                         Slack notifier rejects any reply that is not Slack's
#                         `ok`, so pointing it at the echo sink fails every send
# When DEVIN_WEBHOOK_URL is unset it defaults to the in-cluster alert-sink so the
# config still validates and deliveries can be observed in
# `kubectl logs deploy/alert-sink`.
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
${SLACK_ROUTE}

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
            # `values`, not `secrets`: prometheus-operator re-marshals this file and
            # writes Secret-typed fields back as the literal string "<secret>".
            # The whole file already lives in a Kubernetes Secret.
            X-Webhook-Secret:
              values: ["${DEVIN_WEBHOOK_SECRET}"]
${SLACK_RECEIVER}

inhibit_rules:
  - source_matchers:
      - severity = "critical"
    target_matchers:
      - severity = "warning"
    equal: ["alertname", "namespace"]
