---
layout: post
title: "Provisioning and observing a GCP fleet with Terraform, Ansible, and Grafana"
date: 2026-07-14 09:00:00 +0000
categories: [infrastructure, observability]
tags: [terraform, ansible, gcp, prometheus, grafana, loki, grafonnet, gitlab-ci, iac]
---

I keep rebuilding the same shape: a handful of long-running VMs on GCP, each
running some stateful service, and a monitoring box that watches all of them.
Every time, the interesting part is not any single tool — it's the **seams**
between them. This post walks through a small, self-contained reference that
captures those seams, split across four independent repos:

- **`tf-gcp-instance`** — a reusable Terraform module: one GCP VM (static
  IPv4+IPv6, an attached SSD, a per-VM service account, DNS records, arbitrary
  labels).
- **`terraform-ci`** — reusable GitLab-CI templates: plan/apply, a
  destructive-plan guard, a Pages summary of the fleet, and an opt-in Grafana
  annotation.
- **`fleet`** — the consumer: it provisions a labelled set of VMs and configures
  them with Ansible.
- **`obs-stack`** — the observability server: Grafana, Prometheus, Alertmanager,
  Loki, Tempo, and Alloy, provisioned onto its own VM.

They depend on each other only in obvious ways — both `fleet` and `obs-stack`
consume the module and the CI templates; `obs-stack` finds `fleet` at runtime,
not build time. Nothing shares state.

## The seam that matters: labels drive everything

The trick that makes the whole thing composable is that **one set of Terraform
labels drives both configuration management and monitoring**. You stamp labels
on a VM once, and three independent systems react to them.

In `fleet`, instances come from a `for_each` over a small map, following an
`${environment}-${service_tier}` convention:

```hcl
locals {
  fleet = {
    "dev-edge"       = { environment = "dev",     service_tier = "edge",   machine_type = "e2-small",  disk_size = 20 }
    "staging-origin" = { environment = "staging", service_tier = "origin", machine_type = "e2-small",  disk_size = 30 }
    "prod-edge"      = { environment = "prod",    service_tier = "edge",   machine_type = "e2-medium", disk_size = 20 }
    "prod-origin"    = { environment = "prod",    service_tier = "origin", machine_type = "e2-medium", disk_size = 50 }
  }
}

module "fleet" {
  source   = "git::https://gitlab.com/oyatrino/oyatrino-o11y-labs/tf-gcp-instance.git?ref=main"
  for_each = local.fleet

  instance_name      = each.key
  machine_type       = each.value.machine_type
  attached_disk_size = each.value.disk_size
  # ... network wiring ...

  additional_labels = {
    use_case           = "web-fleet"
    environment        = each.value.environment
    service_tier       = each.value.service_tier
    dashboard_instance = "${each.value.environment}_${each.value.service_tier}"
    role_app           = "true"
  }
}
```

(`edge` is a small, latency-sensitive front tier; `origin` is a larger,
stateful backing tier — the same app, deployed in two resource profiles. There's
also one hand-tuned `bespoke` instance alongside the templated set, to show the
override path for a box that doesn't fit the convention.)

Now the same labels do three jobs:

**1. Ansible groups hosts by them.** The GCP dynamic inventory keys groups off
labels, so a playbook can target `fleet` without a static host list:

```yaml
plugin: google.cloud.gcp_compute
keyed_groups:
  - { key: labels.environment,  prefix: env }
  - { key: labels.service_tier, prefix: tier }
groups:
  fleet: "'web-fleet' in (labels.use_case | default(''))"
compose:
  environment:        labels['environment']
  dashboard_instance: labels['dashboard_instance']
```

**2. Prometheus discovers scrape targets by them.** On `obs-stack`, Prometheus
uses GCE service discovery — no target list, no reconfiguration when the fleet
grows:

```yaml
scrape_configs:
  - job_name: fleet-node
    gce_sd_configs:
      - { project: your-project, zone: europe-west1-c, port: 9100,
          filter: "labels.use_case=web-fleet" }
    relabel_configs:
      - { source_labels: [__meta_gce_label_environment],        target_label: environment }
      - { source_labels: [__meta_gce_label_dashboard_instance], target_label: dashboard_instance }
```

**3. Grafana dashboards filter on them.** Every dashboard has a
`dashboard_instance` template variable, so one dashboard serves the whole fleet.

Add a VM to the `for_each` map and it is provisioned, configured, scraped, and
graphed — with no change to Ansible, Prometheus, or Grafana.

```
                    ┌──────────────────────────── labels ───────────────────────────┐
                    │  use_case, environment, service_tier, dashboard_instance       │
  terraform apply ──┤                                                                │
                    ▼                    ▼                          ▼                 │
              GCP VM (labelled)   Ansible dynamic inventory   Prometheus GCE SD       │
                    │              (groups by label)          (scrapes by label)      │
                    └──────────────► installs app + agents ──────────► /metrics ──────┘
```

## Why observability lives in its own repo

The original of this design had the monitoring server tangled into the consumer
repo. Pulling it into `obs-stack` bought three things: the fleet repo no longer
carries a Grafana install it doesn't own; the observability stack can be
redeployed on its own cadence; and `obs-stack` becomes a thing you can read and
reason about by itself.

I also modernised how it's deployed. Instead of apt packages and per-component
systemd units, `obs-stack` ships a single Docker-Compose stack — Grafana,
Prometheus, Alertmanager, Loki, Tempo, Alloy — with pinned versions, provisioned
under `/opt/observability` by one Ansible role. The entire stack config validates
locally with no cloud and no VM: `promtool check config/rules`, `amtool
check-config`, `docker compose config`, and JSON checks all run in CI.

### Push or pull? Both — split by signal

A question worth answering explicitly, because the answer is deliberately mixed:

| Signal | Model | How |
|---|---|---|
| Metrics | **pull** | Prometheus GCE service-discovery scrapes node_exporter and the app |
| Logs | **push** | a Grafana Alloy agent on each host ships the journal to Loki |
| Traces | **push** | OTLP into Tempo |

Pull for metrics means the server discovers new VMs automatically — the right
default for a fleet that changes shape. Push for logs and traces is the natural
fit agent-side: there's no "scrape my logs" model, and pushing survives
short-lived nodes. (In this reference the trace path is wired but not yet
exercised — the sample workload emits metrics and logs; adding OTLP spans is left
as the obvious next step.)

### Dashboards as code

Dashboards are grafonnet, not hand-edited JSON. A small helper library exposes
`vars`, `target`, `panels`, `place`, and a `dashboard` skeleton, so each
dashboard stays declarative:

```jsonnet
local lib = import '../lib/lib.libsonnet';
lib.dashboard(
  title='Fleet — Node Overview', uid='fleet-node-overview', tags=['fleet','node'],
  variables=[lib.vars.datasource(), lib.vars.labelValues('dashboard_instance', 'node_uname_info')],
  panels=[ /* CPU %, /opt free % */ ],
)
```

A `Makefile` (`make deps build fmt lint`) compiles the sources to JSON that
Ansible ships to Grafana. The vendored grafonnet library is git-ignored and
rehydrated from a lockfile; the compiled JSON is committed so it's reviewable in
diffs.

## The CI is not just plan/apply

Two things in `terraform-ci` earn their keep.

**Destructive-plan visibility.** GitLab's merge-request Terraform widget shows
"N to add, N to change, N to delete" — but a *replace* (destroy + recreate) shows
up as "1 add, 1 delete", quietly hiding that an existing VM or disk is about to be
destroyed, and never telling you *which attribute* forced it. The guard job parses
the plan JSON, classifies every delete/replace, prints a table naming the resource
and the forcing attribute, and turns the job **orange** — loud, but it doesn't
block the merge. Set an API token and it also posts a single, idempotent
merge-request comment. On the default branch, destructive applies are not
automatic: a manual job with a native confirmation dialog runs them.

**A fleet summary on Pages.** A self-contained HTML page (no CDN, light/dark,
responsive) rendered from `terraform show -json`, with per-instance deep links to
the GCP console, the machine-type reference, the instance's `/healthz`, and — if
you set `GRAFANA_URL` — its Grafana dashboards.

## Closing the loop

The last seam ties CI back to the graphs. On apply, an opt-in job posts a region
annotation to Grafana tagged `["terraform","apply","<environment>"]`. Any
dashboard with an annotation query on those tags then shows a marker at exactly
the moment infrastructure changed — so when a latency graph steps, you can see
"an apply landed right here." The job no-ops unless `GRAFANA_URL` and a scoped
annotation-writer token are set, so the CI template stays generic; `obs-stack`
owns creating that service account.

## Reuse

Each repo stands alone under
[`gitlab.com/oyatrino/oyatrino-o11y-labs`](https://gitlab.com/oyatrino/oyatrino-o11y-labs):
pin the module and CI templates by tag, set your project and a couple of CI
variables, and `terraform apply` then `ansible-playbook`. The sample workload is
a ~90-line stdlib Python service exposing `/healthz` and a Prometheus `/metrics`
histogram — swap it for whatever you actually run.

The point isn't the specific tools. It's that a single label, stamped once at
`terraform apply`, can drive provisioning, configuration, discovery, and
dashboards — and that the boundaries between those systems are worth designing on
purpose.
