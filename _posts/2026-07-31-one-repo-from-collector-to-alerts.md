---
layout: post
title: "One repo, whole vertical: from a consensus collector to its dashboards and alerts"
date: 2026-07-31 13:00:00 +0000
categories: [infrastructure, observability]
tags: [tezos, observability, helm, prometheus-operator, grafana, loki, alertmanager, grafonnet, gitlab-ci]
---

Most observability lives in pieces: the exporter is one repo, the dashboards are pasted into
Grafana by hand or kept in a second repo, and the alerts are a YAML file someone maintains in a
third. Each piece drifts from the others on its own schedule.

`octez-consensus-exporter` is built the opposite way: **one repository holds the whole vertical** —
the collector, the stable contract it exposes, the dashboards, the Helm chart, the alert rules,
the CI that guards all of it, and a local demo that runs the same dashboards offline. A change to a
metric, the panel that shows it, and the alert that fires on it lands in **one commit**, gated
together.

<!--more-->

**Repository (public):**
[gitlab.com/tezos-infra/octez-consensus-exporter](https://gitlab.com/tezos-infra/octez-consensus-exporter)
<!-- Internal fork (private): https://gitlab.com/oyatrino/oyatrino-tezos/octez-consensus-exporter -->

---

## The layers, in one tree

```
exporter/    the collector: RPC client → /metrics endpoint + Loki push
contract/    frozen v1 interface: golden /metrics + Loki event fixtures + CONTRACT.md
dashboards/  grafonnet sources, compiled to JSON in CI
chart/       Helm: workload + scrape wiring + dashboards + alert rules
demo/        fixture-driven local Prometheus + Loki + Grafana
```

- **`exporter/`** produces two outputs: an OpenMetrics `/metrics` endpoint (aggregate signals) and
  a Loki push stream (one structured JSON line per consensus event).
- **`contract/`** freezes those two outputs as golden fixtures plus a written `CONTRACT.md`. That
  contract is the seam everything downstream depends on — dashboards and the demo are written
  against it, so a change to a metric name or an event field is a visible, reviewable diff, not a
  surprise in production.
- **`dashboards/`** are [grafonnet](https://github.com/grafana/grafonnet) sources compiled to
  Grafana JSON. Datasources are bound by *name* (`${thanos}` / `${loki}`), so the same JSON works
  on any fleet Grafana and in the demo.
- **`chart/`** is where it all ships together (next section).
- **`demo/`** runs the exact dashboards against the frozen fixtures in local Prometheus + Loki +
  Grafana — no cluster, no node required.

![Report dashboard](https://gitlab.com/tezos-infra/octez-consensus-exporter/-/raw/main/docs/images/dashboards/report.png)
*The dashboards ship with the collector and run identically in the demo. Screenshots are from the
demo stack, so addresses are synthetic `tz…FAKEBAKER…` placeholders.*

---

## The chart carries code, dashboards *and* alerts

The Helm chart is four templates — and that list is the point:

```
chart/templates/
  exporter-deployment.yaml   the workload
  podmonitor.yaml            how Prometheus scrapes it
  dashboards-configmap.yaml  the compiled dashboards
  prometheusrule.yaml        the alert rules
```

Installing the chart deploys the collector **and** wires its scraping **and** imports its
dashboards **and** loads its alerts. There is no second step where a human remembers to import a
dashboard or copy an alert file — the observability *is* the deployment.

The alerts shipped in the chart:

| Alert | Severity | Fires when |
|---|---|---|
| `ConsensusExporterDown` | critical | the exporter stops being scraped (2m) |
| `ConsensusExporterLagging` | warning | head level stops advancing (5m) |
| `ConsensusAttestationRateLow` | warning | attestation rate below threshold (15m) |
| `ConsensusDelayToQuorumHigh` | warning | 66% quorum takes too long (15m) |
| `ConsensusMissedAttestations` | warning | watchlist delegates miss attestations (5m) |
| `ConsensusMissedBlocks` | warning | baking slots go unfilled (5m) |

---

## Compatible with the standard operator stack — no custom glue

None of this uses bespoke integration. It plugs into the mainstream Prometheus-Operator + Grafana
ecosystem through their own CRDs and conventions:

- **`PodMonitor`** (`monitoring.coreos.com/v1`) — discovered and scraped by any Prometheus
  Operator install.
- **Dashboards `ConfigMap`** carrying the Grafana dashboard-sidecar label — auto-imported by the
  Grafana chart's sidecar; no API calls, no manual upload.
- **`PrometheusRule`** (`monitoring.coreos.com/v1`) — evaluated by Prometheus and routed by
  Alertmanager like any other rule.

So it drops into a cluster already running these charts:

- **kube-prometheus-stack** (Prometheus Operator + Prometheus + Alertmanager + Grafana) —
  [artifacthub.io/…/kube-prometheus-stack](https://artifacthub.io/packages/helm/prometheus-community/kube-prometheus-stack)
- **prometheus-operator-crds** (the `PodMonitor` / `PrometheusRule` CRDs, if you run them
  standalone) —
  [artifacthub.io/…/prometheus-operator-crds](https://artifacthub.io/packages/helm/prometheus-community/prometheus-operator-crds)
- **grafana** —
  [artifacthub.io/…/grafana](https://artifacthub.io/packages/helm/grafana/grafana)
- **loki** —
  [artifacthub.io/…/loki](https://artifacthub.io/packages/helm/grafana/loki)

Point the exporter's Loki URL at your Loki, install the chart into a namespace the operator
watches, and consensus health appears in the same Grafana as the rest of your fleet, alerting
through the same Alertmanager.

---

## CI guards the whole vertical

Because the pieces live together, CI can hold them consistent:

- a **dashboards drift-gate** fails if the committed dashboard JSON (what the chart ships) differs
  from a fresh compile of the grafonnet sources — a dashboard edit can't silently fail to deploy;
- a **contract check** keeps the collector's output aligned with the frozen fixtures the
  dashboards are written against;
- **`helm lint`** validates the chart on every change;
- a **chart-publish** job versions and publishes the chart to the registry on release.

![Exporter pipeline dashboard](https://gitlab.com/tezos-infra/octez-consensus-exporter/-/raw/main/docs/images/dashboards/pipeline-data-flow.png)
*Even the pipeline's own throughput and storage footprint are a shipped dashboard.*

---

The payoff is that observability stops being a separate project with its own drift. The metric, the
panel and the alert are one artifact, versioned and released as a unit, and they land in a cluster
through the same operator CRDs everyone already runs.

Open source and self-contained; issues and contributions welcome:
[gitlab.com/tezos-infra/octez-consensus-exporter](https://gitlab.com/tezos-infra/octez-consensus-exporter).

### References

- kube-prometheus-stack — <https://artifacthub.io/packages/helm/prometheus-community/kube-prometheus-stack>
- Grafana Helm chart — <https://artifacthub.io/packages/helm/grafana/grafana>
- Loki Helm chart — <https://artifacthub.io/packages/helm/grafana/loki>
- grafonnet — <https://github.com/grafana/grafonnet>
- Octez documentation — <https://octez.tezos.com/docs/>
</content>
</invoke>
