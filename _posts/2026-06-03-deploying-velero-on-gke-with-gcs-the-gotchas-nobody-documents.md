---
layout: post
title: "Deploying Velero on GKE with a GCS bucket — the gotchas nobody documents"
date: 2026-06-03 09:00:00 +0000
permalink: /2026/06/deploying-velero-on-gke-with-gcs-the-gotchas-nobody-documents.html
tags: ["velero", "backup", "kubernetes", "gke", "gcp", "gcs", "kopia", "workload-identity", "argocd", "gitops", "sre", "disaster-recovery"]
author: "cm"
---


A practical walk-through of installing Velero on a GKE cluster, backing up to a GCS bucket via Workload Identity, and validating the install end-to-end with three increasingly-rigorous restore tests — including the one you actually run during a real incident. We'll spend most of the time on the parts that the upstream Velero docs hand-wave at: the Workload Identity binding, the silent-failure mode when the binding is wrong, and the subtle race between Velero's restore and GitOps controllers.

I'll use generic names throughout: `my-cluster`, `my-app`, `my-bucket`. The same shape works whether `my-app` is a config-file-on-PVC tool, a database, a queue worker — anything with a PVC you want backed up.

## What we're building

```
+--------------------------+      +----------------------+      +------------------------+
| my-app PVCs              | <--- | Velero + node-agent  | ---> | GCS bucket             |
| (any storage class with  |      | (Kopia fs-backup)    |      | my-bucket              |
|  RWO PVCs)               |      |                      |      |                        |
+--------------------------+      +----------------------+      +------------------------+
                                            ^
                                            | impersonates GSA via WI
                                  +-------------------------+
                                  | KSA velero/velero-server|
                                  +-------------------------+
                                            ^
                                            | iam.workloadIdentityUser
                                  +-------------------------+
                                  | GSA velero-prd@…        |
                                  +-------------------------+
                                            ^
                                            | roles/storage.objectAdmin
                                            v
                                       gs://my-bucket
```

Three identities tie everything together:

1. **Google Service Account (GSA)** — `velero-prd@<project>.iam.gserviceaccount.com`. The identity Velero impersonates when talking to GCS.
2. **Kubernetes Service Account (KSA)** — `velero/velero-server`. The pod identity. Annotated with `iam.gke.io/gcp-service-account: velero-prd@…`.
3. **Workload Identity binding** — gives the KSA permission to impersonate the GSA.

If any one of these is wrong, Velero appears to install fine, the Schedule fires, the Backup CR is created — and then **silently transitions to `FailedValidation` because the GCS auth token can't be fetched**. The user sees a healthy-looking `kubectl get all` and never realises backups aren't happening. We had this state in production for 46 days before noticing.

This post walks through the install in a way that surfaces those failures early.

## Prerequisites

- A GKE cluster with **Workload Identity enabled at the cluster level AND on every node pool** (`workloadPool: <project>.svc.id.goog` on the cluster; `--workload-metadata=GKE_METADATA` on each node pool). Velero docs assume this but don't reiterate it.
- `gcloud` authenticated to the GCP project that owns the cluster, with project-level IAM admin (you'll create a GSA, a bucket, and IAM bindings).
- `kubectl` configured for the target cluster.
- `helm` installed locally.
- A namespace `velero` you're willing to dedicate (the install creates it via Argo or `kubectl create ns velero`).

## Step 1 — Create the GSA

```bash
PROJECT=my-project-id
gcloud iam service-accounts create velero-prd \
  --display-name="Velero prd service account" \
  --project="$PROJECT"
```

Verify:

```bash
gcloud iam service-accounts describe \
  velero-prd@$PROJECT.iam.gserviceaccount.com --project=$PROJECT
```

If this returns `NOT_FOUND` later, your install will fail silently — Velero will impersonate a GSA that doesn't exist, the metadata server will reply `"not defined"`, and every Backup will be `FailedValidation`. **This is the #1 silent-failure cause; if you skip the verify, you'll spend hours debugging.**

## Step 2 — Create the GCS bucket

```bash
gcloud storage buckets create gs://my-bucket \
  --location=EU \
  --uniform-bucket-level-access \
  --project="$PROJECT"
```

Use `EU` / `US` / `ASIA` multi-region for high-availability backups, or a single-region location to minimise egress costs if the cluster is in the same region. Storage class defaults to `STANDARD`; switch to `NEARLINE` or `COLDLINE` only if you're sure backups are read rarely (restores will be slower and pricier).

Verify the bucket actually exists:

```bash
gcloud storage buckets describe gs://my-bucket \
  --format='value(name,location,storageClass)'
```

Yes, you do need this verification step. We've seen "the values file references a bucket that was never created" as a production bug.

## Step 3 — Grant the GSA permission to write to the bucket

```bash
gcloud storage buckets add-iam-policy-binding gs://my-bucket \
  --member="serviceAccount:velero-prd@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/storage.objectAdmin
```

`storage.objectAdmin` is the minimum role Velero needs — it can read, write, and delete objects in the bucket. If you're paranoid, you can tighten to `roles/storage.objectUser` (read/write but no delete), but then you lose the ability for Velero to GC expired Backups, and your bucket will grow forever.

## Step 4 — Bind the KSA to the GSA via Workload Identity

This is the step that makes the in-pod metadata token fetch work.

```bash
gcloud iam service-accounts add-iam-policy-binding \
  velero-prd@$PROJECT.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:$PROJECT.svc.id.goog[velero/velero-server]" \
  --project="$PROJECT"
```

Note the member format: `serviceAccount:<workloadIdentityPool>[<ns>/<ksa>]`. The pool is `<project>.svc.id.goog`. The KSA name `velero-server` is what Velero's Helm chart creates by default. If you customise `serviceAccount.server.name` in the chart values, adjust here too.

Verify:

```bash
gcloud iam service-accounts get-iam-policy \
  velero-prd@$PROJECT.iam.gserviceaccount.com --project=$PROJECT \
  | grep -A1 workloadIdentityUser
```

You want to see `serviceAccount:<project>.svc.id.goog[velero/velero-server]` in the output.

## Step 5 — Install Velero via Helm

Bare-bones values file:

```yaml
# values-velero.yaml
initContainers:
  - name: velero-plugin-for-gcp
    image: velero/velero-plugin-for-gcp:v1.14.0
    volumeMounts:
      - mountPath: /target
        name: plugins

configuration:
  backupStorageLocation:
    - name: default
      provider: gcp
      bucket: my-bucket
      config:
        serviceAccount: velero-prd@my-project-id.iam.gserviceaccount.com
  volumeSnapshotLocation:
    - name: default
      provider: gcp

# No K8s secret — Workload Identity provides auth via the GSA
credentials:
  useSecret: false

serviceAccount:
  server:
    annotations:
      iam.gke.io/gcp-service-account: velero-prd@my-project-id.iam.gserviceaccount.com

# Deploy the node-agent DaemonSet (Kopia) so we can do file-system backup of PVCs.
# Required for any storage class without CSI snapshot support.
deployNodeAgent: true

schedules:
  my-app:
    schedule: "0 * * * *"
    template:
      includedNamespaces:
        - my-app
      defaultVolumesToFsBackup: true
      ttl: "720h"        # 30 days
```

That single hourly schedule with a 30-day TTL gives you 720 hourly snapshots in the bucket at steady state. If you want **GFS-style tiered retention** — fewer, longer-lived snapshots at coarser cadences — keep reading. If a flat 30-day window is fine, skip to "Install".

### Optional: GFS-tiered retention via multiple schedules

The simplest way to get hourly+daily+weekly+monthly retention without writing a custom controller is to define **four schedules**, each with its own cron + TTL. Velero doesn't natively promote one Backup across tiers, so each tier produces its own Backup CR. Kopia content-addresses the data, so the bucket cost stays roughly 1× the actual content even when multiple schedules fire at the same minute.

**Stagger the crons so no two schedules ever fire concurrently** — otherwise tier-boundary moments (Sunday 1st-of-month at 00:00) trigger 4 parallel backups, hammer your node-agent, and (later, if you add pre-backup quiesce hooks) become 4 sequential stop/start cycles on the workload. A 15-minute offset between tiers is enough:

```yaml
schedules:
  my-app-hourly:
    schedule: "0 * * * *"            # every hour at :00
    template:
      includedNamespaces: [my-app]
      defaultVolumesToFsBackup: true
      ttl: 24h
  my-app-daily:
    schedule: "15 2 * * *"           # 02:15 UTC — 15 min after the hourly
    template:
      includedNamespaces: [my-app]
      defaultVolumesToFsBackup: true
      ttl: 168h                       # 7 days
  my-app-weekly:
    schedule: "30 3 * * 0"           # Sun 03:30 UTC
    template:
      includedNamespaces: [my-app]
      defaultVolumesToFsBackup: true
      ttl: 720h                       # 30 days
  my-app-monthly:
    schedule: "45 4 1 * *"           # 1st 04:45 UTC
    template:
      includedNamespaces: [my-app]
      defaultVolumesToFsBackup: true
      ttl: 8760h                      # 365 days
```

Steady-state retention: ~24 hourly + 7 daily + 4 weekly + 12 monthly ≈ 47 Backup CRs per target.

The **schedule-name suffix convention** (`-hourly` / `-daily` / `-weekly` / `-monthly`) matters when you wire up alerting (see the "Alerting" section near the end) — a single rule file can target each tier by regex on the schedule name, so you get different staleness thresholds for free.

What about **true GFS promotion** (the same Backup tagged as both "today's hourly" and "today's daily")? Velero doesn't natively support that. You can build a custom controller that extends `spec.ttl` on the earliest Backup of each calendar period — we designed one and have working code for it — but adding a self-coded shell script to the backup path is a maintainability trade-off you should make consciously. The four-schedule approach above is the native pattern; the custom-promoter alternative is a separate post-worth discussion.

Install:

```bash
helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts
helm install velero vmware-tanzu/velero \
  --version 12.0.1 \
  --namespace velero --create-namespace \
  -f values-velero.yaml
```

## Step 6 — Verify the install **the right way**

Most "did Velero install correctly?" checks I see online check the wrong thing. They check: are the pods running? are the CRDs there? are the schedules created? — **all of which can be true while every Backup silently fails**.

The correct check is the **BackupStorageLocation phase**. Run:

```bash
kubectl -n velero get backupstoragelocations.velero.io
```

You want `PHASE: Available` and `LAST VALIDATED` within the last ~60 seconds. If it says `Unavailable`, read the message:

```bash
kubectl -n velero get bsl default -o jsonpath='{.status.message}{"\n"}'
```

Common messages:
- `cannot fetch token: metadata: GCE metadata "instance/service-accounts/default/token..." not defined` → the WI binding is missing or wrong. Re-check Step 4.
- `NotFound: Unknown service account` → the GSA doesn't exist (your `--member` in Step 4 referenced a typo'd name). Re-check Step 1.
- `Bucket nl-...-velero-backup-... not found: 404` → the bucket doesn't exist or the GSA lacks read access. Re-check Steps 2 and 3.
- `Forbidden` → the GSA exists but the bucket IAM doesn't grant it write. Re-check Step 3.

When `PHASE: Available`, your install is correct and Backups will land successfully.

## Step 7 — Smoke-test by triggering a one-off Backup

```bash
cat <<EOF | kubectl apply -f -
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: my-app-smoke
  namespace: velero
spec:
  includedNamespaces:
    - my-app
  defaultVolumesToFsBackup: true
  ttl: 24h
EOF
```

Watch:

```bash
kubectl -n velero get backup.velero.io my-app-smoke \
  -o custom-columns=PHASE:.status.phase,ITEMS:.status.progress.itemsBackedUp,ERRORS:.status.errors
```

You want `PHASE: Completed`, `ERRORS: <none>`. If `PHASE: FailedValidation`, go back to Step 6 — the BSL is what's broken.

A successful Backup writes:

```
gs://my-bucket/
└── backups/my-app-smoke/
    ├── my-app-smoke.tar.gz
    ├── my-app-smoke-podvolumebackups.json.gz
    └── …
└── kopia/
    └── <kopia repo blobs>
```

You can verify in the GCS console or with `gcloud storage ls gs://my-bucket/`.

## Three tests, in increasing order of rigour

A Backup that exists isn't proof you can actually restore from it. There are three ways to validate restorability, in increasing order of operational rigour and decreasing order of frequency:

1. **Shadow-namespace test** — restore the Backup into a sibling namespace, hash-compare a known file, cleanup. Non-destructive. Recommended monthly. ~5 min.
2. **Destructive in-place test** — suspend GitOps, delete the live PVC + Deployment, restore in place, verify, resume. Destructive. Recommended after install + after cluster upgrades. ~10 min, of which the target is offline for ~3.
3. **Manual recovery dry-run** — simulate "the PVC content is wrong" and recover via shadow restore + `kubectl cp` into the live pod. The everyday-recovery pattern. Non-destructive. ~3 min.

Each test is a runbook in its own right; we'll walk through all three.

## Test 1 — Shadow-namespace restore

This is the routine "is my backup pipeline healthy?" check.

```bash
BACKUP=my-app-smoke   # or pick the latest scheduled Backup

cat <<EOF | kubectl apply -f -
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: my-app-shadow-drill-$(date +%s)
  namespace: velero
spec:
  backupName: $BACKUP
  includedNamespaces:
    - my-app
  namespaceMapping:
    my-app: my-app-restore-drill
EOF
```

Wait for it:

```bash
RESTORE=$(kubectl -n velero get restore.velero.io --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
for i in $(seq 1 18); do
  PHASE=$(kubectl -n velero get restore.velero.io "$RESTORE" -o jsonpath='{.status.phase}')
  echo "t=$((i*10))s phase=$PHASE"
  [ "$PHASE" = Completed ] && break
  sleep 10
done
```

In the shadow namespace (`my-app-restore-drill`) you'll have:
- A new PVC with the same contents the original had at Backup time
- A new Pod that mounts it (likely `0/1 Ready` because it lacks prod credentials — that's fine; we only care about the PVC data)
- The full Deployment and Service spec, restored verbatim

Hash a file you know:

```bash
POD=$(kubectl -n my-app-restore-drill get pod -o jsonpath='{.items[0].metadata.name}')
kubectl -n my-app-restore-drill exec "$POD" -- sha256sum /data/my-state-file
```

Compare with the same hash in the live namespace. **Byte-identical = your backup pipeline is healthy end-to-end.**

Cleanup:

```bash
kubectl delete ns my-app-restore-drill --wait=false
kubectl -n velero delete restore.velero.io "$RESTORE"
```

The Backup itself is untouched; you can shadow-restore it again any time.

## Test 2 — Destructive in-place test, and why it's harder than it looks

The shadow-namespace test proves data integrity but not the operational playbook. To validate "if production loses its PVC, can we restore in place?", you need to:

1. Suspend GitOps reconciliation on the target's Application (otherwise it'll fight you)
2. Delete the live Deployment and PVC
3. Trigger Velero restore
4. Verify the restored pod has the data
5. Resume GitOps

Steps 2-5 are mechanical. Step 1 is the trap.

### Why step 1 traps you

If your Velero target's Argo Application is generated by an `ApplicationSet`, **patching `spec.syncPolicy.automated: null` on the Application alone does not stick**. The ApplicationSet controller reverts it within seconds — it owns the Application and its job is to make the live spec match its template. Argo's `selfHeal` then races Velero: it recreates the Deployment + (empty) PVC before Velero can inject a `PodVolumeRestore` init container into a pod **it** controls, and the restored data is lost.

We discovered this the hard way on a production target. The drill "succeeded" — `velero get restore` showed `Completed`, the new PVC bound, the pod was Running 1/1 — but `/data/<state-file>` was empty. The Velero restore had created a new PVC, but the Deployment-spawned pod (Argo-controlled, not Velero-controlled) mounted the PVC without ever running the `PodVolumeRestore` init container that does the actual data copy from Kopia.

### The fix — verified empirically over three attempts

I had to actually run this in production to discover what works. The empirical record:

| Attempt | Suspend method | What was reverted | Result |
|---|---|---|---|
| 1 | Patch `Application.spec.syncPolicy.automated: null` | ApplicationSet controller put `automated` back in <5s | PVC empty after restore. Recovered via Test 3. |
| 2 | Patch `ApplicationSet.spec.template.spec.syncPolicy.automated: null` | Upstream "app-of-apps" Argo Application put it back in <60s | PVC empty after restore. Recovered via Test 3. |
| 3 | **AppProject Sync Window** (`kind: deny`, 30 min, namespaces: [my-app]) | **Not reverted within the 3-min drill window** | **Restored PVC byte-identical to pre-drill (`sha256` matched)** |

Lesson: on a fully GitOps-managed cluster where the AppProject is also GitOps-managed, **any short-window mechanism that holds for ~3 minutes is enough** for a typical restore (small PVCs complete in ~80s). The AppProject Sync Window meets that bar without requiring a git PR.

### How to do it

**Sync Window on the AppProject** (the verified-working approach):

```bash
PROJECT=$(kubectl -n argocd get application my-app -o jsonpath='{.spec.project}')

kubectl -n argocd patch appproject "$PROJECT" --type=merge -p='{
  "spec": {
    "syncWindows": [{
      "kind": "deny",
      "schedule": "* * * * *",
      "duration": "30m",
      "namespaces": ["my-app"],
      "manualSync": true,
      "timeZone": "UTC"
    }]
  }
}'
```

Probe that the window is effective before destruction:

```bash
kubectl -n my-app delete deployment my-app --wait=false
sleep 20
kubectl -n my-app get deployment my-app   # should be NotFound; if it's back, abort
```

Remove the window at the end:

```bash
kubectl -n argocd patch appproject "$PROJECT" --type=json \
  -p='[{"op":"remove","path":"/spec/syncWindows/0"}]'
```

⚠ Even the AppProject patch may eventually revert if the AppProject itself is GitOps-managed by an upstream "app-of-apps" Argo Application. The window typically lasts 5-10 minutes — enough for the drill — but watch for revert. The fully-bulletproof variant is to edit the AppProject's git source values and merge, which takes longer.

**Other approaches that don't work (don't waste time on them):**

- Patching `Application.spec.syncPolicy.automated: null` directly — the ApplicationSet owns the Application's spec and reverts this in seconds.
- Patching the ApplicationSet template — the upstream "app-of-apps" Argo Application reverts this in under a minute.

The only at-runtime suspend that actually held on the cluster I tested was the AppProject Sync Window. The reason: each level of the GitOps tree reverts the level below. Sync Windows live on the AppProject and override individual Application sync policies; the AppProject is reconciled less aggressively than Application syncPolicy fields, which gives you enough time to do a quick drill.

### The actual destructive-test commands

After the Sync Window is in place and the probe-delete confirms Argo is no longer auto-syncing:

```bash
BACKUP=$(kubectl -n velero get backup.velero.io --sort-by=.metadata.creationTimestamp -o json \
  | jq -r '[.items[] | select(.metadata.name | startswith("velero-my-app-")) | select(.status.phase=="Completed") | .metadata.name] | last')

# Save the pre-drill hash for comparison
LIVE_POD=$(kubectl -n my-app get pod -l app.kubernetes.io/name=my-app --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
kubectl -n my-app exec "$LIVE_POD" -- sha256sum /data/my-state-file

# Destroy
kubectl -n my-app delete deployment my-app --wait=true --timeout=60s
kubectl -n my-app delete pvc my-app --wait=false

# Restore
cat <<EOF | kubectl apply -f -
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: my-app-destructive-$(date +%s)
  namespace: velero
spec:
  backupName: $BACKUP
  includedNamespaces:
    - my-app
EOF

# Wait for Completed (typically 60-120s for small PVCs)
RESTORE=$(kubectl -n velero get restore.velero.io --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
until kubectl -n velero get restore.velero.io "$RESTORE" -o jsonpath='{.status.phase}' | grep -qE 'Completed|Failed'; do sleep 5; done

# Verify
kubectl -n velero get podvolumerestore.velero.io   # must show Completed PVRs with bytes>0
NEW_POD=$(kubectl -n my-app get pod -l app.kubernetes.io/name=my-app -o jsonpath='{.items[0].metadata.name}')
kubectl -n my-app get pod "$NEW_POD" -o jsonpath='{range .spec.initContainers[*]}{.name} {end}'   # must include "restore-wait"
kubectl -n my-app exec "$NEW_POD" -- sha256sum /data/my-state-file   # must match pre-drill hash

# Remove the Sync Window when drill is complete:
kubectl -n argocd patch appproject "$PROJECT" --type=json -p='[{"op":"remove","path":"/spec/syncWindows/0"}]'
```

If `podvolumerestore` count is zero or the pod has no `restore-wait` init container, **Argo won the race** — the new PVC will be empty. Recover via Test 3 (manual fix) below. Don't leave production with an empty PVC.

## Test 3 — Manual fix in case of effective data loss

This is the **everyday recovery pattern**. Whenever production data goes bad — operator mistake, drift between expected and actual state, or even the destructive test from above hitting a race — this is how you recover. It doesn't fight Argo, doesn't require any GitOps suspension, and works on any cluster.

```bash
# 1. Pick the Backup to restore from (most recent Completed predating the corruption)
kubectl -n velero get backup.velero.io --sort-by=.metadata.creationTimestamp \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,CREATED:.metadata.creationTimestamp \
  | grep my-app | grep Completed | tail -10

BACKUP=velero-my-app-20260603200057   # adjust to your choice

# 2. Restore into a shadow namespace (production untouched)
cat <<EOF | kubectl apply -f -
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: my-app-recovery-$(date +%s)
  namespace: velero
spec:
  backupName: $BACKUP
  includedNamespaces: [my-app]
  namespaceMapping:
    my-app: my-app-recovery-shadow
EOF

# 3. Wait for restore Completed (Kopia data pulls in)
RESTORE=$(kubectl -n velero get restore.velero.io --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')
until kubectl -n velero get restore.velero.io "$RESTORE" -o jsonpath='{.status.phase}' | grep -qE 'Completed|Failed'; do sleep 5; done

# 4. Copy the file from the shadow pod into the live pod
SHADOW_POD=$(kubectl -n my-app-recovery-shadow get pod -o jsonpath='{.items[0].metadata.name}')
LIVE_POD=$(kubectl -n my-app get pod -l app.kubernetes.io/name=my-app --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')

kubectl -n my-app-recovery-shadow exec "$SHADOW_POD" -- cat /data/my-state-file > /tmp/recovered
sha256sum /tmp/recovered                                                # sanity check the local copy
kubectl -n my-app cp /tmp/recovered "$LIVE_POD":/data/my-state-file
kubectl -n my-app exec "$LIVE_POD" -- sha256sum /data/my-state-file     # must match what's in shadow

# 5. Restart the live pod if your app reads the file only at startup
kubectl -n my-app delete pod "$LIVE_POD"   # Deployment respawns; same PVC, restored file
kubectl -n my-app rollout status deployment/my-app

# 6. Cleanup
rm -f /tmp/recovered
kubectl delete ns my-app-recovery-shadow --wait=false
kubectl -n velero delete restore.velero.io "$RESTORE"
```

The Backup itself is untouched — you can use it again for further investigation if needed.

For copying a whole directory rather than a single file, replace step 4 with a `tar` stream:

```bash
kubectl -n my-app-recovery-shadow exec "$SHADOW_POD" -- tar -cf - -C / data \
  | kubectl -n my-app exec -i "$LIVE_POD" -- tar -xf - -C /
```

`kubectl cp` uses `tar` under the hood and requires `tar` in the container; if your image is distroless and lacks `tar`, use the explicit `exec ... cat | exec -i ... cat > target` variant.

## Alerting: catch the 46-day silent failure mode in 1 hour instead

The Workload-Identity silent-failure mode at the start of this post is genuinely dangerous: Velero pods Running, Schedules Enabled, `kubectl get all` looks healthy, every Backup is `FailedValidation`. You can sit in that state for **weeks**. The fix is monitoring on metrics Velero already exposes for free.

Velero's controller pod serves `/metrics` on port `8085` with these alert-grade series (no PushGateway needed, no Alloy CI integration, just scrape):

| Metric | What it tells you |
|---|---|
| `velero_backup_last_successful_timestamp{schedule=...}` | the heartbeat — last successful backup time per schedule |
| `velero_backup_validation_failure_total{schedule=...}` | **the silent-failure detector** — increments any time a Backup gets `FailedValidation`, which is what happens when the BSL is `Unavailable` |
| `velero_backup_failure_total{schedule=...}` | hard failures (BSL was OK at validation time, but the actual backup work errored) |
| `velero_backup_partial_failure_total{schedule=...}` | partial failures |
| `velero_backup_attempt_total{schedule=...}` | did the schedule actually fire? |

To wire this up:

1. **Enable the ServiceMonitor** in your Velero Helm values:
   ```yaml
   metrics:
     serviceMonitor:
       enabled: true
       autodetect: false   # render unconditionally, not only when CRD is visible at template time
       additionalLabels:
         release: prometheus-stack   # matches kube-prometheus-stack's serviceMonitorSelector
   ```
2. **Add Prometheus alerting rules**. Five generic alerts cover the whole surface — one for failures, four for staleness (tiered by schedule-name suffix so each cadence gets the right threshold):

```yaml
groups:
  - name: velero
    rules:
      - alert: VeleroBackupFailing
        expr: |
          (increase(velero_backup_validation_failure_total{schedule!=""}[1h]) > 0)
          or
          (increase(velero_backup_failure_total{schedule!=""}[1h]) > 0)
        labels: { severity: warning, team: infra }
        annotations:
          summary: "Velero schedule {{ $labels.schedule }} is producing failed Backups"
          description: "Validation failures usually mean BackupStorageLocation is Unavailable; hard failures usually mean node-agent / Kopia trouble."

      - alert: VeleroHourlyBackupStale
        expr: |
          time() - max by (schedule) (
            velero_backup_last_successful_timestamp{schedule!~".*-(daily|weekly|monthly)$",schedule!=""} > 0
          ) > 9000
        labels: { severity: warning, team: infra }
        annotations:
          summary: "Velero hourly schedule {{ $labels.schedule }} has not succeeded in 2.5h+"
          description: "Last successful backup was {{ $value | humanizeDuration }} ago."

      - alert: VeleroDailyBackupStale
        expr: |
          time() - max by (schedule) (velero_backup_last_successful_timestamp{schedule=~".*-daily$"} > 0) > 90000
        labels: { severity: warning, team: infra }
        annotations:
          summary: "Velero daily schedule {{ $labels.schedule }} has not succeeded in 25h+"
          description: "Last successful backup was {{ $value | humanizeDuration }} ago."

      - alert: VeleroWeeklyBackupStale
        expr: |
          time() - max by (schedule) (velero_backup_last_successful_timestamp{schedule=~".*-weekly$"} > 0) > 691200
        labels: { severity: warning, team: infra }
        annotations:
          summary: "Velero weekly schedule {{ $labels.schedule }} has not succeeded in 8d+"
          description: "Last successful backup was {{ $value | humanizeDuration }} ago."

      - alert: VeleroMonthlyBackupStale
        expr: |
          time() - max by (schedule) (velero_backup_last_successful_timestamp{schedule=~".*-monthly$"} > 0) > 2764800
        labels: { severity: warning, team: infra }
        annotations:
          summary: "Velero monthly schedule {{ $labels.schedule }} has not succeeded in 32d+"
          description: "Last successful backup was {{ $value | humanizeDuration }} ago."
```

The schedule-name suffix convention (`-hourly` / `-daily` / `-weekly` / `-monthly`) from the GFS section earlier is what lets a single rule file give each tier the right staleness threshold:

| Schedule suffix (or none) | Threshold | Maps to |
|---|---|---|
| (no suffix, e.g. `velero-my-app`) | 2.5h | hourly bucket via negated regex |
| `-hourly` | 2.5h | hourly |
| `-daily` | 25h | daily |
| `-weekly` | 8d | weekly |
| `-monthly` | 32d | monthly |

**Validate before merging:**

```bash
# promtool via docker (no local Prometheus needed):
docker run --rm -v "$PWD/rules:/rules" --entrypoint promtool \
  prom/prometheus check rules /rules/velero-rules.yaml
# Expected: "SUCCESS: 5 rules found"
```

**Synthetic test post-deploy:** pause one of your Schedules (`kubectl patch schedule.velero.io my-app-hourly --type=merge -p '{"spec":{"paused":true}}'`) and wait for the staleness threshold to elapse. The corresponding alert should fire. Un-pause to clear. This proves both that the metric flows end-to-end (Velero → Prometheus → Thanos → Ruler → Alertmanager → wherever your team-label routes) and that the rule expression matches.

With this in place, the 46-day silent-failure scenario at the top of this post becomes a **1-hour** detection — `VeleroBackupFailing` fires the first time the BSL fails to validate.

## What stays the same regardless of GitOps tooling

Three things you should always do in this order during any data-loss incident:

1. **Confirm the Backup exists and is good.** `kubectl -n velero get backup.velero.io` should show a `Completed` Backup predating the loss. If not, you have a backup-pipeline problem, not a recovery problem.
2. **Restore to a shadow namespace first.** Verify hash. This is your sanity check that the Backup actually contains the data you remember.
3. **Then `kubectl cp` into the live pod.** No Argo wrestling. No GitOps suspension. Whatever your cluster's GitOps tree looks like, this works.

## Recap — the five things that go wrong (with confidence rankings)

| Probability | Failure | Symptom | Where to check |
|---|---|---|---|
| Very high | GSA referenced in values file doesn't exist | Every Backup is `FailedValidation`. Velero pods look healthy. | `gcloud iam service-accounts describe <gsa>` |
| Very high | GCS bucket referenced in values file doesn't exist | Every Backup is `FailedValidation`. Velero pods look healthy. | `gcloud storage buckets describe gs://<bucket>` |
| High | WI binding (`roles/iam.workloadIdentityUser`) missing between GSA and KSA | BSL `Unavailable` with `"GCE metadata ... not defined"` | `gcloud iam service-accounts get-iam-policy <gsa> \| grep workloadIdentityUser` |
| Medium | GKE node pool lacks `--workload-metadata=GKE_METADATA` | Same as above — metadata token endpoint returns 404 | `gcloud container node-pools describe <pool> --cluster <cluster> --region <r>` |
| Subtle but high if you destructive-drill on Argo apps | Argo's selfHeal races Velero and recreates the (empty) PVC before Velero can inject `restore-wait` into a Velero-controlled pod | Restore "Completed", no PVRs fired, PVC is empty | Use AppProject Sync Window per Test 2; if you missed it and the drill failed, recover via Test 3 |

## What the docs don't tell you (but should)

- Velero will install happily with a misconfigured BSL. The pods will be `Running` and the Schedules will fire. **The only signal is `BackupStorageLocation.status.phase`**. Make this part of your install acceptance test.
- `helm install` doesn't fail if the GSA / bucket doesn't exist — those are runtime checks the Velero controller does, not install-time validation Helm does.
- `kubectl get backup -A` returns "No resources found" on some clusters because `backup` is a short name that resolves to a different CRD (Rancher's `backups.resources.cattle.io`, for instance). Use the fully-qualified `backups.velero.io`.
- The Helm chart's default `serviceAccount.server.name` is `velero-server`. If you change it, your Workload Identity binding (Step 4) needs to reference the new name. We've seen people stuck for hours on a mismatched name where the chart default changed in a minor version.

Velero is a great tool. Most of the install friction comes from the Workload Identity setup being spread across three or four GCP commands that have to align exactly. Get those right, verify with the BSL phase, and run a shadow-restore test monthly. That's all most teams need.
