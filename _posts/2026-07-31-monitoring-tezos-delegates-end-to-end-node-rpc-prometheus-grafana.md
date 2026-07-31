---
layout: post
title: "Monitoring Tezos delegates end-to-end: node RPC → Prometheus → Grafana"
date: 2026-07-31 19:15:00 +0000
categories: [observability, tezos]
tags: [tezos, octez, baking, prometheus, grafana, grafazos, json-exporter, kubernetes, helm]
---
{% raw %}

Standard node dashboards cover chain-level metrics: head level, peer count, resource usage. Delegate-level health — missed attestation slots, DAL participation sufficiency, remaining grace period before deactivation — lives in a different data set: the delegate RPCs of the Octez node.

This post describes an end-to-end pipeline that turns those RPCs into Prometheus metrics and visualizes them with the `octez-delegates` dashboard that now ships in [Grafazos](https://gitlab.com/tezos/tezos/-/tree/master/grafazos), the official Octez dashboard collection. The data comes entirely from the node itself — no indexer or third-party API is involved.

```
Octez node RPC ──> json-exporter ──> Prometheus ──> Grafana (Grafazos octez-delegates)
```

## How it works

The [prometheus-community/json-exporter](https://github.com/prometheus-community/json_exporter) probes the delegate RPCs of the node and converts the JSON fields into Prometheus gauges. Each metric group is one exporter module probed against a **field-specific sub-RPC**:

```
/chains/main/blocks/head/context/delegates/<address>/participation
/chains/main/blocks/head/context/delegates/<address>/dal_participation
/chains/main/blocks/head/context/delegates/<address>/baking_power
...
```

Metric names follow a simple contract: `octez_delegate_` + the flattened (dot → underscore) JSON path of the RPC field. `.participation.missed_slots` becomes `octez_delegate_participation_missed_slots`, so the whole metric set is discoverable from the [RPC documentation](https://octez.tezos.com/docs/api/rpc.html) alone. Booleans are exported as 0/1 gauges; balances are in mutez. This naming contract is exactly what the Grafazos delegates dashboard consumes.

### ⚠️ One important rule: never probe the full delegate object

Scraping `/context/delegates/<address>` (the full object, no field) is the obvious shortcut and must be avoided. On mainnet, ~95% of that response's cost and payload is the `delegators` list, which no metric uses. When the scrape timeout is shorter than the time the node needs to enumerate delegators, the exporter cancels and re-issues the query every interval — which can pin the node's single-threaded RPC handling at 100% and starve RPC, metrics and even P2P for every other client. This failure mode has been observed in production on mainnet nodes.

Field-specific sub-RPCs are cheap, bounded, and return exactly what the metrics need.

## Step 1 — Export the metrics

json-exporter runs alongside the node (systemd unit, container, or sidecar — any deployment method works) and needs two pieces of configuration: its own `config.yml` defining the metric modules, and a Prometheus scrape job per delegate per module.

A trimmed `config.yml` (more modules follow the same pattern — the naming contract above gives the metric name for any RPC field):

```yaml
modules:
  delegate_participation:
    headers:
      Accept: application/json
    metrics:
      - name: octez_delegate_participation_expected_cycle_activity
        help: Expected attestation slots in current cycle
        path: '{ .expected_cycle_activity }'
      - name: octez_delegate_participation_missed_slots
        help: Missed attestation slots in current cycle
        path: '{ .missed_slots }'
      - name: octez_delegate_participation_remaining_allowed_missed_slots
        help: Slots that can still be missed before losing attesting rewards
        path: '{ .remaining_allowed_missed_slots }'
  delegate_baking_power:
    headers:
      Accept: application/json
    metrics:
      - name: octez_delegate_baking_power
        help: Delegate baking power in mutez
        path: '{ @ }'   # scalar sub-RPC: extract the document root
```

And the matching Prometheus scrape config (one job per delegate per module; json-exporter listens on 7979 by default):

```yaml
scrape_configs:
  - job_name: delegate_participation_mybaker
    metrics_path: /probe
    scrape_interval: 30s
    scrape_timeout: 10s
    params:
      module: [delegate_participation]
      target:
        - "http://127.0.0.1:8732/chains/main/blocks/head/context/delegates/tz1YourDelegateAddressHere/participation"
    static_configs:
      - targets: ["localhost:7979"]
        labels:
          delegate: "tz1YourDelegateAddressHere"
          delegate_name: "my-baker"
```

## On Kubernetes: packaging it into a Helm chart

On Kubernetes the same pipeline maps onto four objects: a sidecar container, a ConfigMap, a Service, and a ServiceMonitor (Prometheus Operator). The rest of this section shows how to wire them into an existing octez-node chart so that adding a delegate becomes a one-line values change.

The target values interface:

```yaml
delegateRpcMonitoring:
  enabled: true
  interval: 30s
  scrapeTimeout: 10s
  delegates:
    - name: "my-baker"                # optional friendly name
      address: "tz1YourDelegateAddressHere"
    - name: "my-second-baker"
      address: "tz1AnotherDelegateAddress"
  metricGroups:                       # one entry per sub-RPC
    - name: delegate_participation
      rpcPath: participation
      metrics:
        - name: octez_delegate_participation_missed_slots
          help: Missed attestation slots in current cycle
          path: '{ .missed_slots }'
        # ... one entry per field
    - name: delegate_baking_power
      rpcPath: baking_power
      metrics:
        - name: octez_delegate_baking_power
          help: Delegate baking power in mutez
          path: '{ @ }'
```

Two lists, combined by the templates: **delegates × metricGroups** becomes the set of scrape endpoints, while **metricGroups** alone becomes the exporter configuration. Adding a delegate touches only the first list; adding a metric touches only the second.

### 1. The sidecar

json-exporter runs as a sidecar in the node pod, not as a separate Deployment. The reason is access: the probes target the node's RPC on `127.0.0.1`, so the RPC port never needs to be exposed outside the pod — no ACL widening, no extra NetworkPolicy.

```yaml
# extra container in the node StatefulSet pod spec
- name: delegate-rpc-exporter
  image: prometheuscommunity/json-exporter:v0.7.0
  args:
    - --config.file=/etc/json-exporter/config.yml
  ports:
    - name: rpc-mtr-delegt      # ≤15 chars, referenced by the ServiceMonitor
      containerPort: 9934
  volumeMounts:
    - name: delegate-monitor-config
      mountPath: /etc/json-exporter
# ...and the matching volume
volumes:
  - name: delegate-monitor-config
    configMap:
      name: octez-node-delegate-rpc-monitor
```

json-exporter listens on 7979 by default; `--web.listen-address=:9934` (or leaving 7979 everywhere) — the only requirement is that the container port, Service port and ServiceMonitor port name agree.

### 2. The ConfigMap — exporter modules from `metricGroups`

The exporter config is generated straight from the values, one module per metric group:

```yaml
{{- if and .Values.delegateRpcMonitoring.enabled (gt (len .Values.delegateRpcMonitoring.delegates) 0) }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: octez-node-delegate-rpc-monitor
data:
  config.yml: |
    modules:
    {{- range $group := .Values.delegateRpcMonitoring.metricGroups }}
      {{ $group.name }}:
        headers:
          Accept: application/json
        metrics:
        {{- range $metric := $group.metrics }}
        - name: {{ $metric.name }}
          help: '{{ $metric.help }}'
          path: '{{ $metric.path }}'
        {{- end }}
    {{- end }}
{{- end }}
```

Note the guard: the objects are only rendered when the feature is enabled **and** at least one delegate is configured — an enabled feature with an empty delegate list should produce nothing, not broken scrape targets.

### 3. The Service

A plain ClusterIP Service exposing the exporter port, with the same selector labels as the node StatefulSet:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: octez-node-delegate-rpc-monitor
spec:
  selector:
    app.kubernetes.io/name: octez-node   # match the pod labels
  ports:
    - name: rpc-mtr-delegt
      port: 9934
      targetPort: rpc-mtr-delegt
```

### 4. The ServiceMonitor — one endpoint per delegate per group

This is where the two lists combine. Each `(delegate, metricGroup)` pair becomes one endpoint: the probe `target` is the field-specific sub-RPC URL for that delegate, the `module` selects the metric group, and relabelings stamp the `delegate` / `delegate_name` labels onto every series so dashboards and alerts can filter per delegate:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: octez-node-delegate-rpc
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: octez-node
  endpoints:
  {{- range $delegate := .Values.delegateRpcMonitoring.delegates }}
  {{- range $group := $.Values.delegateRpcMonitoring.metricGroups }}
  # {{ $delegate.name | default $delegate.address }} / {{ $group.name }}:
  # field-specific sub-RPC — never the full delegate object
  - port: rpc-mtr-delegt
    path: /probe
    params:
      target: ["http://127.0.0.1:8732/chains/main/blocks/head/context/delegates/{{ $delegate.address }}/{{ $group.rpcPath }}"]
      module: ["{{ $group.name }}"]
    interval: {{ $.Values.delegateRpcMonitoring.interval | default "30s" }}
    scrapeTimeout: {{ $.Values.delegateRpcMonitoring.scrapeTimeout | default "10s" }}
    relabelings:
    - targetLabel: delegate
      replacement: '{{ $delegate.address }}'
    {{- if $delegate.name }}
    - targetLabel: delegate_name
      replacement: '{{ $delegate.name }}'
    {{- end }}
  {{- end }}
  {{- end }}
```

Implementation details worth getting right:

- **`target` points at `127.0.0.1:8732`** — the URL is resolved *by the sidecar*, inside the pod. Prometheus only ever talks to the exporter port.
- **The nested `range` scoping**: inside `range $delegate`, the root context is gone — the inner loop and every values lookup need the `$.` prefix.
- **`scrapeTimeout` < `interval`**, and both matter: with field-specific sub-RPCs, 10s is comfortable headroom; it is *not* a substitute for the full-object rule above (a timeout on an expensive query is precisely what creates the cancel-and-retry loop).
- **Port names** are limited to 15 characters (`rpc-mtr-delegt` is 14) — an easy validation error to hit.
- The number of endpoints grows as `delegates × metricGroups`. With the full field set (~6 groups) and a handful of delegates that is a few dozen cheap probes — fine — but it is the knob to keep in mind before adding 50 delegates to one node.

With this in place, `helm template` renders the ConfigMap, Service and ServiceMonitor from the values file, Prometheus Operator picks up the ServiceMonitor automatically, and the metrics appear with the exact names and labels the Grafazos dashboard expects.

## Exported metrics

The full useful field set, per delegate:

- **Status** — `octez_delegate_deactivated`, `octez_delegate_is_forbidden`, `octez_delegate_grace_period`
- **Participation (current cycle)** — expected/minimal cycle activity, missed slots, missed levels, remaining allowed missed slots, expected attesting rewards
- **DAL participation (current cycle)** — attestable vs attested DAL slots, expected DAL rewards, `sufficient_dal_participation`, `denounced`
- **Staking & balances** — baking power, total/own/external staked and delegated, minimum delegated this cycle, pending slashed amount, staking parameters
- **Voting** — voting power, current voting power, remaining proposals

## Step 2 — The dashboard

Grafazos (the jsonnet-based Octez dashboards, in the `tezos/tezos` repo under [`grafazos/`](https://gitlab.com/tezos/tezos/-/tree/master/grafazos)) includes an `octez-delegates` dashboard built on these exact metric names: an overview row (activity & status), then sections for staking & balances, current-cycle participation, DAL participation, voting & governance, and staking parameters & risk — filterable per delegate through a dashboard variable.

Building it requires jsonnet ([go-jsonnet](https://github.com/google/go-jsonnet)) and, if the `vendor/` directory is not populated, [jsonnet-bundler](https://github.com/jsonnet-bundler/jsonnet-bundler):

```sh
git clone https://gitlab.com/tezos/tezos.git
cd tezos/grafazos
make install-jb    # only if vendor/ is missing
make delegates
```

The result lands in `output/octez-delegates.json`, importable in Grafana via *Dashboards → New → Import* against a Prometheus datasource; the delegate is selected in the variable dropdown. With several Prometheus datasources, building with `make delegates DATASOURCE_SELECTION=true` adds a datasource selector.

## Alerting

Once the metrics exist, useful alerts are one-liners. Examples:

```yaml
# Getting close to losing attestation rewards this cycle
- alert: DelegateMissedSlotsBudgetLow
  expr: octez_delegate_participation_remaining_allowed_missed_slots
        < 0.2 * octez_delegate_participation_expected_cycle_activity
  for: 15m

# DAL participation no longer sufficient for the cycle
- alert: DelegateDalParticipationInsufficient
  expr: octez_delegate_dal_participation_sufficient_dal_participation == 0
  for: 30m

# Should never happen — page immediately
- alert: DelegateDeactivatedOrForbidden
  expr: octez_delegate_deactivated == 1 or octez_delegate_is_forbidden == 1
```

The whole pipeline runs against the operator's own node, scales linearly with the number of monitored delegates (one set of sub-RPC probes each), and keeps the node healthy as long as probing sticks to field-specific sub-RPCs.

Feedback is welcome — the dashboard takes contributions in the `tezos/tezos` repo under `grafazos/`.

{% endraw %}
