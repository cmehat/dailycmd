---
layout: post
title: "Monitoring Tezos consensus from a single node: architecture, cost, and rollout"
date: 2026-07-31 12:00:00 +0000
categories: [infrastructure, observability]
tags: [tezos, consensus, distributed-systems, prometheus, thanos, loki, grafana, kubernetes, argocd, sre]
redirect_from:
  - /2026/07/monitoring-tezos-consensus-health-without-the-protocol-upgrade-treadmill.html
---

Consensus is a distributed-systems problem, and monitoring it well means respecting that.
No single node holds the global truth about what the network agreed on and *when*: each node
observes a partial, time-skewed view assembled from gossip. A block that reached quorum at the
same wall-clock instant everywhere still *arrives* at different nodes at different times. So the
useful question is never "what is consensus doing" in the abstract — it is "what does consensus
look like **from here**, and how does *here* compare to *there*."

This post is a technical walk-through of `octez-consensus-exporter`, a tool that observes Tezos
consensus health from the vantage of one [Octez](https://octez.tezos.com/docs/) node and feeds
the result into a Prometheus/Thanos + Loki + Grafana fleet. It covers four things I care about
in production: the **distributed-systems model**, the **architectural decisions**, the **real
resource cost**, and **how it lands safely**.

<!--more-->

**Repository (public):**
[gitlab.com/tezos-infra/octez-consensus-exporter](https://gitlab.com/tezos-infra/octez-consensus-exporter)
<!-- Internal fork (private): https://gitlab.com/oyatrino/oyatrino-tezos/octez-consensus-exporter -->

---

## 1. Monitoring consensus in a distributed system

Tezos uses [Tenderbake](https://octez.tezos.com/docs/active/consensus.html), a BFT-style
consensus where a block proceeds through phases:

```
Block proposed → Preattestations → Attestations → 66% quorum → Applied
```

Each phase is a threshold of *attesting power*, gathered from operations that propagate through
the mempool. Three consequences drive the whole design:

- **Timing is per-observer.** "Time to reach 66% quorum" is measured against when *this* node
  saw the operations. A different node, with different peers, sees a different delay. There is no
  single correct number — there is a distribution across vantage points.
- **The level is the correlation key.** A block level is a single logical instant every vantage
  agrees on. It is the natural join key to line up what different observers saw for the same
  block — and, deliberately, it is used as a **time axis, never as a label** (more below).
- **Local vs global must be separable.** When quorum is slow, is the network struggling, or is
  *this* node's peering degraded? You can only answer that by comparing vantage points.

The exporter is therefore **one process per `(network, node)`**, and every metric carries a
`source` label identifying the vantage. Aggregate dashboards read across sources; a dedicated
multi-source / outlier board contrasts them, so a delay that shows up on one vantage but not the
others reads as a *local* problem, while a delay common to all vantages reads as a *network*
event.

![Report dashboard](https://gitlab.com/tezos-infra/octez-consensus-exporter/-/raw/main/docs/images/dashboards/report.png)
*Aggregate consensus health for one network, read across the selected vantage points. Screenshots
are from the fixture-driven demo stack, so every address is a synthetic `tz…FAKEBAKER…` placeholder.*

---

## 2. How it works

The exporter is a read-only client of one node's HTTP RPC. It holds no database; all state lives
in the fleet. It taps two node **monitor streams** at full fidelity and polls a few helpers:

| Source | Mode | Purpose |
|---|---|---|
| `/monitor/heads/main` | stream | new-head clock that drives per-block derivation |
| `/chains/main/mempool/monitor_operations` | stream | live *arrival time* of each consensus operation → reception delay |
| block header | poll | slot time, round, level (basis for all delay math) |
| block operations | poll | operations included in the block → attestation rate, delay-to-quorum |
| attestation / baking rights | poll | who *should* attest/bake → watchlist activity + missed-attestation/-block reconciliation |

On every head it derives validation/application delay, attestation rate, delay-to-quorum, round,
and reconciles rights-vs-seen to detect misses. Concurrently, the mempool tap stamps each
operation's arrival and computes reception delay against the block's slot time, resolving the
delegate via rights-by-slot.

That produces two complementary outputs — the central architectural split:

- **Aggregate signals → Prometheus / Thanos.** Histograms and counters (attestation rate,
  reception delay by kind, delay to quorum, validation/application delay, round distribution, DAL
  slot coverage, missed attestations/blocks, held operations). Cheap, ideal for trends, SLOs and
  alerts. Because histogram buckets accumulate between scrapes, a **15 s** scrape interval loses
  no precision on the distributions.
- **Exact per-event records → Loki.** One structured JSON line per consensus event (`block`,
  `attestation`, `preattestation`, `missing_block`), carrying delegate, block hash, round and
  millisecond delay. This is what turns "the p90 delay jumped" into "these three delegates on this
  level were 2 s late."

---

## 3. Architectural decisions

The decisions that matter, and why:

1. **One exporter per `(network, node)`.** Vantage isolation, linear scaling, no central
   collector to bottleneck or lose. Each exporter's blast radius is one node's view.
2. **No storage of its own.** Metrics and events are pushed to the fleet's existing Thanos and
   Loki. Nothing to back up, migrate or scale separately.
3. **Two sinks, by question type.** Aggregates that must be cheap and always-on go to Thanos;
   exact per-event forensics that must be precise-but-occasional go to Loki. Neither tool is asked
   to do the other's job.
4. **Level is a time axis, never a label.** Block levels are unbounded and monotonic; making one
   a label would mint a new series per level and blow up cardinality. Levels index Loki events and
   the x-axis of graphs; they never enter the Prometheus label set.
5. **Bounded per-delegate cardinality.** Per-delegate Prometheus series exist only for a
   watchlist: a fixed set plus a rolling last-N of most-recently-active delegates (default N=100).
   This caps the dominant cardinality driver even on mainnet's hundreds of bakers. Loki, which is
   not cardinality-sensitive, retains every delegate.
6. **Datasources bound by name, not UID.** Dashboards reference `${thanos}` / `${loki}` template
   variables resolved by name, so the same compiled dashboards work on any fleet Grafana (and the
   demo) without hard-coding runtime UIDs.
7. **Dashboards compiled in CI, with a drift-gate.** Grafana dashboards are
   [grafonnet](https://github.com/grafana/grafonnet) sources compiled to JSON; a CI gate fails if
   the committed JSON (what the chart ships) drifts from a fresh compile, so a dashboard edit can't
   silently fail to deploy.

---

## 4. Resource-consumption evaluation

The exporter is deliberately light — it is an RPC client with a bounded working set, not a
database. Measured on the live fleet (one exporter per network, busiest network shown):

| | Config | Measured (mainnet) |
|---|---|---|
| **CPU** | request `50m`, limit `300m` | **~9m** actual |
| **Memory** | request `64Mi`, limit `192Mi` | **~12Mi** RSS |

For scale, the *node* it observes uses ~74m CPU and ~4.9 GiB on the same host — the exporter is a
rounding error next to the thing it watches.

**Metric cardinality (Thanos series per network):**

| network | series | tracked delegates (window) |
|---|---|---|
| mainnet | ~5 400 | ~200 |
| shadownet | ~750 | ~23 |
| bakingnet | ~480 | ~13 |
| ushuaianet | ~450 | ~12 |

Series count tracks the delegate watchlist (each tracked delegate adds a reception-delay
histogram); the fixed aggregate metrics are ~20 series per exporter. The `capacity` knob is the
direct lever on Prometheus footprint.

**Event throughput (Loki):**

| network | events/s | Loki volume/day |
|---|---|---|
| mainnet | ~81 | ~2.4 GiB |
| shadownet | ~15 | ~0.44 GiB |
| ushuaianet | ~12 | ~0.36 GiB |
| bakingnet | ~9 | ~0.25 GiB |

Fleet total is roughly **115 events/s** and **~3.5 GiB/day** of Loki ingest across four networks —
dominated by mainnet's committee size. Loki cost is thus a function of committee size and
retention, both knowable in advance; the exporter ships a self-observability dashboard that tracks
exactly these numbers so retention can be sized from data rather than guessed.

![Exporter pipeline dashboard](https://gitlab.com/tezos-infra/octez-consensus-exporter/-/raw/main/docs/images/dashboards/pipeline-data-flow.png)
*The exporter measuring its own footprint: event rate, Loki throughput, active series and a
retention-bounded on-disk estimate.*

---

## 5. Landing it safely in production

The exporter shares a node's RPC and a fleet's Prometheus/Loki, so "safe" means: don't destabilise
the node, don't blow up the shared TSDB, and fail loudly rather than silently.

- **Canary first.** New behaviour is validated on a testnet node before mainnet, then rolled to
  the fleet. One `Application` per network (generated by an Argo CD ApplicationSet from the public
  teztnet list) keeps the blast radius at a single network.
- **Deterministic images.** Workloads run on digest-pinned images, not floating tags, so a rollout
  is reproducible and a rollback is exact.
- **Self-healing bootstrap.** Nodes bootstrap from snapshots behind a head-progress probe that
  restarts a stuck sync, so the fleet comes up and recovers without hand-holding.
- **Cardinality is the safety valve.** The watchlist cap bounds the exporter's contribution to the
  shared Prometheus regardless of how many bakers appear; resource limits bound CPU/memory. The
  shared TSDB cannot be surprised.
- **RPC surface is explicit.** A separate-pod client hitting `monitor` and `helpers` endpoints
  needs the node's ACL relaxed (`--allow-all-rpc`); this is a deliberate, documented chart setting,
  not an accident.
- **Fail loud.** CI drift-gates (dashboards, and a node-gated regeneration of the RPC types)
  turn a silent divergence into a red pipeline.

---

The result is a consensus-health view that sits beside the rest of the fleet's node, host and
network metrics — correlatable in one Grafana, alertable through the same Alertmanager, and cheap
enough that running one per node across every public network is unremarkable.

Open source and self-contained; issues and contributions welcome:
[gitlab.com/tezos-infra/octez-consensus-exporter](https://gitlab.com/tezos-infra/octez-consensus-exporter).

### References

- Octez documentation — <https://octez.tezos.com/docs/>
- Tenderbake (Tezos consensus) — <https://octez.tezos.com/docs/active/consensus.html>
- Data Availability Layer (DAL) — <https://docs.tezos.com/architecture/data-availability-layer>
- teztnets — <https://teztnets.com/>
- grafonnet — <https://github.com/grafana/grafonnet>
</content>
</invoke>
