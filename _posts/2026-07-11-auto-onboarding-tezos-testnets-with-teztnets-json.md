---
layout: post
title: "Auto-onboarding Tezos test networks with teztnets.json"
date: 2026-07-11 12:00:00 +0000
categories: [kubernetes, gitops]
tags: [argocd, applicationset, tezos, octez, teztnets, jq, gitops, kubernetes]
---
{% raw %}

I run a small fleet of [`octez-node`](https://tezos.gitlab.io/) instances — one Tezos node per active test network — on Kubernetes, deployed by ArgoCD. For a long time the list of "which networks exist" lived in a JSON file I maintained by hand in the GitOps repo: a network spins up upstream, I open an MR to add it, wait for review, merge; it gets decommissioned, I open another MR to remove it.

The annoying part is that the list already exists, upstream, and is already kept current. Tezos publishes the canonical registry of live test networks at **[https://teztnets.com/teztnets.json](https://teztnets.com/teztnets.json)** — every network, its category, and the exact `octez` image it expects. Copying that into my repo by hand is busywork. I wanted the `ApplicationSet` to read it directly.

This is a concrete application of the [ApplicationSet plugin generator]({% post_url 2026-07-11-driving-argocd-applicationsets-from-any-json-api %}) from the previous post — read that first for how the plugin works and how to deploy it (raw manifests, in the ArgoCD controller's namespace). Here I'll just wire it to `teztnets.json`.

## What teztnets.json looks like

It's an object keyed by network name. Trimmed:

```json
{
  "ghostnet": {
    "aliasOf": null,
    "category": "Long-running Teztnets",
    "docker_build": "tezos/tezos:octez-v23.1"
  },
  "ushuaianet": {
    "aliasOf": null,
    "category": "Protocol Teztnets",
    "docker_build": "tezos/tezos:octez-v25.0"
  },
  "ushuaianet-20260701": {
    "aliasOf": "ushuaianet",
    "category": "Protocol Teztnets"
  }
}
```

Three things matter for my use case:

- **`aliasOf`** — dated snapshots alias the rolling name; I only want the canonical entries (`aliasOf == null`).
- **`category`** — I only want the short-lived *protocol-test* networks, not the long-running public ones (which I run differently).
- **`docker_build`** — `tezos/tezos:octez-v25.0`; I need the tag part, `octez-v25.0`, to pin the node image per network.

## The filter

The plugin's job is to turn that object into a flat list of `{name, octezTag}`. As a `jq` filter (`JSON_FILTER`):

```
to_entries
| map(select(.value.aliasOf == null
             and (.value.category | test("Protocol"))))
| map({
    name: .key,
    octezTag: (.value.docker_build | split(":")[1])
  })
```

Fed the live payload, today that returns exactly:

```json
[ { "name": "ushuaianet", "octezTag": "octez-v25.0" } ]
```

One network now; more when upstream announces the next protocol test net. `JSON_URL` is `https://teztnets.com/teztnets.json`, and that filter is the only network-specific configuration the plugin needs.

## The ApplicationSet

The generator is a `matrix` of the plugin (which networks?) against a single-element `list` (which cluster do the nodes run on?). The template stamps one `octez-node` per network, using the network name and the image tag the plugin extracted:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: octez-test
  namespace: argocd
spec:
  goTemplate: true
  goTemplateOptions: ["missingkey=error"]
  generators:
    - matrix:
        generators:
          - plugin:
              configMapRef:
                name: json-plugin      # the teztnets-configured plugin
              input:
                parameters: {}
              requeueAfterSeconds: 300
          - list:
              elements:
                - cluster: my-workload-cluster
                  server: https://my-workload-cluster.example   # where nodes run
  template:
    metadata:
      name: "octez-{{ .name }}-test"
      labels:
        network: "{{ .name }}"
    spec:
      project: default
      destination:
        server: "{{ .server }}"
        namespace: "octez-{{ .name }}-test"
      source:
        # octez-node packaged as a Helm chart — point repoURL/chart at wherever
        # you host it. Only the values below are network-specific.
        repoURL: <your-octez-node-chart-repo>
        chart: octez-node
        targetRevision: 0.9.1
        helm:
          releaseName: "octez-{{ .name }}-test"
          values: |
            network: "{{ .name }}"
            history: "rolling"
            image:
              repository: tezos/tezos
              tag: "{{ .octezTag }}"
            persistence:
              enabled: true
              size: 250Gi
      syncPolicy:
        automated: { prune: true, selfHeal: true }
        syncOptions: [ CreateNamespace=true ]
```

Note the split of responsibilities that makes this robust: the **plugin** (running next to the ArgoCD controller) answers *which networks exist and on what image*; the **template's `destination`** decides *where the nodes actually run*. The two are independent — the plugin can be on your control-plane cluster while every node lands on a separate workload cluster.

## The payoff

With `prune: true`, the loop is fully self-maintaining on a five-minute requeue:

- a new protocol-test network announced on `teztnets.com` becomes a running `octez-{name}-test` node with **no MR**;
- a decommissioned network drops out of `teztnets.json` and its node is pruned the same way;
- the image tag always matches what upstream says the network expects, because it comes straight from `docker_build`.

The hand-maintained network file is out of the critical path entirely.

## The trade-off

Reading the registry live means you follow upstream's image tag *exactly and immediately* — which is the point, but also means you've handed your rollout timing to `docker_build`. If you need to **pin** each network to a reviewed image, gate upgrades behind a policy, or keep an auditable git diff of "what changed and when," you want a committed file you regenerate on a schedule instead — see the [companion post]({% post_url 2026-07-11-syncing-a-committed-config-file-from-an-upstream-api-with-scheduled-ci %}).

For a disposable, follow-upstream test fleet, live is exactly right: nobody has to touch it when the network list changes.
{% endraw %}
