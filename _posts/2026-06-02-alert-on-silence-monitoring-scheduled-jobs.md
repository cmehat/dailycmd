---
layout: post
title: "Alert on silence: monitoring the jobs that run when nobody's watching"
date: 2026-06-02 09:00:00 +0000
permalink: /2026/06/alert-on-silence-monitoring-scheduled-jobs.html
tags: ["monitoring", "observability", "alerting", "prometheus", "heartbeat", "dead-mans-switch", "cron", "ci", "sre"]
author: "cm"
---

Every team has them: the scheduled jobs that quietly keep the lights on. The
nightly backup. The hourly dependency scan. The cron task that renews
certificates, rotates logs, or syncs data between systems. They run on a timer,
succeed without fanfare, and disappear from everyone's attention — until one day
they stop, and nobody notices for hours or days.

## The incident

A scheduled pipeline that ran every hour failed five times in a row. The cause
was mundane: a pinned container image tag had been pruned from the registry, so
every run died on an image-pull error before it could do any work. No code
changed. No alert fired. The job simply ceased to exist, hour after hour.

The breakage was discovered by accident — while chasing an unrelated downstream
problem, someone noticed the upstream work had stopped flowing. By then it had
been broken long enough that the backlog of skipped work had piled up.

The uncomfortable realization: **there was no monitoring on the job's own
health.** We monitored what the job produced. We did not monitor whether the job
ran at all.

## Why ordinary alerting misses this

The instinct is to add a failure handler: "if the job fails, send an alert."
Most CI systems and schedulers make this trivial — an `on_failure` notification,
a try/catch that posts to a chat channel.

But look closely at the incident. The job didn't fail. **The job never
started.** The container couldn't be pulled, so the runner never executed the
script, so there was nothing to catch a failure and nothing to fire the
notification. A failure handler is code that lives *inside* the job. If the job
can't launch, the handler can't run.

This is the central trap of monitoring scheduled work:

> You cannot alert on the failure of something that never happened. The absence
> of a signal produces no event.

A crashed process emits a stack trace. A timed-out request emits an error. But a
cron job that silently stops emits *nothing* — and "nothing" is exactly what
your alerting pipeline is not looking at.

## The fix: invert the logic with a heartbeat

The pattern that solves this is old enough to have several names — a
**heartbeat**, a **dead man's switch**, or **alert-on-absence**. The idea is to
flip the polarity of the alert:

- Don't alert when the job fails.
- Alert when the job *hasn't reported success recently enough.*

Concretely: on every **successful** run, the job pushes a single piece of
evidence that it completed — a timestamp. Something watches that timestamp. If
it goes stale, the watcher fires.

```text
on success:
    record "last successful run = now"

continuously, elsewhere:
    if (now - last_successful_run) > expected_interval + grace:
        ALERT
```

With Prometheus-style metrics, the job pushes a gauge on success and the rule
watches its age:

```yaml
- alert: ScheduledJobStale
  expr: time() - my_job_last_success_timestamp_seconds > 9000   # ~2.5h
  labels:
    severity: warning
  annotations:
    summary: "Scheduled job hasn't reported success in over 2.5h"

- alert: ScheduledJobMissing
  expr: absent(my_job_last_success_timestamp_seconds)
  for: 24h
  labels:
    severity: critical
  annotations:
    summary: "Scheduled job heartbeat metric is absent entirely"
```

Notice what this catches that a failure handler cannot:

- The job ran and failed → no fresh timestamp → alert. ✅
- The job couldn't even start (bad image, runner down, scheduler
  misconfigured) → no fresh timestamp → alert. ✅
- The job was silently deleted from the schedule entirely → no fresh
  timestamp → alert. ✅

The watcher lives *outside* the job, so it doesn't depend on the job being
healthy enough to report its own death. Silence becomes the signal.

### Two thresholds, not one

It's worth defining two separate alerts rather than one:

1. **Stale** — the timestamp exists but is older than expected (e.g., two missed
   runs). This is your day-to-day "something broke" signal.
2. **Missing** — there is *no* timestamp at all. This catches a different,
   sneakier failure: the monitoring itself was never wired up correctly, or
   credentials were wrong from day one, so the heartbeat never arrived even
   once.

The "missing" case is easy to forget, and it's precisely the one that bites you
when you deploy the monitoring and assume it works. An absent metric and a
healthy-but-quiet metric look identical if you only check for staleness.

## Alternatives I weighed (and why I passed)

Before settling on the push-a-heartbeat approach, several other options were on
the table. Each is reasonable; each lost on a specific axis worth naming.

**Poll the scheduler's API from outside.** Have an external prober ask the
CI/orchestration system "did the last run succeed?" The problem is
authentication: API polling needs a token, tokens expire, and expiring tokens
become a recurring manual chore — and a new silent-failure source of their own
when they lapse. I had an explicit goal of *no credentials that require periodic
human rotation.*

**Stand up a dedicated push-collector service.** A common pattern is to deploy a
small intermediary (a push gateway) that scheduled jobs push to, which then
exposes the metric for scraping. It works, but it means operating a new
component — its own deployment, its own ingress, its own auth — to do a job the
existing metrics-ingestion endpoint already did. More infrastructure for no
behavioral gain.

**Use a third-party dead-man's-switch service.** Hosted "ping me on this
schedule or I'll alert you" services implement exactly this pattern and are
genuinely good. I passed only because it would route alerts through a *separate*
channel, parallel to the alerting fabric every other system already used.
Consistency of the alerting path — one place alerts are defined, enriched, and
routed — was worth more to me than saving the setup. (If you don't already have
a mature alerting pipeline, an external snitch is often the fastest correct
answer.)

**An `on_failure` notifier in the job config.** Already covered above: it can't
fire when the job never launches, which was the actual incident.

The meta-lesson in this list: **prefer the option that reuses your existing
fabric.** The best monitoring is monitoring that's defined, routed, and silenced
the same way as everything else you already operate. Novel side-channels are
extra surface to maintain and extra places for an alert to get lost.

## The part nobody warns you about: it's the plumbing that bites

Designing the pattern took an afternoon. Making it actually deliver a metric
end-to-end took considerably longer — and the obstacles were entirely
incidental, not architectural. Three of them are worth generalizing, because you
will meet their cousins:

**1. Minimal images don't have the tools you assume.** The agent image used to
push the metric shipped without `curl`, `wget`, `nc`, or a scripting
interpreter — just a shell and a TLS library. A naive `if curl -fsS ...; then`
didn't error loudly; it hit "command not found" *inside an `if` condition*,
which most shells exempt from `set -e`, so it silently looped until a timeout.
The fix was to use the shell's built-in TCP capability instead of assuming a
binary exists:

```bash
# No curl in the image — probe with bash's built-in /dev/tcp
if exec 3<>/dev/tcp/"$HOST"/"$PORT"; then
    echo "endpoint reachable"
else
    echo "endpoint unreachable" >&2
    exit 1
fi
```

> Lesson: never assume a container has standard CLI tools. Slim and
> "distroless"-style images strip them deliberately. Check what's actually in
> the image.

**2. Defaults can be mutually incompatible.** The agent refused to start because
its default scrape timeout was *longer* than the scrape interval I'd configured
— an invalid combination it rejected outright. Two perfectly reasonable
defaults, fatal in combination.

**3. Metric and API names drift across versions.** The counter I polled to
confirm "the sample was actually delivered" had been *renamed* in a recent major
version of the tool. Polling the old name would have looked fine while silently
never confirming delivery — a monitoring system that lies green.

What tied all three together — and the single most transferable takeaway:

> When you wrap a long-running, daemon-shaped tool inside a one-shot script,
> **run that exact script against that exact image locally before you ship it.**
> Tooling drift (no `curl`) and version drift (renamed metrics) are invisible
> from documentation, `--help` output, or reading the config schema. They only
> surface when you actually execute the real binary in the real image.

A few minutes reproducing the job locally — pointed at a throwaway local
receiver — caught all three before they reached production.

## A concrete implementation

The scheduled job in
question is [Renovate](https://github.com/renovatebot/renovate) (a dependency
update bot) running hourly in CI. The metric is shipped with
[Grafana Alloy](https://github.com/grafana/alloy) via `prometheus.remote_write`
to a Prometheus-compatible receiver (Thanos Receive, here), reusing the same
ingestion endpoint the rest of the fleet already uses — no new component, no
token to rotate.

> Everything below was tested before publishing: `shellcheck` + `bash -n` on the
> script; the Alloy config and script run inside the real `grafana/alloy:v1.13.1`
> image against a `prom/prometheus --web.enable-remote-write-receiver` standing
> in for the receiver; `actionlint` + `yamllint` on the workflows; and
> `promtool check rules` on the alert rules. The metric was confirmed to land in
> the receiver with a stable series identity across runs.

### The Alloy config

The exporter reads a `*.prom` textfile, the scrape ships it, and a `relabel`
step pins a **stable series identity** — without it, every run is scraped from a
fresh ephemeral runner whose hostname becomes the `instance` label, so you'd
accumulate a trail of orphaned series instead of updating one. (I learned that
the hard way: two runs produced two series with different `instance` labels
until I added the relabel.)

```river
// ci/heartbeat/alloy.river
prometheus.exporter.unix "heartbeat" {
  set_collectors = ["textfile"]
  textfile {
    directory = "/heartbeat/textfile"
  }
}

prometheus.scrape "heartbeat" {
  targets    = prometheus.exporter.unix.heartbeat.targets
  forward_to = [prometheus.relabel.heartbeat.receiver]

  // scrape_timeout default (10s) > our interval -> Alloy refuses the config at
  // startup. Pin both to 1s so a one-shot run scrapes and ships immediately.
  scrape_interval = "1s"
  scrape_timeout  = "1s"
}

prometheus.relabel "heartbeat" {
  forward_to = [prometheus.remote_write.receiver.receiver]
  rule {
    target_label = "job"
    replacement  = "renovate-heartbeat"
  }
  rule {
    target_label = "instance"
    replacement  = "renovate-scheduled-pipeline"
  }
}

prometheus.remote_write "receiver" {
  endpoint {
    url = sys.env("THANOS_RECEIVE_URL")
    basic_auth {
      username = sys.env("THANOS_RECEIVE_USERNAME")
      password = sys.env("THANOS_RECEIVE_PASSWORD")
    }
  }
}
```

### The push script (no `curl` in the image)

```bash
#!/usr/bin/env bash
# ci/heartbeat/push_metrics.sh
# Write a heartbeat, then BLOCK until Alloy confirms a sample was delivered.
# The grafana/alloy image ships no curl/wget/nc/python -- only bash + openssl.
set -euo pipefail

METRIC_NAME="renovate_scheduled_pipeline_last_success_timestamp_seconds"
TEXTFILE_DIR="/heartbeat/textfile"
ALLOY_ADDR="127.0.0.1"; ALLOY_PORT="12345"
DEADLINE_SECONDS="${DEADLINE_SECONDS:-60}"

# Raw HTTP GET over bash's built-in /dev/tcp -- no curl available.
http_get() {
  local host="$1" port="$2" path="$3" line body=""
  exec 3<>"/dev/tcp/${host}/${port}" || return 1
  printf 'GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n' "$path" "$host" >&3
  while IFS= read -r line <&3; do [ "$line" = $'\r' ] && break; done
  while IFS= read -r line <&3; do body+="${line}"$'\n'; done
  exec 3>&-
  printf '%s' "$body"
}

mkdir -p "$TEXTFILE_DIR"
now="$(date +%s)"
cat > "${TEXTFILE_DIR}/heartbeat.prom" <<EOF
# HELP ${METRIC_NAME} Unix time of the last successful scheduled pipeline run.
# TYPE ${METRIC_NAME} gauge
${METRIC_NAME} ${now}
EOF

alloy run /heartbeat/alloy.river \
  --server.http.listen-addr="${ALLOY_ADDR}:${ALLOY_PORT}" &
ALLOY_PID=$!
trap 'kill "$ALLOY_PID" 2>/dev/null || true' EXIT

# The Alloy v1.x delivery counter is prometheus_remote_storage_samples_total
# -- NOT the older prometheus_remote_write_samples_total. Polling the wrong
# name loops forever even when networking is fine.
deadline=$(( $(date +%s) + DEADLINE_SECONDS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  metrics="$(http_get "$ALLOY_ADDR" "$ALLOY_PORT" /metrics || true)"
  delivered="$(printf '%s\n' "$metrics" \
    | awk '/^prometheus_remote_storage_samples_total/ {s += $NF} END {print s+0}')"
  if [ "${delivered%.*}" -ge 1 ] 2>/dev/null; then
    echo "delivered ${delivered} sample(s)"; exit 0
  fi
  sleep 1
done
echo "ERROR: no sample delivered within ${DEADLINE_SECONDS}s" >&2
exit 1
```

### GitLab CI

The heartbeat job uses the Alloy image directly and runs *only* after Renovate
succeeds:

```yaml
# .gitlab-ci.yml
stages: [renovate, heartbeat]

renovate:
  stage: renovate
  image: renovate/renovate:43.150.1   # the original incident: this tag got pruned
  script: [renovate]
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'

heartbeat:
  stage: heartbeat
  image: grafana/alloy:v1.13.1
  needs: ["renovate"]
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
      when: on_success          # fresh timestamp == Renovate really finished
  variables:
    THANOS_RECEIVE_URL: "$THANOS_RECEIVE_URL"
    THANOS_RECEIVE_USERNAME: "$THANOS_RECEIVE_USERNAME"
    THANOS_RECEIVE_PASSWORD: "$THANOS_RECEIVE_PASSWORD"
  script:
    - bash ci/heartbeat/push_metrics.sh
```

### GitHub Actions equivalent

One wrinkle when porting: the Alloy image has no Node.js, so using it as a job
`container:` would break JavaScript actions like `actions/checkout`. The clean
fix is to keep a normal runner and run the *same* script inside the Alloy image
via `docker run`:

```yaml
# .github/workflows/renovate.yml
name: renovate
on:
  schedule:
    - cron: "0 * * * *"   # hourly, same cadence as the staleness threshold
  workflow_dispatch: {}

jobs:
  renovate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run Renovate
        uses: renovatebot/github-action@v40.3.6
        with:
          token: ${{ secrets.RENOVATE_TOKEN }}

  heartbeat:
    needs: renovate          # implicit success() gate: only runs if Renovate passed
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Push heartbeat
        env:
          THANOS_RECEIVE_URL: ${{ secrets.THANOS_RECEIVE_URL }}
          THANOS_RECEIVE_USERNAME: ${{ secrets.THANOS_RECEIVE_USERNAME }}
          THANOS_RECEIVE_PASSWORD: ${{ secrets.THANOS_RECEIVE_PASSWORD }}
        run: |
          docker run --rm \
            -v "$PWD/ci/heartbeat:/heartbeat" \
            -e THANOS_RECEIVE_URL \
            -e THANOS_RECEIVE_USERNAME \
            -e THANOS_RECEIVE_PASSWORD \
            --entrypoint /usr/bin/env \
            grafana/alloy:v1.13.1 \
            bash /heartbeat/push_metrics.sh
```

### The alerts

```yaml
groups:
  - name: renovate-scheduled-pipeline
    rules:
      - alert: RenovateScheduledPipelineStale
        # The `last_over_time(...[24h])` wrap is load-bearing — see the
        # postscript below for why a vanilla `time() - metric > 9000`
        # silently never fires under one-shot push.
        expr: |
          time() - max(last_over_time(
            renovate_scheduled_pipeline_last_success_timestamp_seconds[24h]
          )) > 9000
        labels: { severity: warning, team: infra }
        annotations:
          summary: "Renovate scheduled pipeline has not succeeded in >2.5h"

      - alert: RenovateScheduledPipelineMissing
        expr: absent(renovate_scheduled_pipeline_last_success_timestamp_seconds)
        for: 24h
        labels: { severity: critical, team: infra }
        annotations:
          summary: "Renovate heartbeat metric absent for 24h"
```

## Postscript: the alert that looked correct but never fired

A day after shipping the above, while sanity-checking a different incident,
I noticed something uncomfortable about the `RenovateScheduledPipelineStale`
rule. The expression looks textbook:

```promql
time() - renovate_scheduled_pipeline_last_success_timestamp_seconds > 9000
```

"Current time minus the last-success timestamp; if greater than 2.5 hours,
alert." Hard to misread.

It had been live for weeks. It had never fired.

Querying the production rule directly, well within the threshold window:

```
$ # ~53 minutes after a fresh heartbeat
$ promql 'time() - max(renovate_scheduled_pipeline_last_success_timestamp_seconds{source="..."})'
result_count = 0    ← no data; the rule evaluator sees nothing
```

The interaction is this: Prometheus (and Thanos, by default) uses a **5-minute
staleness lookback** when evaluating expressions. A series whose newest sample
is older than that lookback is treated as *absent* — not "stale with old value",
literally not there. `max(absent_series)` returns no data; the rule evaluator
treats no-data as `inactive`; the alert never advances to firing.

For a typical metric this is invisible — node-exporter scrapes every 15 seconds,
the 5-minute lookback always finds a recent sample. But the heartbeat here is
*one-shot*: Alloy runs for ~6 seconds at the end of each hourly pipeline, ships
~6 samples, and exits. From the rule evaluator's perspective, the series is
"absent" for **55 of every 60 minutes** between runs.

So the literal-looking expression has a sparse-emission interaction nobody warns
you about:

- When a heartbeat just landed: `time() - timestamp ≈ 0`. Not > 9000. Doesn't
  fire.
- When the metric is stale (90%+ of the time): expression returns no data. The
  evaluator can't compare no-data to 9000. Doesn't fire.

The alert is structurally incapable of firing under any sustained-failure
scenario. The "Missing" rule still works because `absent()` is exactly the
function designed to return a value when the series is gone — and `for: 24h`
correctly waits out the hourly successful pushes.

The fix is one wrap:

```promql
time() - max(last_over_time(
  renovate_scheduled_pipeline_last_success_timestamp_seconds[24h]
)) > 9000
```

`last_over_time(...[24h])` evaluates against samples within the last 24 hours,
robust to a 60-minute (or 23-hour) gap between pushes. If genuinely no heartbeat
in 24 hours, the expression returns empty — at which point the paired `Missing`
rule's `absent() for: 24h` is firing already. Coverage is split between the two
by design.

> Sparse heartbeats and the default staleness lookback are mutually
> incompatible. Wrap the latest-value lookup in `last_over_time(...[window])`
> where `window` is several multiples of the expected emission cadence.

(I caught this only because something *else* drew me to the rule. If you take
one thing from this section: dry-run your alert expressions against the actual
metric shape in production before you trust them. Looking right is not the same
as evaluating right.)

One thing worth calling out explicitly: wrap sparse-heartbeat lookups in
`last_over_time(...[window])`. The default staleness lookback (5 min) is sized
for typical scrape intervals (15–60 s); a metric pushed once per hour is a
fundamentally different shape, and a vanilla `max(...)` over it returns no data
90%+ of the time. Run your alert expressions against the real metric, between
pushes, before you ship them. The details are in the postscript above.
