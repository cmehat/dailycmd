---
layout: post
title: "A preview page for every merge request with GitLab CI — even on the Free plan"
date: 2026-06-05 09:00:00 +0000
permalink: /2026/06/gitlab-ci-per-mr-preview-pages.html
tags: ["gitlab", "gitlab-ci", "gitlab-pages", "ci", "cd", "review-app", "preview", "merge-request", "environment", "artifacts", "devops"]
author: "cm"
---

You change a CSS rule, a paragraph of copy, a generated report. The diff looks
right. But "looks right in the diff" and "looks right in a browser" are not the
same thing, and the only way to close that gap is to render the change somewhere
a reviewer can click.

This is a how-to for wiring that up on GitLab: **every merge request gets its
own preview page, with a "View app" button right on the MR.** No review server
to babysit, no extra infrastructure — and, the part most tutorials skip, **it
works on the Free plan**, because it doesn't rely on publishing multiple
GitLab Pages sites at once.

The example is lifted from a real Terraform + Ansible infrastructure pipeline I
maintain, where each MR renders an HTML "infrastructure report" so reviewers can
*see* what a change produces instead of reading raw plan output.

## The catch nobody mentions

The obvious way to do per-MR previews is GitLab Pages **parallel deployments**
(`pages.path_prefix`), which host each branch under its own URL prefix. It's
clean — and on many setups it's either gated behind a paid tier or a recent
GitLab version. If you're on the Free plan, you typically get **one** Pages
deployment: your default branch. Try to publish a second one per MR and you're
out of luck.

So we use a different lever that exists on every plan:

> GitLab serves a job's **artifacts** over the web. If a CI job uploads an HTML
> file as an artifact, GitLab gives you a URL that renders it — sandboxed on the
> Pages domain. Point a merge-request **environment** at that URL and you have a
> per-MR preview without ever publishing a second Pages site.

The build artifact *is* the preview. No parallel deployment required.

## How the pieces fit

1. A **build job** produces the page into `public/` and uploads it as an
   artifact (with a short `expire_in`, since previews are disposable).
2. A **review job** declares an `environment` whose `url` points at that
   artifact, served on the Pages domain so the HTML renders inline.
3. `auto_stop_in` + an `on_stop` job make the environment self-clean.

The `environment` block is the magic: attaching one to a job is exactly what
makes the **View app** button appear on the merge request.

## Step 1 — build the page into an artifact

Nothing special; produce your HTML into `public/` and upload it. Keep the
default-branch publish and the MR preview as two jobs sharing the same build —
here I'll show a single build that both consume:

```yaml
build-page:
  stage: build
  script:
    - mkdir -p public
    - ./generate_report.sh > public/index.html   # whatever produces your HTML
  artifacts:
    paths:
      - public
    expire_in: 1 day        # previews are disposable
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
    - if: $CI_MERGE_REQUEST_IID
```

`expire_in: 1 day` matters: the preview lives only as long as the artifact does,
so you're not accumulating stale renders.

## Step 2 — publish the real site on the default branch

On the default branch, do a normal Pages publish. This is your "production"
page and the baseline reviewers compare previews against:

```yaml
pages:
  stage: deploy
  script:
    - echo "Publishing to ${CI_PAGES_URL}"
  needs:
    - job: build-page
      optional: true
  artifacts:
    paths:
      - public
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  environment:
    name: "Production page - $CI_DEFAULT_BRANCH"
    url: "$CI_PAGES_URL"
```

(The `pages` job name is special — it's what triggers GitLab's built-in Pages
deployment. The `script` is a formality; the artifact is what gets published.)

## Step 3 — the per-MR preview (the actual trick)

Here's the review job. It runs only on merge requests and points the environment
URL at the build artifact, served on the Pages domain:

```yaml
pages:review:
  stage: deploy
  script:
    - echo "Creating review environment for ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
  needs:
    - build-page
  artifacts:
    paths:
      - public
  rules:
    - if: $CI_MERGE_REQUEST_IID
  environment:
    name: "Review page - ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
    url: "${CI_PAGES_URL}/-/jobs/${CI_JOB_ID}/artifacts/public/index.html"
    auto_stop_in: 1 hour
    on_stop: stop_review
```

The `url` line is the whole point, so it's worth dissecting:

- **`${CI_PAGES_URL}`** is your project's Pages root — e.g.
  `https://group.gitlab.io/-/subgroup/project`. Using the variable instead of
  hardcoding the host keeps it correct across renames and custom domains.
- **`/-/jobs/${CI_JOB_ID}/artifacts/public/index.html`** is GitLab's
  artifact-browsing path. `CI_JOB_ID` ties the URL to *this* job's artifacts, so
  each MR pipeline gets its own preview.
- Serving it **through the Pages domain** (the `group.gitlab.io` host) is what
  makes the browser *render* the HTML. That detail is the difference between a
  working preview and a downloaded file — see the gotcha below.

`auto_stop_in: 1 hour` tells GitLab to retire the environment automatically, and
`on_stop: stop_review` names the teardown job.

> If you've ever hardcoded that artifact URL with the literal hostname and left
> yourself a "replace with variables" TODO — `${CI_PAGES_URL}` is the variable.
> For a project at `group/subgroup/project` it expands to exactly
> `https://group.gitlab.io/-/subgroup/project`, so the composed URL matches the
> hand-written one character-for-character.

## Step 4 — the stop job

`on_stop` needs a real job to point at. It does almost nothing — its existence
and `action: stop` are what let GitLab (and the manual "Stop environment"
button) close the environment:

```yaml
stop_review:
  stage: .post
  script:
    - echo "Stopping review environment for ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
  rules:
    - if: $CI_MERGE_REQUEST_IID
      when: manual
  environment:
    name: "Review page - ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
    action: stop
```

The `environment.name` **must match** the review job's exactly, or GitLab won't
know which environment this job stops.

## The full thing

```yaml
stages:
  - build
  - deploy

build-page:
  stage: build
  script:
    - mkdir -p public
    - ./generate_report.sh > public/index.html
  artifacts:
    paths:
      - public
    expire_in: 1 day
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
    - if: $CI_MERGE_REQUEST_IID

# Real Pages site on the default branch
pages:
  stage: deploy
  script:
    - echo "Publishing to ${CI_PAGES_URL}"
  needs:
    - job: build-page
      optional: true
  artifacts:
    paths:
      - public
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  environment:
    name: "Production page - $CI_DEFAULT_BRANCH"
    url: "$CI_PAGES_URL"

# Per-MR preview served from job artifacts
pages:review:
  stage: deploy
  script:
    - echo "Creating review environment for ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
  needs:
    - build-page
  artifacts:
    paths:
      - public
  rules:
    - if: $CI_MERGE_REQUEST_IID
  environment:
    name: "Review page - ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
    url: "${CI_PAGES_URL}/-/jobs/${CI_JOB_ID}/artifacts/public/index.html"
    auto_stop_in: 1 hour
    on_stop: stop_review

stop_review:
  stage: .post
  script:
    - echo "Stopping review environment for ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
  rules:
    - if: $CI_MERGE_REQUEST_IID
      when: manual
  environment:
    name: "Review page - ${CI_MERGE_REQUEST_SOURCE_BRANCH_NAME}"
    action: stop
```

Open an MR and the pipeline produces a **View app** button linking to that MR's
freshly built page. The default branch keeps publishing the real site at the
Pages root, so reviewers can compare the two side by side.

## Gotchas

- **Pages must be enabled** for the project, even though the preview rides on
  artifacts rather than a published Pages site — the artifact-serving URL lives
  on the Pages domain. No `pages` job has to have run yet; the domain just needs
  to exist.
- **It only renders inline on the Pages domain.** The same artifact reached via
  `$CI_JOB_URL/artifacts/...` (the main `gitlab.com` host) is served with a
  download disposition for HTML, so the browser saves the file instead of
  showing it. The `${CI_PAGES_URL}/-/jobs/...` form is sandboxed on the Pages
  domain and renders inline. This is *the* reason the URL is built the way it is.
- **`environment.name` must match between the review job and its `stop` job**,
  character for character — including the branch-name interpolation. A mismatch
  silently breaks teardown.
- **Link to a file, not a directory.** Point at `.../public/index.html`, not
  `.../public/`; the artifact browser doesn't do directory-index redirects.
- **`expire_in` is your cleanup.** When the artifact expires the preview 404s,
  which is fine — `auto_stop_in` closes the environment in parallel. Keep the
  two roughly aligned so the button doesn't outlive the page it points to.
- **Free-plan reality check.** This exists precisely because you *can't* publish
  a second Pages site on Free. If you're on a tier/version with parallel
  deployments (`pages.path_prefix`), that's the cleaner route — but this one
  works everywhere and costs nothing.

## Recap

| Piece | Key | Does what |
|---|---|---|
| Build | `artifacts.paths: [public]` + `expire_in` | Renders the page and ships it as a disposable artifact |
| Real site | `pages` job + `url: $CI_PAGES_URL` | Publishes the default branch as the baseline |
| MR preview | `environment.url: ${CI_PAGES_URL}/-/jobs/${CI_JOB_ID}/artifacts/public/index.html` | Serves *this MR's* artifact inline; produces the **View app** button |
| Lifetime | `auto_stop_in` + `on_stop` + `action: stop` | Auto-retires the environment; manual stop button too |
| Scope | `rules: $CI_MERGE_REQUEST_IID` | Preview on MRs, real publish on the default branch |

A handful of lines, no paid tier, no extra infrastructure — and every reviewer
gets a real, rendered page to click instead of a diff to imagine. For anything
your pipeline can emit as HTML — docs, a static site, a generated report, a
dashboard snapshot — it's about the cheapest review-quality upgrade you can add.
