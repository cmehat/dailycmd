#!/usr/bin/env python3
"""
fetch_images.py — Download remote IMAGES referenced in Jekyll posts and rewrite
the references to local paths, so the site no longer depends on Blogger's
(googleusercontent) URLs.

Scans posts for:
  - <img src="...">            (thumbnails / inline images)
  - <a href="...image...">     (Blogger wraps thumbnails in a link to full-size)
...but ONLY downloads when the host is a known image host AND (for href) the URL
actually looks like an image. Ordinary article hyperlinks are left untouched.
A Content-Type check at download time is the final guard against grabbing HTML.

Usage (run from repo root, on a machine with internet access):

    python3 tools/fetch_images.py --dry-run                    # report, no network
    python3 tools/fetch_images.py                              # download + rewrite
    python3 tools/fetch_images.py --hosts kaourintinn.free.fr  # also localize free.fr

Idempotent and re-runnable. Writes assets/images/_manifest.json and lists any
dead URLs at the end.
"""
import argparse, glob, hashlib, json, os, re, sys, time
import urllib.parse, urllib.request

# Hosts that ONLY serve media — safe to localize.
IMAGE_HOSTS = [
    "blogger.googleusercontent.com",
    "googleusercontent.com",      # lh3/lh4.googleusercontent.com, etc.
    "bp.blogspot.com",            # 1.bp.blogspot.com, 2.bp.blogspot.com, ...
    "photos1.blogger.com",
]
URL_ATTR_RE = re.compile(r'(src|href)\s*=\s*(["\'])(?P<url>[^"\']+)\2', re.I)
IMG_EXT_RE = re.compile(r'\.(jpe?g|png|gif|webp|svg|bmp|ico|tiff?)(?:[?#]|$)', re.I)
CT_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/bmp": ".bmp", "image/tiff": ".tiff", "image/x-icon": ".ico",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def host_matches(netloc, hosts):
    netloc = netloc.lower().split(":")[0]
    return any(netloc == h or netloc.endswith("." + h) or netloc.endswith(h) for h in hosts)


def is_image_href(url, netloc):
    if IMG_EXT_RE.search(url):
        return True
    if netloc.endswith("googleusercontent.com"):   # only serves media
        return True
    if "bp.blogspot.com" in netloc and not url.split("?")[0].lower().endswith(".html"):
        return True
    return False


def local_name(url, content_type=None):
    p = urllib.parse.urlparse(url)
    base = urllib.parse.unquote(os.path.basename(p.path)).split("?")[0]
    name, ext = os.path.splitext(base)
    if not re.match(r"^\.[A-Za-z0-9]{1,4}$", ext):
        ext = CT_EXT.get((content_type or "").split(";")[0].strip().lower(), "")
        if not ext:
            m = IMG_EXT_RE.search(url)
            ext = "." + m.group(1).lower() if m else ".img"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:40] or "image"
    return f"{hashlib.sha1(url.encode()).hexdigest()[:8]}-{name}{ext}"


def download(url, timeout, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--globs", default="_posts/*.md,_drafts/*.md")
    ap.add_argument("--hosts", default="", help="extra image hosts, comma-separated")
    ap.add_argument("--outdir", default="assets/images")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    hosts = IMAGE_HOSTS + [h.strip() for h in args.hosts.split(",") if h.strip()]
    outdir = os.path.join(root, args.outdir)

    files = []
    for g in args.globs.split(","):
        files += glob.glob(os.path.join(root, g.strip()))
    files = sorted(set(files))

    refs = {}          # url -> set(files)
    src_skipped = {}   # host -> count (embedded resources we leave as-is)
    for fp in files:
        text = open(fp, encoding="utf-8").read()
        for m in URL_ATTR_RE.finditer(text):
            attr, url = m.group(1).lower(), m.group("url")
            if url[:1] in ("/", "#") or url.startswith(("data:", "mailto:")):
                continue
            netloc = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
            if not netloc:
                continue
            target = host_matches(netloc, hosts)
            if attr == "src":
                if target:
                    refs.setdefault(url, set()).add(fp)
                else:
                    src_skipped[netloc] = src_skipped.get(netloc, 0) + 1
            elif attr == "href" and target and is_image_href(url, netloc):
                refs.setdefault(url, set()).add(fp)

    print(f"Scanned {len(files)} files.")
    print(f"Images to localize: {len(refs)}")
    if src_skipped:
        print("Embedded resources left as-is (external, not the old blog):")
        for h, c in sorted(src_skipped.items(), key=lambda x: -x[1]):
            print(f"  {c:>3}  {h}")

    if args.dry_run:
        print("\n--dry-run: would download and rewrite:")
        for url in sorted(refs):
            print(f"  {local_name(url):<52} <- {url}")
        return

    if not refs:
        print("Nothing to do.")
        return

    os.makedirs(outdir, exist_ok=True)
    manifest_path = os.path.join(outdir, "_manifest.json")
    url_to_local = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}

    failures, notimg = [], []
    for i, url in enumerate(sorted(refs), 1):
        if url in url_to_local and os.path.exists(os.path.join(root, url_to_local[url].lstrip("/"))):
            continue
        try:
            data, ct = download(url, args.timeout)
        except Exception as e:  # noqa: BLE001
            failures.append((url, str(e)))
            print(f"[{i}/{len(refs)}] FAIL {url} ({e})", file=sys.stderr)
            continue
        ctype = ct.split(";")[0].strip().lower()
        if ctype and not ctype.startswith(("image/", "video/")) and ctype != "application/octet-stream":
            notimg.append((url, ctype))
            print(f"[{i}/{len(refs)}] skip {url} (not media: {ctype})", file=sys.stderr)
            continue
        fname = local_name(url, ct)
        with open(os.path.join(outdir, fname), "wb") as f:
            f.write(data)
        url_to_local[url] = "/" + os.path.join(args.outdir, fname).replace("\\", "/")
        print(f"[{i}/{len(refs)}] ok   {url} -> {url_to_local[url]} ({len(data)} B)")
        time.sleep(args.delay)

    rewritten = 0
    for fp in files:
        text = open(fp, encoding="utf-8").read()
        new = text
        for url, local in url_to_local.items():
            if url in new:
                new = new.replace(url, local)
        if new != text:
            open(fp, "w", encoding="utf-8").write(new)
            rewritten += 1

    json.dump(url_to_local, open(manifest_path, "w"), indent=2, ensure_ascii=False)
    print(f"\n\u2713 {len(url_to_local)} images in {args.outdir}/  |  rewrote {rewritten} files")
    print(f"\u2713 manifest: {os.path.join(args.outdir, '_manifest.json')}")
    if notimg:
        print(f"\n\u26a0 {len(notimg)} URL(s) were not images (left as links):")
        for u, c in notimg:
            print(f"  {u} ({c})")
    if failures:
        print(f"\n\u26a0 {len(failures)} URL(s) could not be fetched (likely dead) — handle manually:")
        for u, e in failures:
            print(f"  {u} ({e})")


if __name__ == "__main__":
    main()
