---
layout: post
title: "Tini: the missing PID 1 for your containers"
date: 2026-07-11
categories: [docker, containers]
tags: [docker, docker-compose, tini, dumb-init, pid1, signals, devops]
---

When `docker stop` takes ten seconds instead of milliseconds, or when a container quietly accumulates zombie processes over time, the cause is usually the same: PID 1 is special, and your application was never written to handle it.

## What makes PID 1 special

On Linux, the kernel assigns PID 1 to the first process in a new PID namespace. Two things make it different from every other process:

**Signal handling.** The kernel *never* delivers `SIGTERM` to PID 1 unless it has explicitly installed a signal handler for it. Most applications don't — they rely on the default handler the C runtime sets up, which the kernel bypasses for PID 1. The practical result: `docker stop` sends `SIGTERM` to your container's PID 1, nothing happens, and after the timeout (default 10 seconds) Docker sends `SIGKILL`. Every graceful shutdown attempt silently fails.

**Zombie reaping.** When a process exits, it stays in the process table as a zombie until its parent calls `wait()` on it. If the parent exits first, the orphan is reparented to PID 1, which becomes responsible for reaping it. The operating system init daemon knows this and handles it. Your application almost certainly doesn't.

## What Tini does

[Tini](https://github.com/krallin/tini) (Tiny Init) is a minimal init process designed to solve exactly these two problems. It sits as PID 1, registers signal handlers that forward signals to its child process, and reaps zombie processes. Nothing else.

You launch your application as a child of Tini rather than as PID 1 directly:

```dockerfile
RUN apk add --no-cache tini
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["your-app"]
```

`docker stop` now sends `SIGTERM` to Tini, which forwards it to your application. Your application's default `SIGTERM` handler runs, it shuts down cleanly, and the container exits in milliseconds rather than ten seconds.

## The Docker Compose shortcut

If you control the Compose file and don't need the init embedded in the image, there's a one-liner:

```yaml
services:
  web:
    image: your-image
    init: true
```

`init: true` injects the Tini binary that ships with Docker Engine itself as PID 1. No separate download, no image change, no supply chain surface beyond what you've already trusted by running Docker. This is the lowest-effort correct solution for most Compose-based workloads.

## Why not just handle signals in the application?

You can. But it doesn't solve zombie reaping unless you also implement `waitpid()` loops, every language runtime handles signals slightly differently, and getting all shutdown paths right (graceful, panicking, subprocess spawning) is non-trivial. Tini is 200 lines of C that has been doing exactly this since 2015.

## Checking whether you need it

A quick test: run your container and check what's at PID 1.

```bash
docker exec <container> cat /proc/1/cmdline | tr '\0' ' '
```

If it prints your application binary directly, you're exposed to both problems. If it prints `tini` or `dumb-init`, you're covered.

## The alternative: dumb-init

[dumb-init](https://github.com/Yelp/dumb-init) (from Yelp) solves the same problem with a similar approach. It's more actively maintained than Tini and also available in Alpine and Debian package repos:

```dockerfile
RUN apk add --no-cache dumb-init
ENTRYPOINT ["dumb-init", "--"]
CMD ["your-app"]
```

The functional difference between the two is small enough not to matter for most workloads. If you're starting from scratch: `dumb-init` from the package manager, or `init: true` in Compose if that fits your deployment model. If you're already using Tini: stay on it, there's no reason to switch.

If you're starting from scratch: `dumb-init` from the package manager, or `init: true` in Compose. If you're already using Tini: no reason to switch.
