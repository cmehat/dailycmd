---
layout: post
title: "A readable Alertmanager Slack template — and how to ship it on Kubernetes"
date: 2026-07-14 16:00:00 +0000
categories: [observability, kubernetes]
tags: [alertmanager, prometheus, slack, kubernetes, helm, kube-prometheus-stack, go-template]
---

Out of the box, Alertmanager's Slack notifications are close to useless: a wall
of grey text, no color, no obvious "what/where/how-bad", and no way to act
without leaving Slack. A few dozen lines of Go template turn them into something
an on-call actually wants to receive — colored by severity, titled with a
one-line subject, the key labels surfaced, and buttons that jump straight to the
runbook, the query, the dashboard, and a pre-filled silence.

Here's the template I use, commented, followed by how to actually get it into a
Kubernetes Alertmanager with the prometheus-community charts.

## The template

Alertmanager templates are [Go `text/template`](https://pkg.go.dev/text/template).
Each `{{ define "name" }}…{{ end }}` block is a named snippet you reference from
the receiver config. Comments are `{{/* … */}}`.

```gotemplate
{{/* Emoji chosen by severity — the at-a-glance signal in the Slack title. */}}
{{ define "__slack_severity_icon" -}}
  {{ if ne .Status "firing" -}}
  {{- else if eq (.CommonLabels.severity | toLower) "critical" "high" -}}
:fire:
  {{- else if eq (.CommonLabels.severity | toLower) "warning" -}}
:warning:
  {{- else if eq (.CommonLabels.severity | toLower) "info" "low" -}}
:information_source:
  {{- else -}}
:question:
  {{- end -}}
{{- end }}

{{/* Slack attachment color. Resolved alerts are always green; firing alerts
     are yellow for warnings and red for everything more severe. */}}
{{ define "__slack_color" -}}
{{ if eq .Status "firing" -}}
  {{ if eq (.CommonLabels.severity | toLower) "warning" -}}warning
  {{- else -}}danger{{- end -}}
{{ else -}}good{{- end }}
{{- end }}

{{/* A deep link that opens Alertmanager's "new silence" form pre-filled with
     this alert's label matchers — one click to acknowledge from Slack.
     It URL-encodes every common label into the filter expression. */}}
{{ define "__alert_silence_link" }}
{{- .ExternalURL }}/#/silences/new?comment=ack-from-slack&filter=%7B
{{- range .CommonLabels.SortedPairs -}}
  {{- if ne .Name "alertname" -}}
{{- .Name }}%3D"{{- .Value | urlquery | reReplaceAll "\\+" "%20" -}}"%2C%20
  {{- end -}}
{{- end -}}
alertname%3D"{{ .CommonLabels.alertname | urlquery | reReplaceAll "\\+" "%20" }}"%7D
{{- end -}}

{{/* One-line subject: "<AlertName> is CRITICAL on prod [FIRING:2]".
     Uses a top-level grouping label — here `environment` — adapt to yours. */}}
{{ define "__subject" }}
{{- $env := (index .Alerts 0).Labels.environment -}}
{{ (index .Alerts 0).Labels.alertname }} is
{{- if eq .Status "firing" }} {{ (index .Alerts 0).Labels.severity | toUpper }}
{{- else }} OK{{ end }}
{{- if $env }} on {{ $env | toLower }}{{ end }}
 [{{ .Status | toUpper }}{{ if eq .Status "firing" }}:{{ .Alerts.Firing | len }}{{ end }}]
{{ end }}

{{/* Title = severity icon + subject. This is the bold line in Slack. */}}
{{ define "__slack_title" }}
{{ template "__slack_severity_icon" . -}} {{ template "__subject" . }}
{{- if .CommonLabels.job }} · {{ .CommonLabels.job }}{{ end }}
{{ end }}

{{/* Body. A header block of the most useful labels (only rendered when
     present), then per-alert: summary (bold), description, start time, and a
     full label dump for context. Keep summary short; put detail in
     description. */}}
{{ define "__slack_text" }}
{{ with index .Alerts 0 -}}
{{- if .Labels.environment }}:earth_africa: *environment:* `{{ .Labels.environment }}`
{{ end -}}
{{- if .Labels.instance }}:computer: *instance:* `{{ .Labels.instance }}`
{{ end -}}
{{- if .Labels.job }}:microscope: *job:* `{{ .Labels.job }}`
{{ end -}}
{{ end }}
{{ range .Alerts -}}
{{ if .Annotations.summary }}*{{ .Annotations.summary }}*{{ printf "\n" }}{{ end -}}
{{ if or .Annotations.message .Annotations.description }}{{ .Annotations.message }}{{ .Annotations.description }}{{ printf "\n" }}{{ end -}}
*Date*: _{{ .StartsAt.Format "Jan 02, 2006 15:04:05 UTC" }}_
*Details*:
{{ range .Labels.SortedPairs }} • *{{ .Name }}:* `{{ .Value }}`
{{ end }}
{{ end }}
{{ end }}
```

The pieces worth stealing: **severity-driven color+icon** (the whole point is
pre-attentive triage), the **pre-filled silence link** (acknowledging shouldn't
require leaving Slack), and a **summary/description split** so the title stays
scannable while the detail is still one glance away.

The receiver then references these templates and adds action buttons pointing at
per-alert annotations (`runbook_url`, `dashboard`) and Prometheus'
`GeneratorURL`:

```yaml
slackConfigs:
  - channel: "#your-alerts-channel"
    sendResolved: true
    color:     '{{ template "__slack_color" . }}'
    title:     '{{ template "__slack_title" . }}'
    titleLink: '{{ template "__alert_silence_link" . }}'
    text:      '{{ template "__slack_text" . }}'
    actions:
      - { type: button, text: 'Runbook :green_book:',  url: '{{ (index .Alerts 0).Annotations.runbook_url }}' }
      - { type: button, text: 'Query :mag:',           url: '{{ (index .Alerts 0).GeneratorURL }}' }
      - { type: button, text: 'Dashboard :bar_chart:', url: '{{ (index .Alerts 0).Annotations.dashboard }}' }
      - { type: button, text: 'Silence :no_bell:',     url: '{{ template "__alert_silence_link" . }}' }
```

For that to work, your alert rules need to *set* those annotations — e.g.
`annotations: { summary: "...", description: "...", runbook_url: "...",
dashboard: "..." }`. The template only renders what the rules provide.

## Getting it onto Kubernetes

The template above is just text; the question is how to mount it next to
Alertmanager and point the config at it. It depends which chart you run. All
three of these are prometheus-community charts:

- [`kube-prometheus-stack`](https://artifacthub.io/packages/helm/prometheus-community/kube-prometheus-stack)
  — the umbrella (Prometheus Operator + Prometheus + Alertmanager + Grafana).
- [`prometheus`](https://artifacthub.io/packages/helm/prometheus-community/prometheus)
  — Prometheus + a plain (non-operator) Alertmanager subchart.
- [`alertmanager`](https://artifacthub.io/packages/helm/prometheus-community/alertmanager)
  — Alertmanager on its own.

### kube-prometheus-stack (operator) — the two ways

**a) Inline config + `templateFiles`.** The chart writes each entry of
`alertmanager.templateFiles` into the Alertmanager config Secret and Alertmanager
loads them via the `templates:` glob. Put the config in
`alertmanager.config`:

```yaml
alertmanager:
  templateFiles:
    slack.tmpl: |
      {{/* paste the whole template from above here */}}
  config:
    global:
      slack_api_url_file: /etc/alertmanager/secrets/slack/url   # or use httpConfig bearer
    templates:
      - '/etc/alertmanager/config/*.tmpl'    # where templateFiles land
    route:
      receiver: slack
      group_by: ['alertname', 'environment', 'severity']
    receivers:
      - name: slack
        slack_configs:
          - channel: '#your-alerts-channel'
            send_resolved: true
            color:     '{{ template "__slack_color" . }}'
            title:     '{{ template "__slack_title" . }}'
            title_link:'{{ template "__alert_silence_link" . }}'
            text:      '{{ template "__slack_text" . }}'
```

**b) `AlertmanagerConfig` CRD.** If you drive routing with the operator's CRD
instead of the inline blob, the receiver looks the same but in CRD spelling
(`slackConfigs`, `titleLink`, `sendResolved`), and the **templates still come
from `alertmanager.templateFiles`** in the chart values — the CRD references
template names, it doesn't carry the template bodies. A bot-token setup injects
the token at the HTTP layer:

```yaml
# values.yaml
alertmanager:
  templateFiles:
    slack.tmpl: | {{/* the template */}}
  alertmanagerSpec:
    alertmanagerConfigSelector:
      matchLabels: { alertmanagerConfig: main }
```
```yaml
# a separate AlertmanagerConfig object
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata: { name: main, labels: { alertmanagerConfig: main } }
spec:
  receivers:
    - name: slack
      slackConfigs:
        - channel: '#your-alerts-channel'
          sendResolved: true
          httpConfig:
            authorization: { type: Bearer, credentials: { name: slack-bot, key: token } }
          color:     '{{ template "__slack_color" . }}'
          title:     '{{ template "__slack_title" . }}'
          titleLink: '{{ template "__alert_silence_link" . }}'
          text:      '{{ template "__slack_text" . }}'
  route: { receiver: slack, groupBy: ['alertname','environment','severity'] }
```

### prometheus chart (non-operator)

Everything is `alertmanagerFiles` — the chart renders `alertmanager.yml` plus any
template files you add:

```yaml
alertmanagerFiles:
  alertmanager.yml:
    templates: ['/etc/config/*.tmpl']
    route: { receiver: slack }
    receivers:
      - name: slack
        slack_configs: [{ channel: '#your-alerts-channel', text: '{{ template "__slack_text" . }}', title: '{{ template "__slack_title" . }}' }]
  # extra template file, mounted alongside the config:
  slack.tmpl: | {{/* the template */}}
```

### alertmanager chart (standalone)

`config:` holds `alertmanager.yml`; `templates:` is a map of filename → body that
the chart mounts and the config globs:

```yaml
config:
  templates: ['/etc/alertmanager/*.tmpl']
  route: { receiver: slack }
  receivers:
    - name: slack
      slack_configs: [{ channel: '#your-alerts-channel', title: '{{ template "__slack_title" . }}', text: '{{ template "__slack_text" . }}' }]
templates:
  slack.tmpl: | {{/* the template */}}
```

## The one gotcha

Whichever chart you use, the constant is: the template file has to be **mounted
into the Alertmanager container** *and* referenced by the `templates:` glob in
the config. The charts differ only in which values key writes the file. If your
Slack messages render as literal `{{ template "__slack_title" . }}` text, the
config found the receiver but not the template file — check the glob path
matches where the chart mounts `templateFiles`/`templates`.

Keep the alert *rules* holding the human content (`summary`, `description`,
`runbook_url`, `dashboard`) and this template holding the *presentation*. That
separation is what lets one template make every alert in the fleet look right.
