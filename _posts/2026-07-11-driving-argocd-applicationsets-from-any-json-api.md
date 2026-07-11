---
layout: post
title: "Driving ArgoCD ApplicationSets from any JSON API"
date: 2026-07-11
categories: [kubernetes, gitops]
tags: [argocd, applicationset, plugin-generator, kubernetes, gitops, jq, jsonpath, sre]
mermaid: true
---

ArgoCD's `ApplicationSet` ships with generators for the common cases — a static `list`, files in `git`, registered `clusters`, pull requests, SCM org scans. But sooner or later you want to template Applications off a source none of those cover: a REST API, an internal service catalogue, a vendor's status feed, some JSON that already exists and is already kept current by someone else. That's what the **plugin generator** is for, and it's the least-documented of the bunch.

This post is a from-scratch, no-Helm walkthrough of standing one up: the protocol, the raw manifests, how to write the filter, and the handful of things that will bite you in production. I'll use a small open-source plugin — [`argocd-applicationset-json-plugin`](https://github.com/cmehat/argocd-applicationset-json-plugin) — that does one generic job: fetch JSON from a URL, filter it with `jq` or JSONPath, and hand the result back to ArgoCD as parameters. Nothing here is specific to it, though; the protocol is the protocol.

## What a plugin generator actually is

The key thing to internalise: **a plugin generator does not run your code inside the controller.** It makes an authenticated HTTP call to a service *you* deploy, and turns the JSON that comes back into generator parameters. The `argocd-applicationset-controller` is the client; your plugin is a tiny HTTP server.

The contract is a single route:

```
POST /api/v1/getparams.execute
Authorization: Bearer <token>
Content-Type: application/json

{ "applicationSetName": "my-appset", "input": { "parameters": {} } }
```

and the response is a list of parameter objects:

```json
{ "output": { "parameters": [
  { "name": "alpha", "region": "eu" },
  { "name": "beta",  "region": "us" }
] } }
```

Each object becomes one set of template variables. That's the whole interface.

<div class="mermaid">
flowchart LR
  G["ApplicationSet<br/>(plugin generator)"] -->|reads| CM["ConfigMap<br/>baseUrl + token ref"]
  AC["applicationset-controller"] -->|"POST getparams.execute"| SVC["your plugin Service"]
  SVC --> APP["JSON source (any URL)"]
  AC -->|"one Application per parameter"| OUT["Applications"]
</div>

## Deploying the plugin backend (raw manifests)

Four objects, all in the **same namespace as the ApplicationSet controller** (usually `argocd`). We'll come back to *why* that matters.

**Secret** — the shared token the controller authenticates with:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: json-plugin
  namespace: argocd
stringData:
  token: "replace-me-with-a-real-secret"
```

**Deployment** — the plugin server. It reads its config from environment variables and the token from a mounted file:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: json-plugin
  namespace: argocd
spec:
  replicas: 1
  selector:
    matchLabels: { app: json-plugin }
  template:
    metadata:
      labels: { app: json-plugin }
    spec:
      containers:
        - name: plugin
          image: ghcr.io/cmehat/argocd-applicationset-json-plugin:jq-latest
          env:
            - name: JSON_URL
              value: "https://api.github.com/users/kubernetes/repos?per_page=100"
            - name: JSON_FILTER          # jq expression
              value: 'map(select(.archived == false) | {name: .name, stars: (.stargazers_count|tostring)})'
          ports:
            - { name: http, containerPort: 4355 }
          volumeMounts:
            - { name: token, mountPath: /var/run/argo, readOnly: true }
          # The only route is POST-only; a GET returns 501. Probe the socket,
          # not the endpoint. (More on this below.)
          readinessProbe:
            tcpSocket: { port: 4355 }
          livenessProbe:
            tcpSocket: { port: 4355 }
      volumes:
        - name: token
          secret: { secretName: json-plugin }
```

**Service** — how the controller reaches it, in-cluster:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: json-plugin
  namespace: argocd
spec:
  selector: { app: json-plugin }
  ports:
    - { port: 4355, targetPort: 4355 }
```

**ConfigMap** — this is what the `ApplicationSet` references. ArgoCD discovers it by the `app.kubernetes.io/part-of: argocd` label, and reads the plugin's URL and token from it:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: json-plugin
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: argocd   # required for discovery
data:
  baseUrl: "http://json-plugin.argocd.svc.cluster.local:4355"   # include the port!
  token: "$json-plugin:token"           # $<secret-name>:<key>
```

## Wiring the ApplicationSet

Now the generator just points at that ConfigMap by name:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: repos
  namespace: argocd
spec:
  goTemplate: true
  generators:
    - plugin:
        configMapRef:
          name: json-plugin
        input:
          parameters: {}
        requeueAfterSeconds: 300   # re-fetch the source every 5 min
  template:
    metadata:
      name: "repo-{{ .name }}"
    spec:
      project: default
      source:
        repoURL: https://github.com/my-org/app-of-apps.git
        targetRevision: main
        path: "charts/generic"
        helm:
          values: |
            displayName: "{{ .name }}"
            stars: "{{ .stars }}"
      destination:
        server: https://kubernetes.default.svc
        namespace: "repo-{{ .name }}"
      syncPolicy:
        automated: { prune: true, selfHeal: true }
        syncOptions: [ CreateNamespace=true ]
```

Every object the plugin returns becomes one Application, refreshed on the requeue interval. New entries in the upstream JSON appear on their own; entries that disappear are pruned.

## Writing the filter: jq or JSONPath

The plugin ships two flavours — a `jq` image (`jq-*` tags) and a JSONPath image (`jsonpath-*` tags, the default). Both take the fetched JSON and must emit a **flat array of objects with string values** (ArgoCD parameters are strings).

A few generic shapes:

**Array of objects → pick fields** (e.g. the GitHub repos list above):

```
map({name: .name, url: .html_url})
```

**Object-of-objects → keys as items**, dropping entries that are aliases of another (a common pattern in registry-style files):

```
to_entries
| map(select(.value.aliasOf == null))
| map({name: .key})
```

**Filter by a property** — only public, only in a region, only "active":

```
map(select(.status == "active") | {name: .id, region: .region})
```

The JSONPath variant covers the simple cases without a `jq` binary — `JSON_PATH=$.*`, `JSON_PATH_KEYS_ONLY=true`, `JSON_PATH_EXCLUDE_IF_EXISTS=aliasOf` gives you the "keys, minus aliases" shape declaratively. Reach for `jq` when you need to reshape or compute fields.

Whichever you use, **test the filter against the real payload before you deploy it** — pipe the live URL through `jq` locally. Most plugin-generator "it produces nothing" incidents are actually a filter that returns `[]` or the wrong shape.

## Deployment notes (the things that bite)

Three lessons from running this for real:

**1. The plugin must live where the *controller* runs — not where the Applications deploy.**
This is the counterintuitive one. The controller resolves `configMapRef` in *its own* namespace and dials `baseUrl` over *its own* in-cluster network. If your ArgoCD control plane and your workloads are on different clusters (a very common topology), the plugin belongs on the **control-plane** cluster, next to the controller. Put it on the workload cluster and the controller just reports:

```
error getting plugin from generator: error fetching ConfigMap "json-plugin" not found
```

Where the generated Applications ultimately deploy is decided entirely by the template's `destination` — independently of where the plugin lives.

**2. Health-check the socket, not the endpoint.**
The plugin's only route is `POST /api/v1/getparams.execute`, and it's token-authenticated — a plain `GET` returns `501`. An `httpGet` probe against that path therefore *always fails*: liveness kills the container in a crash loop (`exitCode 137`), and readiness keeps the pod out of the Service's endpoints, so even a healthy process is unreachable. Use `tcpSocket` probes (as above). For a stateless request/response server, "is the port accepting connections?" is the right liveness signal.

**3. Two small ones.** Pin an image tag your registry actually has (an unbuilt tag gives `ImagePullBackOff` with a misleading `not found`), and make sure the ConfigMap's `baseUrl` carries the **service port** — no port means port 80, and the Service listens on 4355.

## When to reach for this

A plugin generator is the right tool when the source of truth is *external* JSON you don't want to copy into git, and you want Applications to track it live. It's a running service you have to operate, so it's overkill for a static list. But for "template one Application per row of this API," it turns an MR-per-change chore into a five-minute reconcile loop.

The plugin used here is open source: [argocd-applicationset-json-plugin](https://github.com/cmehat/argocd-applicationset-json-plugin). In the [next post]({% post_url 2026-07-11-auto-onboarding-tezos-testnets-with-teztnets-json %}) I put it to work against a real, public feed — the Tezos test-network registry.
