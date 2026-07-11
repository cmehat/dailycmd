---
layout: post
title: "Why your Loki alerts never fire — four layers of silent failure"
date: 2026-06-04 09:00:00 +0000
permalink: /2026/06/why-your-loki-alerts-never-fire-four-layers-of-silent-failure.html
tags: ["loki", "alerting", "observability", "kubernetes", "sre", "debugging", "helm", "alertmanager", "prometheus", "gitops"]
author: "cm"
---
{% raw %}


A single alert didn't fire for two and a half months. Four independent bugs, each masking the next, each failing silently. The metrics endpoint told the whole story; the logs told none of it.

The setup: self-hosted Loki receiving logs from a handful of Kubernetes clusters, rules in a ConfigMap synced by a sidecar, alertmanager from a sub-chart of the same Helm umbrella. Names (`my-loki`, `my-alert`, `my-channel`) are anonymized.

## TL;DR

A LogQL-based alert sat in a ConfigMap, watched by a sidecar, mounted into the ruler container — and silently produced nothing for ten weeks. Four serial bugs, each masking the next:

1. The sidecar wrote the file to `/etc/loki/rules/` but the ruler expects `/etc/loki/rules/<tenant>/`.
2. The line filter used `{label=~".*"}` which Loki rejects ("empty-compatible regex"), so the rule never parsed.
3. The line filter caught one phrasing of the error log but not the other.
4. The `alertmanager_url` resolved to a service that didn't exist — the chart's sub-chart had a longer name than the umbrella's release name.

Each layer failed silently. The metrics endpoint told the whole story; the logs told none of it.

## The setup

```
ConfigMap (rules)                            alertmanager service
   ↑                                              ▲
   │ (kustomize configMapGenerator)               │
   │                                              │
+-----------------------------------+   alertmanager_url  +--------------------+
| loki-backend (SimpleScalable)     | ───────────────────►| alertmanager       |
|                                   |                     | (from a sub-chart) |
|   sidecar ──writes──► emptyDir    |                     +--------------------+
|              /etc/loki/rules/...                                ▼
|   ruler ──reads──► same emptyDir                          Slack route
|              /etc/loki/rules/...
+-----------------------------------+
```

Standard "ConfigMap → emptyDir → ruler" pattern. The chart values point the ruler at the alertmanager's in-cluster DNS name.

## The rule

A simple log-based alert: when a particular fatal log line appears, page the team.

```yaml
- alert: my-alert
  expr: |
    sum by (instance) (rate({app=~".*"} |= "fatal: store corruption" [5m])) > 0
  for: 0s
  labels:
    severity: warning
    maturity: wip
  annotations:
    summary: "Fatal store corruption on {{ $labels.instance }}"
```

`for: 0s` means it should fire on the *first* eval after a match. Couldn't be simpler.

## The discovery

I was investigating an unrelated regression and noticed something off: a known-bad pod had been crash-looping for hours, log lines were piling up in Loki containing exactly the substring the alert was supposed to catch, and the team's WIP-alerts channel was completely silent.

Slack search across the whole instance, bots included, for the alert name: **zero hits** since the rule had been merged ten weeks earlier.

That's a long time for "the alert never fired". Time to find out why.

## Layer 1 — The rule on disk

The first thing to check is whether the rule file actually lands where the ruler reads from. The container is distroless (no shell, no `ls`), but the sidecar is a Python container:

```sh
kubectl -n monitoring exec my-loki-backend-0 -c sidecar -- python3 -c \
  'import os; [print(os.path.join(r,f)) for r,_,fs in os.walk("/etc/loki/rules") for f in fs]'
```

```
/etc/loki/rules/my-alert.yaml
```

Sidecar did its job. File is there.

Now ask the ruler what it has loaded:

```sh
kubectl exec … -- python3 -c \
  'import urllib.request, json;
   r=urllib.request.Request("http://127.0.0.1:3100/prometheus/api/v1/rules",
                            headers={"X-Scope-OrgID":"anonymous"});
   print(json.dumps(json.loads(urllib.request.urlopen(r).read()), indent=2))'
```

```
{"data": {"groups": []}, "status": "success"}
```

**Zero groups.** The ruler can see no rules at all, even though the file is right there on disk.

The fix: Loki's local rule store doesn't scan a flat directory; it scans `<directory>/<tenant_id>/<rule_file>`. With `auth_enabled: false`, the ruler's implicit tenant is `anonymous`. Files placed directly under the configured directory, with no tenant subdirectory, are silently ignored. The chart's `sidecar.rules.folder` needed to be `/etc/loki/rules/anonymous`, not `/etc/loki/rules`.

(Note: Loki uses `fake` as the tenant on the query path and `anonymous` on the ruler path. The two are not interchangeable.)

Move the file under the tenant subdir and check again.

## Layer 2 — The ruler refuses to parse the file

```
{"data": {"groups": []}, "status": "success"}
```

Still empty. Now to the logs:

```sh
kubectl logs my-loki-backend-0 -c loki --since=30m | grep -E 'rule|ruler' | head
```

```
level=error caller=ruler.go:579 msg="unable to list rules"
  err="failed to list rule groups for user anonymous:
       error parsing /etc/loki/rules/anonymous/my-alert.yaml:
       parse error : queries require at least one regexp or equality matcher
       that does not have an empty-compatible value. For instance, app=~\".*\"
       does not meet this requirement, but app=~\".+\" will"
```

Exactly the regex used in the rule: `{app=~".*"}`. Loki refuses to load any rule file with an empty-compatible regex matcher, because such a query would have to scan every stream the instance knows about — billions of bytes, with no way to fingerprint a useful index.

The fix is to make the selector specific. The right answer here was switching to a label that's actually meaningful (`{app="the-actual-app"}`), which has the added benefit of dropping the candidate-stream count from ~3700 to ~30.

That was the bug the ruler complained about, in plain English, in the logs — but only after layer 1 was fixed. As long as the file was at the wrong path, the ruler wasn't even attempting to parse it, so the parse-error log line never showed up.

## Layer 3 — The line filter is too narrow

Now the rule loads. The ruler logs show it evaluating every 15 seconds. But the alert still doesn't fire.

Sanity-check the expression directly against Loki. Pull a sample of recent matches:

```sh
TOKEN=…   # service account token
DS=https://grafana.example.com/api/datasources/proxy/uid/<loki-uid>

curl -fsS --get \
  --data-urlencode 'query={app="my-app"} |= "fatal: store corruption"' \
  --data-urlencode "start=$(date -u -d '7 days ago' +%s)000000000" \
  --data-urlencode "end=$(date -u +%s)000000000" \
  --data-urlencode "limit=10" \
  -H "Authorization: Bearer $TOKEN" "$DS/loki/api/v1/query_range" | jq '.data.result[]'
```

Three matches in the last week. None on the day I was looking at the known-bad pod.

A look at the crash-looping pod's actual log lines shows why: the application emits two different phrasings of the same logical error, only one of which contains the substring the alert filters on. The first phrasing comes from one code path (`fatal: store corruption`), the second from another (`Store_corruption` — capitalised, underscored — bubbled up as part of an exception type name).

`|=` is a case-sensitive substring match. Of course it didn't catch the second phrasing. Switch to a regex alternation:

```
|~ "fatal: store corruption|Store_corruption"
```

…and the count over the last week jumps from "the few I found" to "thousands" — matching almost exactly the duration of the crash-loop event.

## Layer 4 — The notifier dispatches into the void

Now the rule is loaded, the selector is correct, and the expression evaluates to a non-zero rate during the crash-loop window — verified directly:

```sh
# At the time the pod was crash-looping
curl --data-urlencode 'query=<expr> > 0' --data-urlencode "time=<that timestamp>" …
```

```
[{"metric":{...},"value":[<ts>,"0.0033333333"]}]
```

`0.003 > 0` → alert should be firing.

Slack: still zero. The wip channel receives plenty of other alerts; it's not the channel.

The answer is in the ruler's metrics. There's a family of `loki_prometheus_notifications_*` counters that count attempts the ruler makes to dispatch to alertmanager:

```sh
kubectl exec … -c sidecar -- python3 -c \
  'import urllib.request;
   [print(l) for l in urllib.request.urlopen("http://127.0.0.1:3100/metrics").read().decode().split("\n")
    if "loki_prometheus_notifications_" in l and not l.startswith("#")]'
```

```
loki_prometheus_notifications_alertmanagers_discovered{user="anonymous"}              1
loki_prometheus_notifications_sent_total{alertmanager="http://…/api/v2/alerts"}       2590
loki_prometheus_notifications_errors_total{alertmanager="http://…/api/v2/alerts"}     2590
loki_prometheus_notifications_dropped_total{user="anonymous"}                         2590
loki_prometheus_notifications_latency_seconds{quantile="0.5"}                         NaN
loki_prometheus_notifications_latency_seconds_count                                   2590
```

Every attempted dispatch — 2,590 of them — errored out, was dropped, and produced a `NaN` latency quantile because no request ever succeeded long enough to record a duration. And `alertmanagers_discovered=1` only means the URL parsed, not that it pointed at anything real.

Why? DNS:

```sh
kubectl exec … -c sidecar -- python3 -c \
  'import socket; print(socket.gethostbyname_ex("the-configured-alertmanager-service.monitoring.svc.cluster.local"))'
```

```
socket.gaierror: [Errno -2] Name does not resolve
```

The chart we use packages alertmanager as a sub-chart of a larger observability umbrella. When the umbrella is installed under a given release name, the sub-chart synthesises its service name from the umbrella's name *plus* its own component tag — not just the umbrella's release name. So `kubectl get svc -n monitoring | grep alert` shows a service name with an extra segment in the middle, several characters longer than what the original author of the rule had hand-typed into the values file.

The notifier never logged the DNS failure (DNS errors are info-level in the upstream notifier code, and the bundled binary doesn't surface them). The only place the failure appeared was in the metrics endpoint — and even there, the counters initially read as "lots of activity, all happening", which is the wrong intuition until you look at `errors_total` and `dropped_total` side by side.

One-character fix to the values file (well, one segment) and we wait for the pods to roll.

## Verification

```
loki_prometheus_notifications_errors_total{alertmanager="…right-name…"}  0
loki_prometheus_notifications_dropped_total                              0
loki_prometheus_notifications_sent_total                                 0  (no firings yet — the pod stopped crash-looping)
loki_prometheus_notifications_latency_seconds_count                      0
```

A finite latency quantile would have been even better evidence, but we'd need a firing to populate it. The right read on a freshly-rolled pod is: `errors_total` and `dropped_total` stop incrementing, the alertmanager target label in the metric carries the *new* URL, and `alertmanagers_discovered` is 1.

## Why all four hid behind each other

Each layer fails silently in its own particular way, and each masks the next:

1. **Wrong path** → the ruler can't find the file. There's no error log because the ruler isn't even trying to read this file — it's scanning a directory the file isn't in. You can't see the parse error from layer 2 until you fix layer 1.
2. **Bad regex** → the ruler tries to parse the file, fails, and logs the error. But you only notice the parse-error logs after you've fixed layer 1 and started looking. From the outside, layer 1 and layer 2 produce identical symptoms ("the alert isn't loaded").
3. **Narrow line filter** → the rule loads and evaluates, but the query returns zero. You can only tell whether that's "everything is fine, no real event" or "your regex is too narrow" by running the query directly and visually inspecting actual log lines for variant phrasings.
4. **Wrong alertmanager URL** → the notifier dispatches, increments `sent_total` (which counts attempts, not successes), and silently fails. The DNS error is buried at info-level. Only the `errors_total / sent_total / dropped_total` triplet exposes the truth.

None of the four layers emits a warning at the level the operator would see by accident. Every one needs a specific assertion when the rule is first wired up. The metrics endpoint, not the logs, is the source of truth at the dispatch layer.

## What to do differently

**Bench-test new rules before merging.** Drop a `vector(1) > 0` test rule in for ten minutes after every ruler-related change and watch it land on Slack. A passing test there proves all four layers at once. Remove it before merging the real rule.

**Monitor the notifier metrics.** A Grafana panel of `loki_prometheus_notifications_errors_total` per alertmanager target is five lines of PromQL. Without it, 2,590 failed dispatches look like 2,590 successful attempts until you check `errors_total` and `dropped_total` side by side.

The whole chain — ConfigMap, sidecar, ruler, notifier, alertmanager, Slack — looks like a single declarative pipeline. It isn't: each link is a separate process making independent decisions, and each fails without surfacing an error at the level an operator watches. Write the diagnostic chain down somewhere durable; you'll need it again.
{% endraw %}
