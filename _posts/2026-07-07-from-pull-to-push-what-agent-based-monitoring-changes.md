---
layout: post
title: "From Pull to Push: What Agent-Based Monitoring Actually Changes"
date: 2026-07-07
categories: [observability, prometheus]
tags: [prometheus, grafana-alloy, remote-write, alerting, kube-prometheus-stack, mimir]
---

I've spent the last few months migrating a standalone monitoring stack — Prometheus, Alertmanager, Loki and Grafana running on cloud VMs, provisioned with Terraform and configured with Ansible — into an in-cluster, GitOps-managed kube-prometheus-stack. Along the way, one architectural question kept resurfacing in design discussions: should we keep the classic Prometheus **pull** model, or move to an **agent push** model, where lightweight collectors (Grafana Alloy, the OpenTelemetry Collector, Prometheus in agent mode) scrape locally and `remote_write` everything to a central store?

Grafana Cloud, Mimir, Thanos Receive, Amazon Managed Prometheus — they all ingest via `remote_write`. The transport difference is real, but it comes with a less-obvious consequence: **the pull model isn't just a transport choice, it's a health-checking model.** When you switch to push, some of your monitoring assumptions silently invert, and your alerting rules need to change with them.

## What the pull model gives you for free

In classic Prometheus, the server initiates every scrape. That single design decision buys you three things:

**1. `up` is a real health check.** Every scrape attempt produces an `up{job, instance}` sample: `1` if the target answered, `0` if it didn't. This is *active* verification, performed by the same component that evaluates your alerts. `up == 0` means "I tried to reach this thing just now and it did not respond." There is no ambiguity about whose fault it is.

**2. Absence is loud.** A dead target doesn't disappear from your metrics — it shows up as `up == 0`. The failure mode is a *signal*, not a *gap*.

**3. Service discovery and scrape config live in one place.** The Prometheus server knows the full inventory of what it's supposed to be monitoring. If a target exists in SD but stops answering, that's detectable by construction.

The canonical alert in this world is trivially simple:

{% raw %}
```yaml
- alert: TargetDown
  expr: up == 0
  for: 5m
  annotations:
    summary: "{{ $labels.job }} on {{ $labels.instance }} is unreachable"
```
{% endraw %}

## What the push model takes away

In an agent-based architecture, the topology inverts. An agent sits next to (or inside) each node or cluster, scrapes its local targets, and ships samples over `remote_write` to a central store — Mimir, Thanos Receive, a managed service. Rules are evaluated centrally, against whatever data *arrived*.

**The central store never tries to reach your targets.** It only knows what it receives. So the question your alerting can answer changes from *"did the target respond when I probed it?"* to *"has data about this target shown up recently?"* Those sound similar. They are not.

Consider the failure modes:

| Failure | Pull model | Push model |
|---|---|---|
| Target process dies | `up == 0` from the server | Agent reports `up == 0` — **detected**, if the agent still runs |
| Agent dies | n/a (no agent) | Silence. No `up` metric at all. |
| Network partition agent → store | Scrape fails, `up == 0` | Silence (agent buffers in WAL, then drops) |
| Whole node/VM disappears | `up == 0` | Silence |
| Misconfigured relabeling drops a job | Visible in targets page | Silence |

Four out of five failure modes in the push column produce the same symptom: **nothing**. And "nothing" is the one thing a naïve `up == 0` alert cannot fire on, because rule evaluation needs samples to evaluate. Your loudest failure signal just became your quietest.

## Rewriting the alerting layer

The migration is therefore not "port the alert rules and change the datasource." Three categories of rules need rethinking.

### 1. Presence alerts replace liveness alerts

`up == 0` still works for the case where the agent is healthy but its local target is down — keep those rules. But every one of them needs a companion **absence** rule that fires when the time series stops arriving entirely:

{% raw %}
```yaml
- alert: MetricsAbsent
  expr: absent_over_time(up{job="octez-node"}[10m])
  for: 5m
  labels:
    severity: p1
  annotations:
    summary: "No samples received for job octez-node in 10m — target, agent, or pipeline is down"
```
{% endraw %}

Two things bite here in practice. First, `absent_over_time()` can only take a fixed selector — it can't enumerate instances it has never seen, so you lose the per-instance granularity `up` gave you. You either maintain one absence rule per critical job (my choice), or you generate them from your inventory (we generate ours with Jsonnet, which keeps the rule set in lockstep with the deployment config). Second, absence alerts fire during *intentional* removals too — decommission a node and forget to remove the rule, and you page yourself at 3 a.m. for a machine that no longer exists. The inventory and the rules must come from the same source of truth. GitOps helps enormously here: the same ArgoCD application that removes the scrape config removes the absence rule.

### 2. The pipeline itself becomes a monitored system

With pull, the path from target to alert evaluation was one hop. With push, it's target → agent scrape → WAL → `remote_write` queue → network → ingester → store → ruler. Each hop can back up, and a backed-up pipeline delays *every* alert downstream — your `for: 5m` is now `for: 5m + ingestion lag`.

The agent exposes everything you need; you just have to actually alert on it:

- `prometheus_remote_storage_highest_timestamp_in_seconds` minus `prometheus_remote_storage_queue_highest_sent_timestamp_seconds` — your end-to-end write lag. Alert when it exceeds, say, 2 minutes.
- `prometheus_remote_storage_samples_failed_total` and `samples_dropped_total` — non-zero rates here mean you are losing data, not just delaying it.
- `prometheus_remote_storage_shards` pinned at `max_shards` — the queue can't keep up.
- WAL disk usage on the agent — a long partition fills the buffer, and recovery replays a flood of out-of-order-adjacent samples at the store.

None of these alerts existed in the old stack because none of these components existed. Budget for it: in our migration, roughly a third of the "new" alert rules monitor the monitoring pipeline itself.

### 3. The dead man's switch is no longer optional

In the pull world, a fully dead Prometheus was already a known blind spot, and the standard mitigation was a `Watchdog`/`DeadMansSwitch` alert — a rule that always fires, routed to an external service that pages you when the *heartbeat stops*. In the push world this pattern graduates from "nice to have" to **structural requirement**, because entire-pipeline death is now indistinguishable from "everything is fine" at every layer you control.

The heartbeat must terminate *outside* the failure domain of the stack it monitors: healthchecks.io, Dead Man's Snitch, PagerDuty's dead-man integration, or a second minimal Prometheus in another provider doing nothing but probing the first. If your monitoring runs in-cluster — as ours now does — this external witness is also your answer to the awkward question "what tells us the cluster hosting the monitoring is down?" Whatever you pick, the property that matters is independence: different network path, different provider, different credentials.

## What you get in exchange

What you get in exchange:

- **Topology fit.** No inbound connectivity to edge networks, NATed VMs, or short-lived workloads. The agent only needs one outbound HTTPS path. This is the original motivation and it's real — our old stack needed carefully curated firewall rules and a VPN mesh for the central server to reach every target.
- **One query surface.** All regions and clusters land in one store with consistent `external_labels`. Cross-fleet queries and global dashboards stop being a federation science project.
- **HA without gaps.** Run two agents per site with identical configs and distinct replica labels; the store (Mimir, or Grafana Cloud's Adaptive dedup) deduplicates. Compare that to HA pull-Prometheus pairs, where deduplication is the *query layer's* problem forever.
- **Centralized rule management.** Rules live in the ruler, deployed via GitOps, versioned with everything else — no more Ansible-templated `rules.d` drift across regional servers.

## Migration checklist

1. **Inventory every `up`-based alert** and write its `absent_over_time()` companion. Generate them from the same source of truth as the scrape configs.
2. **Add pipeline alerts** on `remote_write` lag, failed/dropped samples, shard saturation and agent WAL disk *before* cutover, not after the first silent outage.
3. **Stand up the external dead man's switch first.** It's the only component that can tell you the cutover itself went wrong.
4. **Re-tune `for:` durations.** Add expected ingestion lag headroom to time-critical rules, or alerts that fired reliably at `for: 2m` under pull will flap under push.
5. **Set `external_labels` deliberately** (cluster, region, replica) before the first sample ships — relabeling history after the fact is miserable.
6. **Run both models in parallel** for at least one full incident cycle. The gaps you find will be in the absence-detection layer, and you want to find them while the old `up == 0` safety net still exists.

The one-line summary: **pull-based monitoring fails loud, push-based monitoring fails silent** — and the entire migration effort, beyond plumbing, is about re-introducing loudness on purpose. If you only carry your alert rules across unchanged, you haven't migrated your monitoring; you've migrated your dashboards and quietly deleted your smoke detector.
