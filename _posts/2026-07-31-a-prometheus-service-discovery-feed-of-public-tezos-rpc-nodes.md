---
layout: post
title: "A Prometheus service-discovery feed of public Tezos RPC nodes"
date: 2026-07-31 09:00:00 +0000
categories: [observability, tezos]
tags: [prometheus, json-exporter, http-sd, service-discovery, tezos, octez, alerting, monitoring]
---

When you run your own blockchain nodes, two failure modes look identical from
the inside: **your node is stuck**, and **the whole chain has halted**. Your
node's head level stops moving in both cases. To tell them apart you need an
external reference — and the natural one is the set of *public* RPC nodes that
other operators run.

The problem is that "the set of public Tezos RPC nodes" doesn't exist in any
one place. It's scattered across several lists, each maintained by a different
project, each in its own format:

- [`teztnets.com/teztnets.json`](https://teztnets.com/teztnets.json) — test
  networks, with one or more public RPC URLs each
- [`taquito.io/rpc_nodes.json`](https://taquito.io/rpc_nodes.json) — the
  community list the Taquito SDK ships
- [docs.tezos.com/architecture/nodes](https://docs.tezos.com/architecture/nodes)
  — the documentation's table of public endpoints
- octez.js `rpc_nodes.json` — the list the octez.js SDK consumes

None of them is in a format Prometheus can consume directly. So I built a small
project that does the boring part on a schedule:
**[tezos-facts](https://gitlab.com/tezos-infra/vigies/tezos-facts)** fetches
all of these sources, deduplicates the union, and publishes it as a
[Prometheus HTTP service-discovery](https://prometheus.io/docs/prometheus/latest/http_sd/)
file on GitLab Pages:

> **<https://tezos-infra.gitlab.io/vigies/tezos-facts>**

(The publishing pattern — scheduled CI materializing upstream APIs into
committed, versioned files — is the one I described in
[an earlier post]({% post_url 2026-07-11-syncing-a-committed-config-file-from-an-upstream-api-with-scheduled-ci %}).)

## What the feed looks like

The main file is `merged_prometheus_sd.json`: standard `http_sd` format, one
target per public RPC endpoint, with labels you can filter on:

- **`tezos_network`** — `mainnet`, `ghostnet`, the current test networks…
- **`provider`** — who operates the endpoint, when the source knows
- **`source_sd`** — which upstream list the target came from (`teztnets`,
  `taquito`, `tezos_docs`, `octez_js`), so you can keep or drop sources with a
  single `relabel_configs` rule

Per-source files are published alongside the merged one if you'd rather trust a
single upstream.

## Wiring it into Prometheus

There's a twist: a Tezos node exposes a JSON RPC, not a `/metrics` endpoint.
Prometheus can *discover* the targets from the feed, but it can't scrape them
directly. The standard answer is the
[Prometheus JSON exporter](https://github.com/prometheus-community/json_exporter)
in probe mode: Prometheus hands the exporter a target URL, the exporter fetches
the JSON and extracts values with JSONPath.

The scrape config does all the plumbing with relabeling — the discovered
address becomes the `target` parameter, and the actual scrape goes to the
exporter:

```yaml
scrape_configs:
  - job_name: tezos_public_rpc_head
    metrics_path: /probe
    params:
      module: [tezos_head_header]
    http_sd_configs:
      - url: https://tezos-infra.gitlab.io/vigies/tezos-facts/merged_prometheus_sd.json
        refresh_interval: 1h
    relabel_configs:
      # SD target host -> the RPC URL the JSON exporter must fetch
      - source_labels: [__address__]
        target_label: __param_target
        replacement: https://${1}/chains/main/blocks/head/header
      - source_labels: [__address__]
        target_label: instance
      # send the scrape to the JSON exporter instead of the node
      - target_label: __address__
        replacement: json-exporter:7979
```

And the exporter module (`--config.file`) that pulls the head level out of the
header:

```yaml
modules:
  tezos_head_header:
    metrics:
      - name: tezos_public_node_head_level
        help: "Head block level from /chains/main/blocks/head/header"
        path: '{ .level }'
```

That's the whole integration. New public nodes appear in your target list
within the `refresh_interval`; decommissioned ones disappear the same way. No
config changes, no redeploys.

## What you get to alert on

With `tezos_public_node_head_level` in hand, the public head of each network is
one aggregation away:

```promql
max by (tezos_network) (tezos_public_node_head_level)
```

Two alerts fall out of it immediately.

**"My node is behind"** — compare your own node's head level (Octez exports it
natively via its metrics endpoint) against the public maximum:

```promql
  max by (tezos_network) (tezos_public_node_head_level)
- on (tezos_network)
  max by (tezos_network) (octez_validator_chain_head_level) > 5
```

If the public network is ahead of you, the problem is on your side.

**"The chain has halted"** — the public maximum itself stops moving:

```promql
delta(max by (tezos_network) (tezos_public_node_head_level)[10m:]) == 0
```

If *nobody's* head is advancing, your node is fine — the chain isn't. Those two
alerts firing together vs. separately is exactly the triage signal that a
node-only view can't give you.

Public nodes are, of course, public: some are slow, some are stale, some
rate-limit. Using `max` across many of them is what makes the reference robust
— any single healthy node is enough to establish the true head.

## Also in the box

The same site publishes a few other machine-readable facts, regenerated
automatically from their upstream sources:

- **`networks.json`** — live networks and their metadata, from teztnets.com
- **`network_aliases.json`** — what floating aliases like `currentnet` and
  `proposednet` currently point to
- **`releases.json`** — Octez releases, straight from the GitLab releases API

Everything is MIT-licensed and merge requests are welcome —
[gitlab.com/tezos-infra/vigies/tezos-facts](https://gitlab.com/tezos-infra/vigies/tezos-facts).
If you operate a public RPC endpoint and want it in the feed, the right move is
to get listed in one of the upstream sources (teztnets, Taquito, or
docs.tezos.com); the feed will pick it up on its own.
