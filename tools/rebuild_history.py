#!/usr/bin/env python3
"""
rebuild_history.py — Rebuild git history so each blog post is its own commit,
dated at the post's real publication date (from the front-matter `date:`).
Run from the repo root.

Builds a fresh orphan branch `dated`, leaving your current branch as a backup:
  1. one root commit with all site infrastructure (config, layouts, includes,
     assets, tools), dated just before the first post;
  2. then one commit per post, oldest first, with the commit's AUTHOR and
     COMMITTER date set to the post date (GitHub's graph uses author date).
"""
import glob, os, re, subprocess, sys

def head(path, n=1500):
    with open(path, encoding="utf-8") as f:
        return f.read(n)

def post_date(path):
    m = re.search(r'^date:\s*(.+?)\s*$', head(path), re.M)
    return m.group(1).strip() if m else os.path.basename(path)[:10] + " 12:00:00 +0000"

def post_title(path):
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', head(path), re.M)
    return m.group(1).strip() if (m and m.group(1).strip()) else os.path.basename(path)

def git(*args, date=None):
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], check=True, env=env)

def main():
    if subprocess.run(["git","rev-parse","--git-dir"], capture_output=True).returncode:
        sys.exit("Not a git repo — run from the repo root.")
    if subprocess.run(["git","status","--porcelain"], capture_output=True, text=True).stdout.strip():
        sys.exit("Working tree not clean — commit first (current branch = backup).")

    posts = sorted(glob.glob("_posts/*.html") + glob.glob("_posts/*.md"))
    if not posts:
        sys.exit("No posts under _posts/.")

    git("checkout", "--orphan", "dated")
    git("reset")                                  # empty the index
    git("add", "-A")                              # stage everything
    git("rm", "-r", "--cached", "--quiet", "_posts")   # ...except posts
    git("commit", "-m", "Site setup: Jekyll, theme, widgets, tooling", date=post_date(posts[0]))

    for p in posts:
        git("add", p)
        git("commit", "-q", "-m", f"post: {post_title(p)}", date=post_date(p))
    print(f"OK: {len(posts)} post commits + 1 setup commit on branch 'dated'.")

if __name__ == "__main__":
    main()
