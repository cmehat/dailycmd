# dailycmd — blog.oyatrino.com

Jekyll site migrated from the Blogger blog *Commandes en Vrac* (Google Takeout).
Builds natively on GitHub Pages — a plain `git push` rebuilds it, no Actions
workflow needed.

## What's here

- `_posts/` — 284 published posts, with original Blogger permalinks preserved
  (`/YYYY/MM/slug.html`), tags, dates and authors.
- `_config.yml` — site identity, `url: https://blog.oyatrino.com`, minima theme.
- `CNAME` — `blog.oyatrino.com` (the canonical domain).
- `tools/blogger2jekyll.py` — the converter, kept so you can re-run it
  (e.g. to pull in drafts or localize images).

## Local preview

```bash
bundle install
bundle exec jekyll serve   # http://localhost:4000
```

(Requires Ruby + Bundler. `bundle install` pulls the `github-pages` gem,
which matches exactly what GitHub builds server-side.)

## Publish to GitHub Pages

1. Create a repo (e.g. `oyatrino-blog`) and push this directory to `main`.
2. Repo **Settings → Pages**: set *Source* to *Deploy from a branch*,
   branch `main`, folder `/ (root)`. The `CNAME` file already sets the
   custom domain.
3. Once DNS resolves (below), tick **Enforce HTTPS**.

## DNS for `blog.oyatrino.com`

`blog` is a **subdomain**, so it's a single CNAME record (not the apex
A-records):

| Type  | Host   | Value                       |
| ----- | ------ | --------------------------- |
| CNAME | `blog` | `<your-github-user>.github.io.` |

GitHub provisions the TLS cert automatically a few minutes after the record
resolves.

## The other two domains (.ca / .xyz)

GitHub Pages serves only one canonical domain per site, so `.ca` and `.xyz`
each get a tiny redirect repo (see the `redirect-blog-ca/` and
`redirect-blog-xyz/` folders shipped alongside this one). Each:

- has its own `CNAME` (claiming that domain on GitHub so nobody else can),
- redirects every path to `https://blog.oyatrino.com/...` (path-preserving).

Same DNS pattern for each: `CNAME  blog  <your-github-user>.github.io.`

## Re-running the converter

```bash
# include the 23 drafts (written to _drafts/, not published until you move them)
python3 tools/blogger2jekyll.py "<path>/Commandes en Vrac/feed.atom" . --include-drafts

# localize images (run on your machine — needs internet to googleusercontent):
python3 tools/blogger2jekyll.py "<path>/Commandes en Vrac/feed.atom" . --download-images
```

## Localizing images (remove the googleusercontent dependency)

`tools/fetch_images.py` downloads every Blogger-hosted image into
`assets/images/` and rewrites the references, so the site stops depending on
`blogger.googleusercontent.com`. Run from the repo root (needs internet):

```bash
python3 tools/fetch_images.py --dry-run                      # preview, no network
python3 tools/fetch_images.py                                # download + rewrite
python3 tools/fetch_images.py --hosts kaourintinn.free.fr    # also pull old free.fr files
```

- Handles both `<img src>` thumbnails and the `<a href>` full-size links Blogger
  wraps around them; localizes images and blog-hosted video.
- A Content-Type check prevents grabbing HTML; ordinary article hyperlinks are
  left untouched. External embeds (jsdelivr, YouTube, etc.) are reported and left
  as-is.
- Idempotent: writes `assets/images/_manifest.json`, skips already-localized
  files, and lists any dead URLs at the end. Safe to re-run.

## Notes / TODO

- **Old-post cruft**: pre-2010 posts carry Blogger artifacts
  (`blsp-spelling-error` spans, empty `<blockquote>`). Harmless; clean up
  opportunistically.
- The Takeout also contained 3 other blogs (*Laïp Siks Thesis*, *LAK6ST TRIBE*,
  *Sous La Fenetre la Chienlit*) — not migrated. Re-run the converter against
  their `feed.atom` if you want any of them.

## Sidebar widgets (Archive + Labels)

Three pure-Liquid widgets (no plugins — work on GitHub Pages' native build):

- `_includes/widget_archive.html` — foldable year → month → posts (native
  `<details>`, no JS). Param `open_years=N` expands the N most recent years.
- `_includes/widget_tags_freq.html` — labels by frequency. Param `limit=N`.
- `_includes/widget_tags_alpha.html` — labels A→Z.

They appear in a sidebar on the home page (`_layouts/home.html`) and on two
dedicated pages: `/archive/` (full tree) and `/tags/` (full label index with
each label's posts). Styling lives in `assets/main.scss` (theme-neutral, so it
works with any minima skin). Nav order is set by `header_pages` in `_config.yml`.

To drop a widget anywhere else: `{% include widget_archive.html %}`.
