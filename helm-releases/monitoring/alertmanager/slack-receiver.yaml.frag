  - name: slack-oncall
    slack_configs:
      - api_url: "${SLACK_WEBHOOK_URL}"
        channel: "${SLACK_CHANNEL}"
        send_resolved: true
        title: '[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }} ({{ .CommonLabels.namespace }})'
        text: >-
          {{ range .Alerts }}*{{ .Labels.severity | default "info" }}* {{ .Annotations.summary | default .Labels.alertname }}
          {{ .Annotations.description }}
          {{ end }}
