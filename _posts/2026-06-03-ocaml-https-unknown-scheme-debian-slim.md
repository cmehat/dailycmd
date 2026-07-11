---
layout: post
title: "OCaml's 'unknown scheme' on Debian slim — when /etc/services goes missing"
date: 2026-06-03 14:00:00 +0000
permalink: /2026/06/ocaml-https-unknown-scheme-debian-slim.html
tags: ["ocaml", "cohttp", "conduit", "tls", "docker", "debian-slim", "netbase", "https", "debugging", "kubernetes", "incident"]
author: "cm"
---


I shipped a small OCaml HTTP service to a Kubernetes cluster. Inbound traffic worked. Slash commands (POST in, JSON out) worked. But every time the service had to *call* an `https://` endpoint, it died with:

```
[EROR] failure (cid=N, error="resolution failed: unknown scheme")
[EROR] response (cid=N, code=500)
```

The path from that error to a one-line fix involved being publicly wrong about the cause first.

I'll use generic names: `my-service` (the OCaml HTTP server), `my-service:latest` (its image), `https://hooks.example.com/...` (an outbound URL it tries to POST to). Stack: OCaml 5.2 + `cohttp-lwt-unix` 6.2 + `conduit-lwt-unix` 8.0 + a multi-stage Dockerfile that copies the built binary onto `debian:bookworm-slim`.

## Symptoms

The service has a `/metrics` Prometheus endpoint. After the first outbound failure, its counters looked like this:

```
my_service_http_requests_total{method="POST",path="/inbound/slash",code="200"} 14
my_service_http_requests_total{method="POST",path="/inbound/interactivity",code="500"} 1
```

Server-side, every successful slash-command POST handled itself — read, decode, build JSON response, write — without ever touching the network. The 500 was the first request that needed to make an outbound HTTPS call (post a webhook back to the platform's `response_url`). The bot logged the URL it was about to call:

```
[DBUG] send HTTP request (url="https://slack.com/api/chat.postMessage", verb="POST")
[EROR] failure (cid=N, error="resolution failed: unknown scheme")
```

The URL string is plainly well-formed: `https://`, host, path. So why "unknown scheme"?

## Wrong diagnosis #1: "TLS isn't linked"

My first hypothesis was that the binary lacks TLS support. Plausible: `cohttp-lwt-unix` alone is just an HTTP-over-TCP library; for `https://` you need a TLS implementation (`tls-lwt` for pure OCaml, or `lwt_ssl` for OpenSSL). If you forget to depend on one, `Conduit_lwt_unix` has no resolver registered for the `https` scheme.

I checked the published binary for TLS symbols by running:

```sh
docker run --rm --entrypoint /bin/sh my-service:latest \
  -c 'strings /usr/local/bin/my-service | grep -iE "tls|x509|ssl|cert"'
```

The output was empty. I took that as proof that no TLS library was linked, opened a PR adding `tls-lwt` and `ca-certs` to the dune `(libraries …)` stanza, and wrote a confident description that traced the symptom to missing TLS linkage.

The check was wrong. The runtime image is `debian:bookworm-slim`, which doesn't ship `strings` — so the command silently produced no matches because `binutils` wasn't installed, not because the symbols were absent. I'd run the check inside the wrong container.

## The reviewer's pushback

A reviewer caught it:

> The PR description claims `tls-lwt` wasn't being linked into the executable, but the project already lists `tls-lwt` in the `(executables …)` stanza. If outbound HTTPS is still failing with unknown scheme, it likely means the specific Conduit HTTPS resolver/initializer module isn't getting linked or initialized.

I re-checked, this time extracting the binary onto the host (`docker cp` out of a container created with `docker create`, then running `strings` on the host where it actually exists):

```sh
$ cid=$(docker create my-service:latest)
$ docker cp "$cid":/usr/local/bin/my-service /tmp/my-service-bin
$ docker rm $cid

$ strings /tmp/my-service-bin | grep -cE "^(Tls|X509|Mirage_crypto|Tls_lwt|X509_lwt)(__|$)"
1871

$ strings /tmp/my-service-bin | grep -iE "^Tls_lwt|^X509|^Mirage_crypto_rng_unix" | sort -u | head
Mirage_crypto_rng_unix
Tls
Tls_lwt
Tls__Core
Tls__State
Tls__Utils
X509
X509__
X509__Crl
X509__P12
```

Plenty of TLS code. So the binary was fine. `Conduit_lwt_unix` 8.0 also already lists `tls-lwt` and `ca-certs` in its META `requires`, so they get pulled in transitively from `cohttp-lwt-unix`. The first diagnosis was wrong; the binary has had HTTPS support all along.

Which meant my open PR was wrong too, and I had to figure out the actual cause.

## Reading Conduit's source

The error string "resolution failed: unknown scheme" isn't in my code or my dependencies' `*.ml` files outside conduit. The path through conduit looks like this:

`conduit-8.0.0/lib/conduit/resolver.ml`:

```ocaml
let resolve_uri ?rewrites ~uri t =
  match Uri.scheme uri with
  | None -> return (`Unknown "no scheme")
  | Some scheme -> (
      t.service scheme >>= function
      | None -> return (`Unknown "unknown scheme")
      ...
```

So `unknown scheme` fires when `t.service "https"` returns `None`. The default `t.service` is `system_service`, defined in `conduit-lwt-unix-8.0.0/lib/resolver_lwt_unix.ml`:

```ocaml
let is_tls_service =
  function
  | "https" | "imaps" -> true
  | _ -> false

let system_service name =
  Lwt.catch
    (fun () ->
      Lwt_unix.getservbyname name "tcp" >>= fun s ->
      let tls = is_tls_service name in
      let svc = { Resolver.name; port = s.Lwt_unix.s_port; tls } in
      Lwt.return (Some svc))
    (function Not_found -> Lwt.return_none | e -> Lwt.reraise e)
```

`Lwt_unix.getservbyname "https" "tcp"`. That's the libc `getservbyname(3)` syscall, which reads `/etc/services`:

```
$ man 5 services
DESCRIPTION
       services is a plain ASCII file providing a mapping between human-friendly
       textual names for internet services, and their underlying assigned port
       numbers...
```

If the file is absent, `getservbyname` returns `NULL`, OCaml raises `Not_found`, `system_service` returns `None`, conduit returns `Unknown "unknown scheme"`. **Independently of TLS.** Independently of the URI scheme too, if the OS lookup for that scheme fails.

## Confirming the actual cause

Check the file in the running container:

```sh
$ docker run --rm --entrypoint /bin/sh my-service:latest -c 'ls -la /etc/services'
ls: cannot access '/etc/services': No such file or directory
```

And on a stock `debian:bookworm-slim`:

```sh
$ docker run --rm --entrypoint /bin/sh debian:bookworm-slim -c 'ls -la /etc/services'
ls: cannot access '/etc/services': No such file or directory
```

`debian:bookworm-slim` doesn't ship `/etc/services`. The Debian package that provides it is `netbase`, which is *not* in the slim base. Same story with `iana-etc` on Alpine, and most distroless variants.

## The fix

One line in the runtime stage of the Dockerfile:

```diff
 RUN apt-get update && apt-get install -y --no-install-recommends \
     libgmp10 \
     libssl3 \
     zlib1g \
     ca-certificates \
+    netbase \
  && rm -rf /var/lib/apt/lists/*
```

That's it. No dune changes, no opam changes, no code changes. The Dockerfile had been omitting one OS-level data file the OCaml runtime depends on, and `cohttp-lwt-unix` translated the absence into a misleading-looking library-level error.

## Verifying it actually works

I went back and proved the fix end-to-end with a controlled reproducer. Built both the broken and fixed images, ran each one, and sent a properly HMAC-signed POST to the interactivity endpoint:

```python
import hmac, hashlib, time, urllib.parse, urllib.request, json

secret = b"signing-secret"
payload = {
    "type": "block_actions",
    "user": {"id": "U1", "team_id": "T", "username": "test", "name": "test"},
    "team": {"id": "T", "domain": "d"},
    "channel": {"id": "C", "name": "c"},
    "container": {"type":"message","message_ts":"1","channel_id":"C","is_ephemeral":True},
    "trigger_id": "T",
    "is_enterprise_install": False,
    "response_url": "https://hooks.example.com/actions/T/1/x",
    "actions": [{"type":"users_select","action_id":"a","block_id":"b",
                 "selected_user":"U2","action_ts":"1"}],
}
body = "payload=" + urllib.parse.quote(json.dumps(payload), safe="")
ts = str(int(time.time()))
sig = "v0=" + hmac.new(secret, f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()

req = urllib.request.Request(
    "http://localhost:8080/inbound/interactivity",
    data=body.encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded",
             "X-Slack-Signature": sig,
             "X-Slack-Request-Timestamp": ts},
    method="POST",
)
try:
    print("HTTP", urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
```

The fake `response_url` (`https://hooks.example.com/actions/T/1/x`) doesn't exist on the platform, so the platform's edge will return 404 — but that's a *success* indicator for the experiment. If the outbound HTTPS call goes through at the TLS layer, we'll see a 4xx body coming back from the platform. If it fails at conduit's resolver, we'll see the original `unknown scheme` and HTTP 500.

**Broken image:**

```
HTTP 500
[INFO] request (cid=0, verb="POST", path="/inbound/interactivity")
[WARN] add assignee (cid=0, ..., result="no such alert")
[EROR] failure (cid=0, error="resolution failed: unknown scheme")
[EROR] response (cid=0, code=500)
```

**Fixed image:**

```
HTTP 200
[INFO] request (cid=0, verb="POST", path="/inbound/interactivity")
[WARN] add assignee (cid=0, ..., result="no such alert")
[EROR] platform error (cid=0, error="HTTP 404 Not Found")
[INFO] response (cid=0, code=200)
```

The `HTTP 404 Not Found` is the *progress* signal: TLS handshake succeeded, DNS resolved, the platform's edge returned the expected error for the fake URL. The transport layer works.

## Lessons

There's one bug here and two takeaways.

**1. Slim base images strip OS data files that runtime libraries read.**

`netbase` (provides `/etc/services`, `/etc/protocols`, `/etc/rpc`), `tzdata` (`/usr/share/zoneinfo/*`), `iana-etc` on Alpine — none of these are in the minimal images, and lots of libraries quietly need them. The breakage looks like a library bug because the error originates inside the library; the root cause is the missing data file the library expected.

A small audit list before deploying any slim/distroless image with HTTP/TLS/timezone/protocol-name behaviour:

- Outbound HTTPS or HTTP with named ports → `/etc/services` → `netbase`
- Timezone-aware logging or scheduling → `/usr/share/zoneinfo` → `tzdata`
- DNS via name resolution → `/etc/nsswitch.conf` may be needed → `libc-bin`
- TLS verification → CA bundle → `ca-certificates`

Each of these is one Debian/Alpine package. Each is one line in the Dockerfile.

**2. "I checked" is not the same as "I checked correctly."**

My first check — `strings | grep` for TLS symbols — was the right idea executed wrong. The runtime container didn't have `strings`, so an empty output meant "couldn't run the check," not "no symbols found." I read the result as proof and shipped a wrong diagnosis on the back of it.

What I should have done before claiming the binary lacked TLS: extract the binary out of the container to a host that has `strings`, or use `apk add binutils` / `apt install binutils` inside, or use a different tool entirely (`nm`, `readelf`). When a verification check returns the answer you expected, that's exactly when to double-check that the check actually ran.

The reviewer's "but the project already lists `tls-lwt`" was the cheap nudge that re-opened the diagnosis. Without it I would have shipped the wrong PR. Cheap nudges from reviewers who are reading the same code are worth more than confidence in your own reasoning.

## TL;DR

If you ship an OCaml binary using `cohttp-lwt-unix` or anything else that reaches for `Lwt_unix.getservbyname` and put it on `debian:bookworm-slim`, your outbound calls will die with `resolution failed: unknown scheme`. Add `netbase` to the runtime stage. One line.

And if you're staring at a binary trying to prove a negative ("symbol X isn't here"), make sure the tool you're using to look exists in the same image you're looking inside.
