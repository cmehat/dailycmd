---
layout: post
title: "A serverless reset button: browser-only OAuth to the GCP Compute API"
date: 2025-10-14 15:00:00 +0000
categories: [gcp, frontend]
tags: [gcp, oauth, compute-engine, gitlab-pages, iam, static-site, security]
---

Our infrastructure status page is a **static** site on GitLab Pages: a table of
VMs generated from Terraform state, no backend, no server-side code. It was
useful for looking, but every time a box wedged, the loop was: open the page,
copy the instance name, switch to a terminal, `gcloud compute instances reset …`.
I wanted a **Reset** button right there in the table.

The constraint: static hosting means there is nowhere to run code that holds a
credential. So the credential has to come from the person clicking, in their
browser, at click time. That turns out to be entirely doable with Google Identity
Services and the Compute API — and the interesting part is doing it *safely*.

## The idea

When you click, the browser runs the Google OAuth flow, gets a short-lived
access token for the signed-in user, and calls the Compute API's
[`instances.reset`](https://cloud.google.com/compute/docs/reference/rest/v1/instances/reset)
directly with `fetch`. No token is ever stored; it lives only in that closure,
for that one call.

```javascript
// Initialise the Google Identity Services token client once.
const tokenClient = google.accounts.oauth2.initTokenClient({
  client_id: config.clientId,
  scope: "https://www.googleapis.com/auth/cloud-platform",
  callback: "", // set per-request below
});

async function resetInstance({ project, zone, instance }, button) {
  button.disabled = true;

  // 1. Get an access token for the signed-in user (opens the consent popup).
  const token = await new Promise((resolve, reject) => {
    tokenClient.callback = (resp) => (resp.error ? reject(resp) : resolve(resp));
    tokenClient.requestAccessToken({ prompt: "consent" });
  });

  // 2. Call the Compute API directly from the browser.
  const url = `https://compute.googleapis.com/compute/v1/projects/${project}` +
              `/zones/${zone}/instances/${instance}/reset`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token.access_token}` },
  });

  if (!resp.ok) throw new Error(`API ${resp.status}: ${(await resp.json()).error.message}`);
  alert(`Reset command sent to ${instance}.`);
  button.disabled = false;
}
```

That's the whole mechanism. The static page loads the GIS script, initialises the
client with your OAuth **client ID**, and renders one button per row wired to the
row's `{project, zone, instance}`.

## The part that makes it safe: IAM, not the token scope

The access token above is broad — `cloud-platform`. On its own that's alarming.
What actually constrains it is **IAM on the Google Cloud side**: the token can
only do what the *signed-in identity* is allowed to do. So you grant a tightly
scoped custom role to exactly the people who should be able to reset boxes, and
nobody else's token will work — the API returns 403.

```hcl
resource "google_project_iam_custom_role" "instance_rebooter" {
  role_id     = "instanceRebooterRole"
  title       = "Instance Reboot and View"
  permissions = [
    "compute.instances.reset",
    "compute.instances.get",
    "compute.instances.list",
  ]
}

resource "google_project_iam_member" "rebooter_group" {
  project = var.project_id
  role    = google_project_iam_custom_role.instance_rebooter.id
  member  = "group:rebooters@example.com" # a Google Group, not individuals
}
```

Three permissions, bound to a Google Group. Reset, get, list — nothing else.
Adding or removing someone is a group-membership change, not a Terraform apply.

The other half is the OAuth client configuration in the Cloud console: the
**authorized JavaScript origin** must be your Pages domain, so a token for this
client can only be minted from a page served there.

## Why this is neat — and why I did *not* ship it in the reusable template

This is genuinely satisfying: a real, authenticated, least-privilege cloud
mutation from a static page with no backend, no secrets in the repo, and no
long-lived credentials anywhere. The token is per-click and short-lived; the blast
radius is exactly three permissions for a named group.

But when I later factored the CI and Pages tooling into reusable templates for
other projects, I deliberately **left this out** of the shared template. A few
reasons:

- **It needs a per-deployment OAuth client** (client ID, authorized origins)
  configured out-of-band — not something a template can carry.
- **It's a live mutation surface on a public-ish page.** Safe *because* of IAM,
  but "a button that reboots production if you're in the group" is a thing you
  want each team to opt into consciously, not inherit silently.
- **The console already does this**, with an audit trail and its own auth. A
  bespoke button is worth it for a bespoke status page, not as a default everyone
  gets.

So it stayed a one-off on the status page it was built for, and the reusable
template just links each row to the GCP console instead. That felt like the right
line: ship the convenience where it was designed and understood, keep it out of
the thing other people copy blindly.

The lesson I kept: "static site" doesn't mean "read-only." With browser OAuth and
server-side IAM, a static page can safely drive real infrastructure — as long as
the authorization lives in the cloud, not in the page.
