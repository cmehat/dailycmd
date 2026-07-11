---
layout: post
title: "Materializing an upstream API into git with scheduled CI"
date: 2026-07-11 15:00:00 +0000
categories: [gitops, ci]
tags: [gitlab-ci, gitops, jq, automation, scheduled-pipelines, ci-components]
---

Some **upstream API is the source of truth** for a list your infrastructure depends on — live networks, regions, tenants, feature flags — and you need that list inside your repo so your tooling (rendering, pinning, review, dependency bots) can act on it.

Two options: read the upstream live at deploy time, or **materialize it into git** — fetch it on a schedule, write it to a committed file, let a bot commit the diff. This post is about the second one: when it's the right call, and a reusable way to build it in GitLab CI. The concrete example is the public Tezos test-network registry at [`teztnets.com/teztnets.json`](https://teztnets.com/teztnets.json), but nothing here is specific to it.

## Why commit it instead of reading it live

Reading upstream live is simpler and always current. But materializing into git buys you things a live read can't:

- **Auditable history.** Every change to the list is a commit with a diff and a timestamp. "When did `foonet` appear?" is `git log`, not a guess.
- **Review and pinning.** The committed file can carry *your* metadata per row — a pinned image tag, a release channel, a rollout policy — that upstream doesn't know about. A live read would flatten all of that away.
- **A stable interface for other automation.** A dependency bot (Renovate/Dependabot) can bump the pinned versions in the file; renderers can read it deterministically; nothing at deploy time depends on the upstream being reachable.
- **Decoupled failure.** If upstream has a bad day, your last-known-good file is still in git. A live read fails your reconcile.

The cost is a moving part — a scheduled job — and a few footguns. Here's the whole thing.

## A reusable, parameterized job

The core insight that keeps this from sprawling: make the job take the *list of files to regenerate* as an **input**, so one job template covers every such file in the repo. GitLab's `spec.inputs` does this — a typed array parameter:

```yaml
# .gitlab/generate-networks.yml
spec:
  inputs:
    files:
      type: array
      description: >-
        Repo-root paths to regenerate from the upstream registry.
        Add a path here to bring another file under scheduled regeneration.
---
generate-networks:
  stage: generate
  image: alpine:3.22.1
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'   # only on a schedule
    - when: never
  before_script:
    - apk add --no-cache curl jq git bash
    - git config --global user.email "ci-bot@example.com"
    - git config --global user.name  "CI Bot"
  script:
    - |
      set -euo pipefail
      # inputs.files renders as a JSON array; regenerate each, stage all,
      # commit once so concurrent pushes never race.
      echo '$[[ inputs.files ]]' | jq -r '.[]' | while IFS= read -r target; do
        echo "=== Regenerating $target ==="
        bash .gitlab/generate-networks.sh "$target"
        git add "$target"
      done
      if git diff --cached --quiet; then
        echo "No changes"
      else
        git commit -m "chore: update networks files [ci skip]"
        git push "https://ci-bot:${GITLAB_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git" \
          HEAD:${CI_COMMIT_REF_NAME}
      fi
```

and you include it, passing the files, from your main pipeline:

```yaml
# .gitlab-ci.yml
include:
  - local: .gitlab/generate-networks.yml
    inputs:
      files:
        - networks-teztale.json
        - networks-test.json      # add a path -> it's now on the schedule
```

Three details in that job matter:

- **`rules` pins it to `schedule`.** It never runs on push or MR — only from a GitLab *scheduled pipeline* (say, nightly). Everything else is `when: never`.
- **`[ci skip]` on the commit** stops the bot's own push from triggering another pipeline — no loops.
- **One commit for all files.** Regenerating each and committing together means a second scheduled run (or a human push) can't interleave with a half-written batch.

## The regeneration script

The job delegates per-file logic to a script. The non-obvious requirements: **don't clobber the metadata you keep locally**, give **new** rows a sensible default, and **drop** rows that vanished upstream.

Say each row in the file carries an "upgrade-stream" schema you maintain — a pinned tag, a release channel, a policy — that upstream doesn't provide:

```json
[
  { "name": "alpha", "octezTag": "octez-v25.0", "octezChannel": "stable", "octezPolicy": "manual" }
]
```

The script fetches upstream, then merges: keep existing rows verbatim (preserving your pins), add newcomers inheriting defaults from the first existing row, and let disappearances fall off:

```bash
#!/usr/bin/env bash
set -euo pipefail
target_path="${1:?usage: $0 <target_path>}"
url="${TEZTNETS_URL:-https://teztnets.com/teztnets.json}"

# Derive defaults for new rows from the first existing entry's schema.
default_tag=$(jq -r '.[0].octezTag // empty'     "$target_path")
default_channel=$(jq -r '.[0].octezChannel // empty' "$target_path")
default_policy=$(jq -r '.[0].octezPolicy // empty'   "$target_path")

# Build into a temp file and mv into place: the target is BOTH read (for the
# existing schema) and written, and a `> "$target"` redirect would truncate it
# before jq could slurp it. This is the footgun.
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT

curl -fsS "$url" \
  | jq --slurpfile existing "$target_path" \
       --arg dt "$default_tag" --arg dc "$default_channel" --arg dp "$default_policy" \
    '($existing[0] | map({(.name): .}) | add) as $by_name
     | to_entries
     | map(select(.value.aliasOf == null) | .key)
     | map(. as $n | $by_name[$n]
           // {name: $n, octezTag: $dt, octezChannel: $dc, octezPolicy: $dp})' \
  > "$tmp"

mv "$tmp" "$target_path"; trap - EXIT
```

Reading it top to bottom: build a name→row lookup of what's already committed; take upstream's canonical entries (`aliasOf == null`); for each, **reuse the committed row if it exists** (keeping your pins untouched), otherwise **synthesize a new row** on the fleet's default tag/channel/policy. Rows not in upstream simply aren't emitted — they drop out, and that deletion shows up as a reviewable diff on the next run.

That "reuse if present, default if new" merge is why a dependency bot can bump `octezTag` on one row and the next regeneration won't stomp it — the row already exists, so it's preserved verbatim.

## The two footguns

1. **Truncation.** The file is read and written in the same step. `jq … > "$target"` truncates it to empty *before* `jq` opens it via `--slurpfile`, so you lose your existing schema and every new row gets `null` defaults. Build in a temp file, `mv` into place.
2. **New-row defaults must come from somewhere.** If the file is empty, there's no first row to inherit from, and a new network lands with `null` tag/channel/policy. Guard for it: refuse to run (or seed one canonical row by hand) rather than emit half-populated entries.

## When to use which

Materialize-into-git and read-live are two ends of a spectrum:

| | Scheduled regenerate-and-commit | Live read at deploy time |
|---|---|---|
| History / audit | Yes — every change is a commit | No |
| Per-row local metadata (pins, policy) | Preserved through merge | Lost |
| Works with dependency bots | Yes | No |
| Upstream outage at deploy | Uses last-known-good | Fails |
| Freshness | As fresh as the schedule | Instant |
| Moving parts | A scheduled job to own | None |

If the list is disposable and you *want* to track upstream instantly, read it live. If rows carry state you care about — pins you review, policies you enforce, a history you audit — regenerate on a schedule and let the diff speak. The pattern above is about forty lines of YAML and jq, reusable across every such file in the repo.
