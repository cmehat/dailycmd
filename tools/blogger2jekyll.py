#!/usr/bin/env python3
"""
blogger2jekyll.py — Convert a Google Takeout Blogger feed (the 2018 atom format,
`Takeout/Blogger/Blogs/<Name>/feed.atom`) into Jekyll posts.

Usage:
    python3 tools/blogger2jekyll.py FEED.atom OUTDIR [options]

Options:
    --include-drafts     Also write DRAFT posts to OUTDIR/_drafts/ (default: skip)
    --download-images    Best-effort download of remote post images into
                         OUTDIR/assets/images/<slug>/ and rewrite <img src>.
                         (Run this on your own machine — needs internet access
                         to blogger.googleusercontent.com / *.bp.blogspot.com.)

Outputs:
    OUTDIR/_posts/YYYY-MM-DD-slug.md      published posts
    OUTDIR/_drafts/slug.md                drafts (with --include-drafts)

Each post keeps its original Blogger URL via an explicit `permalink:`, so links
like /2009/02/slug.html resolve identically on the new domain.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

ATOM = "http://www.w3.org/2005/Atom"
BLOGGER = "http://schemas.google.com/blogger/2018"


def q(tag, ns=ATOM):
    return f"{{{ns}}}{tag}"


def text_of(entry, tag, ns=ATOM):
    el = entry.find(q(tag, ns))
    return el.text if el is not None and el.text is not None else ""


def parse_published(s):
    """Handle '2009-02-09T11:29:00Z' and '2024-10-03T14:11:00.005Z'."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # strip fractional seconds if present (datetime.fromisoformat handles them
    # on 3.11+, but be defensive for older interpreters)
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        s2 = re.sub(r"\.\d+", "", s)
        return dt.datetime.fromisoformat(s2)


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "post"


def slug_and_permalink(entry):
    """Prefer the original Blogger path; fall back to a slugified title."""
    fn = text_of(entry, "filename", ns=BLOGGER).strip()
    if fn:
        permalink = fn if fn.startswith("/") else "/" + fn
        slug = os.path.splitext(os.path.basename(permalink))[0]
        return slug, permalink
    return slugify(text_of(entry, "title")), None


def labels_of(entry):
    out = []
    for cat in entry.findall(q("category")):
        term = cat.get("term")
        if term:
            out.append(term)
    return out


IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=)(["\'])(.*?)\2', re.I | re.S)


def localize_images(html, slug, outdir):
    import urllib.request
    import urllib.parse
    imgdir = os.path.join(outdir, "assets", "images", slug)
    rewrites = {}

    def grab(m):
        url = m.group(3)
        if not re.search(r"(googleusercontent|bp\.blogspot|blogger\.com)", url):
            return m.group(0)
        if url in rewrites:
            local = rewrites[url]
        else:
            os.makedirs(imgdir, exist_ok=True)
            name = os.path.basename(urllib.parse.urlparse(url).path) or f"img{len(rewrites)}"
            if "." not in name:
                name += ".jpg"
            dest = os.path.join(imgdir, name)
            try:
                urllib.request.urlretrieve(url, dest)
                local = f"/assets/images/{slug}/{name}"
                rewrites[url] = local
                print(f"    img {url} -> {local}")
            except Exception as e:  # noqa: BLE001
                print(f"    [skip] {url} ({e})", file=sys.stderr)
                return m.group(0)
        return f"{m.group(1)}{m.group(2)}{local}{m.group(2)}"

    return IMG_SRC_RE.sub(grab, html)


def wrap_raw(html):
    """Disable Liquid parsing of the migrated body. Old posts contain shell/code
    snippets with literal {{ }} and {% %} (e.g. `{{snip label}}`) that Jekyll's
    Liquid engine would otherwise try to interpret and choke on."""
    # `{% endraw %}` inside the body would close the block early. None of these
    # posts contain it, but guard anyway by emitting it as a Liquid string.
    if "{% endraw %}" in html:
        html = html.replace("{% endraw %}", "{% endraw %}{{ '{%' }} endraw {{ '%}' }}{% raw %}")
    return "{% raw %}\n" + html + "\n{% endraw %}"


def front_matter(title, date, permalink, tags, author, draft):
    lines = ["---", "layout: post", f"title: {json.dumps(title, ensure_ascii=False)}"]
    if date:
        lines.append(f"date: {date.strftime('%Y-%m-%d %H:%M:%S %z') or date.strftime('%Y-%m-%d %H:%M:%S +0000')}")
    if permalink:
        lines.append(f"permalink: {permalink}")
    if tags:
        lines.append("tags: [" + ", ".join(json.dumps(t, ensure_ascii=False) for t in tags) + "]")
    if author:
        lines.append(f"author: {json.dumps(author, ensure_ascii=False)}")
    if draft:
        lines.append("published: false")
    lines.append("---\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("feed")
    ap.add_argument("outdir")
    ap.add_argument("--include-drafts", action="store_true")
    ap.add_argument("--download-images", action="store_true")
    args = ap.parse_args()

    tree = ET.parse(args.feed)
    root = tree.getroot()

    posts_dir = os.path.join(args.outdir, "_posts")
    drafts_dir = os.path.join(args.outdir, "_drafts")
    os.makedirs(posts_dir, exist_ok=True)

    n_live = n_draft = n_skip = 0
    seen = set()

    for entry in root.findall(q("entry")):
        btype = text_of(entry, "type", ns=BLOGGER).strip()
        if btype != "POST":
            continue
        status = text_of(entry, "status", ns=BLOGGER).strip()
        is_draft = status == "DRAFT"
        if is_draft and not args.include_drafts:
            n_skip += 1
            continue

        title = text_of(entry, "title").strip() or "(untitled)"
        author = ""
        a = entry.find(q("author"))
        if a is not None:
            an = a.find(q("name"))
            author = an.text.strip() if an is not None and an.text else ""
        date = parse_published(text_of(entry, "published"))
        tags = labels_of(entry)
        slug, permalink = slug_and_permalink(entry)
        content = text_of(entry, "content") or ""

        if args.download_images:
            content = localize_images(content, slug, args.outdir)

        fm = front_matter(title, date, permalink, tags, author, draft=is_draft)
        body = fm + wrap_raw(content) + "\n"

        if is_draft:
            os.makedirs(drafts_dir, exist_ok=True)
            fname = f"{slug}.md"
            target = os.path.join(drafts_dir, fname)
            n_draft += 1
        else:
            datestr = date.strftime("%Y-%m-%d") if date else "1970-01-01"
            fname = f"{datestr}-{slug}.md"
            target = os.path.join(posts_dir, fname)
            n_live += 1

        # avoid collisions (two posts same date+slug)
        base, ext = os.path.splitext(target)
        i = 2
        while target in seen or os.path.exists(target):
            target = f"{base}-{i}{ext}"
            i += 1
        seen.add(target)

        with open(target, "w", encoding="utf-8") as f:
            f.write(body)

    print(f"\n✓ {n_live} posts -> _posts/")
    if args.include_drafts:
        print(f"✓ {n_draft} drafts -> _drafts/")
    else:
        print(f"  ({n_skip} drafts skipped — pass --include-drafts to keep them)")


if __name__ == "__main__":
    main()
