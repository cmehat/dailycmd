---
layout: post
title: "Snapshots before scary changes: a $1.50 safety net and three sharp edges"
date: 2026-06-12 18:00:00 +0000
permalink: /2026/06/snapshots-before-scary-changes-the-cheap-insurance-policy.html
tags: ["gcp", "terraform", "infrastructure", "backups", "snapshots", "operations", "sre", "incident-review"]
author: "cm"
draft: true
---

A short ops post about a habit I keep relearning: when you're about to apply an infrastructure change and you're not 100% sure what it will do, **take a disk snapshot first**. The cost is rounding error. The recovery procedure should be a written-down checklist, not improvised under stress. And there are at least three sharp edges between "I took a snapshot, what could go wrong" and "I successfully restored from it."

Generic context throughout: a fleet of cloud VMs running a long-lived stateful application. Each VM has a boot disk and a separate attached disk that holds the application's state directory at `/opt/app`. The application is provisioned via Terraform using a third-party module. None of the specifics matter for the lessons.

## TL;DR

1. A pre-apply snapshot of an attached disk in `europe-west1` costs ~$0.05–$1 per host for a few hours of retention. For a fleet, **single-digit dollars total**. Snapshot pricing is per used-block-gigabyte-month, not per provisioned-gigabyte-month — `df -B1G /opt` is a decent proxy for billable size. There is no creation fee.
2. The terraform-side flow is mechanical and worth writing down once: `gcloud compute disks snapshot` → `terraform apply` → verify `/opt` survived → if not, `stop` + `detach` + `delete` + `create-from-snapshot` (matching the original disk **type**) + `attach` + `start` → cleanup snapshot after a grace period.
3. **Three sharp edges**, none obvious from the docs:
   - On VM recreate, a careless first-boot script can `mkfs.ext4` the attached disk before fstab is wired up. Existing data: gone.
   - `lifecycle { ignore_changes = [metadata_startup_script] }` means a fixed boot script never reaches the VMs it's supposed to protect — `gcloud compute instances add-metadata` is the manual escape hatch.
   - **`gcloud compute instances reset` corrupts the application's local state.** It's a hard power-cycle. Use `stop` + `start` (or `systemctl reboot` from inside the VM). I learned this by losing a testnet node and re-importing from a snapshot URL while my Slack lit up.

## The setup

```
your-project/europe-west1-c/
├── vm-host-a       (instance)
│   ├── boot-disk-a    (50 GB, pd-ssd, OS only)
│   └── disk-data-a    (500 GB, pd-ssd, mounted at /opt/app)
├── vm-host-b
│   ├── boot-disk-b
│   └── disk-data-b
└── … (similar for each host)
```

Each VM runs a stateful application whose data directory lives under `/opt/app`. Lose `/opt/app` and you have to re-sync from a known-good snapshot URL or backup, which for some of these hosts takes hours or days. The boot disk is recreatable from the OS image at any time.

The terraform module provisions the instance with `allow_stopping_for_update = true`, which means `machine_type` changes happen **in-place** (stop → setMachineType → start), not as a destroy+recreate. The attached disk is referenced via `lifecycle { ignore_changes = [attached_disk] }` so terraform doesn't try to reconcile its size/labels every time.

What I was about to do: bump `machine_type` across the fleet (some hosts were under-provisioned per the application's published hardware specs), plus bump the third-party terraform module version (to pull in a startup-script fix — more on that below). Both changes felt routine. Both could plausibly do something unexpected. Cost of insurance was nothing. So: snapshots.

## The procedure

The five steps, generalized. Replace `<HOST>` and pick a tag for `<change>` (e.g. `mtype-bump`, `module-2.9.0`).

### 1. Pre-apply snapshot

```bash
PROJECT=your-project
ZONE=europe-west1-c
HOST=vm-host-a

# Find the original disk type FIRST (you'll need it to recreate)
DISK_TYPE=$(gcloud compute disks describe disk-data-${HOST} \
  --zone="$ZONE" --project="$PROJECT" \
  --format='value(type.basename())')
echo "Original disk type: $DISK_TYPE"

# Snapshot the attached disk
gcloud compute disks snapshot disk-data-${HOST} \
  --snapshot-names=pre-mtype-bump-${HOST}-$(date +%Y%m%d-%H%M) \
  --zone="$ZONE" --project="$PROJECT"
```

Snapshots are incremental once you have a baseline, but the first one of a disk is full-sized (relative to used blocks). For a fleet that's never been snapshotted, expect to pay the full per-host price the first time. Subsequent snapshots are delta-only.

### 2. Apply the change

```bash
terraform apply -target='module.vigie_vm_host_a'   # narrow blast radius
# or just `terraform apply` for the fleet
```

### 3. Verify `/opt` survived

```bash
ssh app@vm-host-a.example.com '
  echo "=== boot ==="; uptime -s
  echo "=== was the disk reformatted in this boot? (expect nothing) ==="
  sudo journalctl -b -u google-startup-scripts.service | \
    grep -E "Creating filesystem|Filesystem UUID" || echo "OK: no mkfs"
  echo "=== /opt content age (expect: predates the apply) ==="
  sudo find /opt -maxdepth 2 -printf "%TY-%Tm-%Td %p\n" | sort | head -3
'
```

If the `find` output shows directories predating the apply, you're done; jump to step 5.

### 4. Restore from snapshot (only if `/opt` was wiped or corrupted)

```bash
# Pick the snapshot you just took
SNAP=$(gcloud compute snapshots list \
  --filter="name~^pre-mtype-bump-${HOST}-" --format='value(name)' \
  --project="$PROJECT" | sort | tail -1)

# 1. Stop the VM — NOT `reset` — sends ACPI shutdown so the application's
#    in-memory state flushes to disk cleanly.
gcloud compute instances stop "$HOST" --zone="$ZONE" --project="$PROJECT"

# 2. Detach the wiped/corrupted disk
gcloud compute instances detach-disk "$HOST" \
  --disk=disk-data-${HOST} \
  --zone="$ZONE" --project="$PROJECT"

# 3. Delete it (the snapshot is the source of truth now)
gcloud compute disks delete disk-data-${HOST} \
  --zone="$ZONE" --project="$PROJECT" --quiet

# 4. Recreate from snapshot — same name, same TYPE as the original
gcloud compute disks create disk-data-${HOST} \
  --source-snapshot="$SNAP" \
  --type="$DISK_TYPE" \
  --zone="$ZONE" --project="$PROJECT"

# 5. Re-attach with the device-name expected by fstab
gcloud compute instances attach-disk "$HOST" \
  --disk=disk-data-${HOST} \
  --device-name=opt \
  --zone="$ZONE" --project="$PROJECT"

# 6. Start the VM
gcloud compute instances start "$HOST" --zone="$ZONE" --project="$PROJECT"
```

### 5. Cleanup snapshot after a grace period

```bash
gcloud compute snapshots delete "$SNAP" --project="$PROJECT" --quiet
```

I usually leave the snapshot in place for ~24 h. Costs pennies and gives me a window to discover problems that take a day to surface (peers re-sync slowly, a logfile fills, etc.).

## Sharp edge #1 — the wipe-on-recreate trap

The third-party terraform module I was using had this startup script burned into the VM's metadata:

```bash
function setup_attached_disk () {
  disk_dev=$(realpath "/dev/disk/by-id/google-${disk_name}")
  if [ -b "${disk_dev}" ]; then
    echo "INFO: Found attached disk, block device ${disk_dev}"
  else
    errcho "ERROR: Disk not found"
  fi

  # Format and mount disk
  mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "${disk_dev}" \
    || errcho "ERROR formating ${disk_name}"
  mkdir -p "${mount_path}"
  mount "${disk_dev}" "${mount_path}" || errcho "ERROR mounting"

  # Write fstab entry…
}
```

On a **truly first boot** with an empty disk, this is correct. But notice the absence of any guard: no `mountpoint -q`, no `blkid` check. If for any reason `/opt` isn't mounted via `fstab` by the time `google-startup-scripts.service` runs (and the script writes the fstab entry itself, so on a fresh boot of a new VM there is no fstab entry yet), the script formats the disk. If the disk has existing data — say, because the VM was destroyed and recreated while the attached disk persisted — that data is gone, with no warning.

The fix is mechanical: two guards before `mkfs.ext4`.

```bash
if mountpoint -q "${mount_path}"; then
  echo "INFO: ${mount_path} already mounted, skipping format + fstab update"
  return 0
fi
if blkid "${disk_dev}" >/dev/null 2>&1; then
  echo "INFO: existing filesystem on ${disk_dev}, mounting without reformatting"
  mkdir -p "${mount_path}"
  mount "${disk_dev}" "${mount_path}" || errcho "ERROR mounting existing fs"
  return 0
fi

# First-boot path: empty disk, both guards fall through, mkfs runs
mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "${disk_dev}" || …
```

I pushed the fix upstream as a one-commit MR on the third-party module. Then bumped my consumer to the fixed version. Took maybe 20 minutes including testing.

**Lesson:** any boot script that does anything destructive needs an idempotency guard. "It only runs on first boot" is not enough — first-boot scripts re-run more often than you'd expect when terraform decides a resource needs replacement.

## Sharp edge #2 — `ignore_changes` blocks the cure from reaching the patient

After fixing the script upstream and bumping the module version, I expected `terraform apply` to push the new script into each VM's GCE metadata. It did not.

```hcl
resource "google_compute_instance" "instance" {
  metadata_startup_script = templatefile(…)
  lifecycle {
    ignore_changes = [attached_disk, metadata_startup_script]
  }
}
```

That `ignore_changes` was added at some prior module version to prevent unrelated churn. Sensible at the time. Today it means **the fixed script never makes it onto the existing VMs**. Only freshly-created VMs from this point forward would have the fix.

The escape hatch is `gcloud compute instances add-metadata`, which sets the metadata field directly without going through terraform:

```bash
# Pull the deployed script from the VM, patch it locally to add the guards,
# then push it back. (You could also render the new templatefile output
# from terraform-state-equivalent inputs and push that — both work.)
gcloud compute instances describe "$HOST" --zone="$ZONE" --project="$PROJECT" \
  --format='value(metadata.items[]"key=startup-script")' > /tmp/old.sh

# … insert guards before the mkfs.ext4 line …

gcloud compute instances add-metadata "$HOST" \
  --metadata-from-file startup-script=/tmp/new.sh \
  --zone="$ZONE" --project="$PROJECT"
```

The metadata only matters on a future VM recreate. No reboot needed. But you do have to remember to run the loop — `terraform apply` will lie to you about a clean diff that ignores the very thing you want changed.

**Lesson:** when you add `ignore_changes` for an attribute, document — somewhere your future self will find — the manual procedure for updating that attribute. Otherwise the next time you need to update it (probably during an incident), you'll spend an hour wondering why your terraform changes don't take effect.

## Sharp edge #3 — `gcloud … reset` ate my chain state

This was the painful one. After deploying the fix, I wanted to **prove** the new boot script behaved correctly. So:

```bash
gcloud compute instances add-metadata test-host --metadata-from-file startup-script=/tmp/new.sh …
gcloud compute instances reset test-host …
# wait, then ssh in and check the journal
```

The journal showed `/opt` survived — good. But the application's service was crash-looping. Logs:

```
Error:
  Pack_error: "Inconsistent_store"
```

`reset` is GCE-speak for **hard power-cycle**. No ACPI shutdown, no SIGTERM to systemd, no chance for the application to flush its append-only store cleanly. The on-disk state was now a half-written page-pack the application refused to open. Recovery required a full state-directory wipe + re-import from a published snapshot URL — about 10 minutes for a small testnet node, would have been hours for a bigger one.

The fix is one verb:

```bash
gcloud compute instances stop  "$HOST" --zone="$ZONE" --project="$PROJECT"
gcloud compute instances start "$HOST" --zone="$ZONE" --project="$PROJECT"

# or, equivalently, if you're already SSH'd in:
sudo systemctl stop the-application
sudo reboot
```

`stop` issues an ACPI shutdown, systemd drains its services in dependency order with SIGTERM-then-SIGKILL after a grace period, and the application gets a chance to checkpoint. **Never `reset` a VM running a stateful application unless that application is provably restart-safe under hard power-cycle.**

Three things compound the trap:
- `reset` looks like a soft action in the gcloud help text ("perform a hard reset on the instance"). The word "hard" is right there, but you have to actually read it.
- The instances I was working on had survived `reset` events before — unattended-upgrades reboots use `systemctl reboot` (graceful), so the previous "reboot" experience was misleading.
- The corruption is silent at reset time. The application crashes only on the *next* startup, by which point you've moved on mentally.

**Lesson:** memorize the difference. `stop`/`start` = graceful, `reset` = unsafe for stateful workloads.

## Did the snapshots help?

Indirectly, yes. On the host I corrupted with `reset`, I didn't actually use the disk snapshot for recovery — re-importing from the public snapshot URL was faster than the snapshot-restore dance, because this was a rolling node with ~10 GB of state. But the *option* to restore from the snapshot was what let me try the reset experiment in the first place without much fear. Without the snapshot, I'd have had to be more conservative, which means I'd have learned the `reset` lesson at a worse time.

The snapshots also paid off as the "what does the plan show?" insurance during the module-version bump. The initial `terraform plan` after the version bump reported `Plan: 12 to add, 0 to change, 12 to destroy` — all 12 attached disks across the fleet would be replaced, because the new module version's default for `attached_disk_type` had changed from `pd-ssd` to `pd-balanced`. **Replacement of `google_compute_disk` = total data loss.** Catching it required reading the plan carefully; the change-tag in the diff was `# forces replacement` next to a one-line `type` difference, buried 200 lines into the output.

The fix was one pin per host:

```hcl
module "vigie_vm_host_a" {
  source             = "…/some-module/local"
  version            = "2.9.0"
  attached_disk_type = "pd-ssd"  # match the existing disk; new module default is pd-balanced
  # …
}
```

After the pin, the plan went to `0 add, 0 change, 0 delete`. Pure state migration. No snapshot was needed in the end — but the snapshot procedure gave me a credible "if this goes wrong, I have an out" *while I was reading the plan*, which is what bought me the calm to read it carefully in the first place.

## Takeaways

- **Pre-change disk snapshots cost basically nothing.** A few dollars for an entire fleet, for a few hours of retention. Just take them.
- **Write the restore procedure down.** Six gcloud commands, in order, including the `--type=` and `--device-name=` flags that don't default to "the same as before". You will not get these right under stress.
- **Read `terraform plan` output carefully when bumping module versions.** The interesting changes are buried. Search for `forces replacement` specifically. Module defaults can shift between versions.
- **First-boot scripts need idempotency guards.** `mountpoint -q` + `blkid` is six lines. There is no excuse for a boot script that can wipe data on its 2nd, 3rd, or 100th run.
- **`ignore_changes` is one-way.** When you add it, write down how to manually push the ignored attribute. You will need to, eventually.
- **`reset` is a hard power-cycle.** Don't use it on stateful workloads. `stop` + `start`, or `systemctl reboot` from inside.

None of these are clever. They're all the kind of operational habit that you only learn by losing data to the absence of them. Writing it down so the next person — including future-me — can borrow the lesson without re-buying it.
