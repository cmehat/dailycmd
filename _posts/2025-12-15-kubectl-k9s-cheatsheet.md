---
layout: post
title: "kubectl & k9s cheatsheet"
date: 2025-12-15 16:30:00 +0000
categories: [kubernetes, tools]
tags: [kubernetes, kubectl, k9s, cli, cheatsheet]
---

The commands I actually use. `kubectl` for scripting and precision, [k9s](https://k9scli.io/)
for interactive triage.

## kubectl — context & namespace

| Action | Command |
|--------|---------|
| List contexts | `kubectl config get-contexts` |
| Switch context | `kubectl config use-context NAME` |
| Current context | `kubectl config current-context` |
| Set default namespace | `kubectl config set-context --current --namespace=NS` |

## kubectl — inspect

| Action | Command |
|--------|---------|
| Pods with node + IP | `kubectl get pods -o wide` |
| Everything in a namespace | `kubectl get all -n NS` |
| Watch pods change | `kubectl get pods -w` |
| Describe (events at the bottom) | `kubectl describe pod POD` |
| Recent cluster events | `kubectl get events --sort-by=.lastTimestamp` |
| Resource usage | `kubectl top pods` / `kubectl top nodes` |
| Full YAML of a live object | `kubectl get deploy NAME -o yaml` |
| One field via jsonpath | `kubectl get pod POD -o jsonpath='{.status.podIP}'` |
| Find pods by label | `kubectl get pods -l app=NAME -A` |

## kubectl — logs

| Action | Command |
|--------|---------|
| Follow logs | `kubectl logs -f POD` |
| Specific container | `kubectl logs POD -c CONTAINER` |
| Previous crashed container | `kubectl logs POD --previous` |
| All pods of a deployment | `kubectl logs -f deploy/NAME` |
| Last hour only | `kubectl logs POD --since=1h` |

## kubectl — act

| Action | Command |
|--------|---------|
| Shell into a pod | `kubectl exec -it POD -- sh` |
| Restart a deployment | `kubectl rollout restart deploy/NAME` |
| Watch a rollout | `kubectl rollout status deploy/NAME` |
| Undo a rollout | `kubectl rollout undo deploy/NAME` |
| Scale | `kubectl scale deploy/NAME --replicas=3` |
| Port-forward | `kubectl port-forward svc/NAME 8080:80` |
| Copy file out of a pod | `kubectl cp NS/POD:/path/file ./file` |
| Apply / diff first | `kubectl apply -f f.yaml` / `kubectl diff -f f.yaml` |
| Throwaway debug pod | `kubectl run tmp --rm -it --image=busybox -- sh` |
| Cordon + drain a node | `kubectl cordon NODE && kubectl drain NODE --ignore-daemonsets` |

Watch out for short-name collisions: on clusters with extra CRDs, `kubectl get
backup` may resolve to a different resource than you expect — prefer the
fully-qualified form (`backups.velero.io`) in scripts.

## k9s — navigation

Everything starts with `:` (command mode) or `/` (filter).

| Action | Keys |
|--------|------|
| Open resource view | `:pods`, `:deploy`, `:svc`, `:nodes`, … |
| Any CRD too | `:applications`, `:certificates`, … |
| Filter rows | `/pattern` (regex), `Esc` to clear |
| Invert filter | `/!pattern` |
| Switch namespace | `:ns` then Enter on one, or `0` for all |
| Switch context | `:ctx` |
| Go back | `Esc` |
| Quit | `:q` or `Ctrl-c` |

## k9s — on a selected pod

| Action | Keys |
|--------|------|
| Logs | `l` (then `f` to toggle follow, `p` for previous container) |
| Shell | `s` |
| Describe | `d` |
| YAML | `y` |
| Delete | `Ctrl-d` |
| Kill (no grace) | `Ctrl-k` |
| Port-forward | `Shift-f` |
| Sort by CPU / memory | `Shift-c` / `Shift-m` |
| Mark pod (multi-select) | `Space` |

## k9s — wider views

| Action | Keys |
|--------|------|
| Events for the cluster | `:events` |
| Pulses (cluster overview) | `:pulses` |
| xray (resource tree) | `:xray deploy NS` |
| Popeye (lint the cluster) | `:popeye` |
| Toggle wide columns | `Ctrl-w` |
| Toggle header | `Ctrl-e` |

## The triage loop

My default incident sequence, entirely in k9s:

1. `:pods` then `/` on the app name — anything not `Running`?
2. `d` on the suspect — read the Events section first, not the spec.
3. `l` then `p` — the *previous* container's logs hold the crash reason;
   the current one may be too young to have logged anything.
4. `:events` sorted by time — anything cluster-wide (evictions, image pulls,
   OOM kills) that the pod view doesn't show.
