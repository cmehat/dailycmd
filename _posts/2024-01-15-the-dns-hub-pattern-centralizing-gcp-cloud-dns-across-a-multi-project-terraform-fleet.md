---
layout: post
title: "The DNS Hub Pattern: Centralizing GCP Cloud DNS Across a Multi-Project Terraform Fleet"
date: 2024-01-15 09:00:00 +0000
permalink: /2024/01/the-dns-hub-pattern-centralizing-gcp-cloud-dns-across-a-multi-project-terraform-fleet.html
tags: ["terraform", "gcp", "dns", "cloud-dns", "dnssec", "multi-project", "infrastructure", "iac", "gitops"]
author: "cm"
---

When you manage more than one Terraform project against the same GCP organization, DNS quickly becomes a coordination problem. Every compute project wants to create DNS records. If each one manages its own zones, you end up with zones scattered across projects, DNSSEC configured inconsistently (or not at all), and no single place to answer "where does `foo.example.com` live?"

The fix is a small, dedicated Terraform project whose only job is to own the zones. Every other project refers to them by name. This post walks through the pattern, what the hub project manages, and how two different consumer shapes — a direct-compute fleet and a shared module — reference it.

I'll use generic domain names and project identifiers throughout. The shape is what matters.

## The problem with distributed zone ownership

Consider a fleet of GCP compute projects:

- `tf-compute-fleet` — deploys VMs running your workloads
- `tf-k8s-cluster` — manages your GKE cluster
- `tf-gcp-sandbox` — provisions short-lived dev/test machines

Each of them needs to register DNS records: `vm-1.example.com`, `cluster-ingress.example.com`, `dev-sandbox-alice.example.com`. The naive approach gives each project a `google_dns_managed_zone` resource and the ability to create records in it.

This breaks down fast:

- **DNSSEC** requires the zone to exist before the key signing keys can be configured at the registrar. If three projects can recreate the zone, the DNSSEC chain breaks whenever one of them does a `terraform destroy` + `terraform apply` cycle.
- **Cross-project record creation** requires that the project creating the record has IAM permission on the project hosting the zone. Keeping these permissions tidy across a growing fleet is its own lifecycle problem.
- **Private zones** and VPC peering visibility require explicit `private_visibility_config` blocks that reference specific VPC network self-links. Those self-links come from whichever project owns the network — usually not the same one that owns the zone.

A dedicated hub solves all of this by making the zone the single authoritative resource, and every consumer a reader/writer.

## What the hub project manages

The hub is a focused Terraform project with one concern: GCP Cloud DNS. Ours manages four zones:

| Zone name (in `google_dns_managed_zone`) | DNS name | Visibility | DNSSEC |
|---|---|---|---|
| `example-a-com` | `example-a.com.` | public | enabled |
| `example-b-com` | `example-b.com.` | public | enabled |
| `example-org-eu` | `example-org.eu.` | public | enabled |
| `corp-internal` | `corp.internal.` | private | n/a |

For each **public zone**, the pattern is:

```hcl
resource "google_dns_managed_zone" "example_a_com" {
  name        = "example-a-com"
  dns_name    = "example-a.com."
  description = "[TF] DNS zone for example-a.com"

  labels = {
    deployment = "terraform"
    project    = "dns-hub"
    registrar  = "your-registrar"
  }

  dnssec_config {
    kind          = "dns#managedZoneDnsSecConfig"
    non_existence = "nsec3"
    state         = "on"
  }
}
```

Two things worth noting about the DNSSEC block:

- All three fields (`kind`, `non_existence`, `state`) force replacement on change. Set them once and don't touch them — or accept a destroy/recreate and a DNSSEC chain break at the registrar.
- `nsec3` is preferred over `nsec` because it prevents zone walking (enumeration of all names in the zone). For a public zone under active use, this matters.

For each zone, the hub also creates a **dedicated VPC network** and a baseline **firewall rule**. The VPC is what other projects reference by self-link when they need to attach an instance to a zone's network or configure private DNS peering:

```hcl
resource "google_compute_network" "example_a_com" {
  name                    = "network-example-a-com"
  auto_create_subnetworks = false
}

resource "google_compute_firewall" "example_a_com" {
  name        = "network-example-a-com-firewall"
  network     = google_compute_network.example_a_com.name
  description = "[TF] Baseline firewall for example-a-com network"

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["web"]
}
```

The **private zone** is configured differently. It has no DNSSEC (meaningless for private zones) and must explicitly enumerate which VPC networks can resolve names in it:

```hcl
resource "google_dns_managed_zone" "corp_internal" {
  name        = "corp-internal"
  dns_name    = "corp.internal."
  description = "[TF] Private DNS zone for internal service discovery"
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.network_a.id
    }
    networks {
      network_url = google_compute_network.network_b.id
    }
  }
}
```

The `network_url` values here reference networks within the hub project itself. If you need a VM in a *different* project's VPC to resolve `corp.internal.` names, you'd add that VPC's network self-link here — which means the hub needs a cross-project IAM binding on `roles/dns.admin` for the consumer project. More on this below.

## State backend

The hub uses a GCS backend so its state is available to other projects via `terraform_remote_state` if they need to fetch output values:

```hcl
terraform {
  backend "gcs" {
    bucket = "my-tfstate-bucket"
    prefix = "dns-hub"
  }
}
```

The bucket itself is managed by a separate bootstrap project (or created manually for the first time). The hub does not manage its own backend bucket — that's a circularity you want to avoid.

## Consumer shape 1: direct compute fleet

A VM project that needs to register records in zones the hub manages doesn't own the zone — it just creates records in it. This requires:

1. IAM: the VM project's Terraform SA needs `roles/dns.admin` on the hub project (or the more granular `roles/dns.resourceRecordSetsAdmin` if you want to restrict to record creation only).
2. A `data` source to look up the managed zone by name.
3. A `google_dns_record_set` resource pointing at it.

```hcl
data "google_dns_managed_zone" "example_a_com" {
  provider = google
  project  = "my-dns-hub-project"
  name     = "example-a-com"
}

resource "google_dns_record_set" "vm_web" {
  provider     = google
  project      = "my-dns-hub-project"
  managed_zone = data.google_dns_managed_zone.example_a_com.name
  name         = "vm-1.${data.google_dns_managed_zone.example_a_com.dns_name}"
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_instance.vm_web.network_interface[0].access_config[0].nat_ip]
}
```

The provider block for the hub project needs explicit `project` override if your default provider is configured for the compute project:

```hcl
provider "google" {
  alias   = "dns_hub"
  project = "my-dns-hub-project"
  region  = "us-central1"
}
```

For the VPC network, the compute project references the hub's network by self-link — a stable URL that doesn't change unless the resource is destroyed and recreated:

```hcl
# Self-links are stable: projects/{project}/global/networks/{name}
network_self_link = "https://www.googleapis.com/compute/v1/projects/my-dns-hub-project/global/networks/network-example-a-com"
```

Hard-coding self-links across projects is fine as long as the naming convention is locked. The alternative (a `terraform_remote_state` data source) is cleaner but introduces a dependency on the hub's state file being accessible, which complicates CI ordering. For a stable, rarely-changing network name, the self-link string wins on simplicity.

## Consumer shape 2: a shared Terraform module

When many compute projects share the same pattern — attach to a hub-managed network, register a DNS record, run a startup script, add SSH keys — the repetition calls for a module. The module accepts the zone name as a string parameter and handles the zone lookup and record creation internally:

```hcl
module "my_vm" {
  source  = "your-registry/compute-instance/gcp"
  version = "x.y.z"

  instance_name  = "web-server-1"
  machine_type   = "e2-standard-2"
  image          = "ubuntu-minimal-2404-noble-amd64-v20241116"

  # Which hub-managed zone to register the DNS record in.
  # Valid values match the zone `name` fields in the dns-hub project.
  dns_zone_name = "example-a-com"

  # Optionally override the record name (defaults to instance_name).
  use_custom_record_name = true
  custom_record_name     = "web-server-prod"
}
```

Inside the module, the zone lookup and record creation are parameterized by that `dns_zone_name` string:

```hcl
variable "dns_zone_name" {
  description = "Name of the GCP Cloud DNS managed zone (owned by the dns-hub project) in which to create the instance's DNS record."
  type        = string
  default     = "example-org-eu"
}

data "google_dns_managed_zone" "selected" {
  project = var.dns_hub_project
  name    = var.dns_zone_name
}

resource "google_dns_record_set" "instance" {
  project      = var.dns_hub_project
  managed_zone = data.google_dns_managed_zone.selected.name
  name         = "${local.record_name}.${data.google_dns_managed_zone.selected.dns_name}"
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_instance.instance.network_interface[0].access_config[0].nat_ip]
}
```

The module also takes `network_self_link` and `subnetwork_self_link` to attach the instance to the hub's VPC, keeping all instances in the same zone on the same network and making private DNS resolution consistent.

The contract between the module and the hub is deliberately loose: the hub's zone names are a stable enum, and the module accepts one of them as a string. There's no hard Terraform resource dependency, no remote state reference, no provider alias threading. The coupling is just a naming convention — which is easy to validate in CI and easy to reason about.

## CI ordering

Because the hub has no Terraform dependencies on any consumer project, it can (and should) run in its own pipeline, independently. The consumer pipelines depend on the hub only at the IAM level — if the hub hasn't been applied yet, the consumer's DNS record creation will fail with a 404 on the zone lookup.

In practice the hub is a slow-moving project: zones are created once, DNSSEC is configured, and then it barely changes. Pinning it to its own pipeline with a manual `terraform apply` gate is sufficient. Consumer pipelines run on their own cadence and fail loudly if the zone disappears — which is the correct failure mode.

## What this doesn't cover

A few things that are out of scope for the hub pattern as described here:

- **Subdomain delegation**: if consumer projects need their own sub-zones (`team-a.example-a.com.`), you'd add NS records in the hub's zone pointing to a delegated zone in the consumer project. The hub stays authoritative for the apex; consumers own their sub-zones.
- **Record lifecycle management at scale**: if you have hundreds of VMs registering records, consider a more automated approach (GCP Cloud DNS API directly from CI, or a controller in-cluster). Hand-managed `google_dns_record_set` resources across dozens of `.tf` files get unwieldy.
- **Private DNS from GKE**: a GKE cluster in a different project that wants to resolve `corp.internal.` names needs its VPC added to the hub's `private_visibility_config`. This requires the hub to know about the GKE project's VPC self-link, which is a mild coupling — acceptable if the cluster is long-lived, awkward if clusters are ephemeral.

## Summary

The dns-hub pattern is a small architectural choice with outsized leverage: one project, four zones, and a clear IAM boundary. Every other project creates records but doesn't own zones. DNSSEC is configured once and protected from accidental destruction. The VPC networks the hub creates are the shared fabric that connects compute to DNS.

The tradeoff is an extra IAM dependency and a slightly more complex provider configuration in consumer projects. For a fleet of more than two or three Terraform projects pointing at the same domains, it's worth it.
