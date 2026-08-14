from __future__ import annotations

from typing import Any


def gcp_terraform_files(intent: dict[str, Any], diff: dict[str, Any] | None = None) -> dict[str, str]:
    cloud = intent.get("cloud") or {}
    deployment = intent.get("deployment") or {}
    network = intent.get("network") or {}
    topology = cloud.get("topology", "gke")
    project = cloud.get("project_id", "sandbox-project")
    region = cloud.get("region", "asia-south1")
    bucket = cloud.get("state_backend", f"{network.get('name', 'fabric')}-tfstate")
    name = network.get("name", "fabric-network")
    node_count = int(deployment.get("node_count", deployment.get("vm_count", 2)))
    machine = deployment.get("machine_type", "e2-standard-4")
    version = deployment.get("adapter_version", "latest")
    changed = sorted((diff or {}).get("changed_paths", []))
    provider = f'''terraform {{
  required_version = ">= 1.6.0"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 5.0"
    }}
  }}
  backend "gcs" {{
    bucket = "{bucket}"
    prefix = "nfh-fabric/{name}"
  }}
}}

provider "google" {{
  project = "{project}"
  region  = "{region}"
}}
'''
    if topology == "vm":
        main = f'''resource "google_compute_network" "fabric" {{
  name                    = "{name}-vpc"
  auto_create_subnetworks = false
}}

resource "google_compute_instance" "fabric" {{
  count        = {node_count}
  name         = "{name}-node-${{count.index}}"
  machine_type = "{machine}"
  zone         = "{region}-a"
  labels       = {{ adapter_version = "{version}" }}
  boot_disk {{ initialize_params {{ image = "ubuntu-os-cloud/ubuntu-2204-lts" }} }}
  network_interface {{ network = google_compute_network.fabric.id }}
}}
'''
    else:
        main = f'''resource "google_container_cluster" "fabric" {{
  name               = "{name}-gke"
  location           = "{region}"
  networking_mode    = "VPC_NATIVE"
  initial_node_count = 1
}}

resource "google_container_node_pool" "fabric" {{
  name       = "fabric-pool"
  cluster    = google_container_cluster.fabric.name
  location   = google_container_cluster.fabric.location
  node_count = {node_count}
  node_config {{
    machine_type = "{machine}"
    labels       = {{ adapter_version = "{version}" }}
  }}
}}
'''
    return {
        "providers.tf": provider,
        "main.tf": main,
        "upgrade.diff.json": "{\n  \"changed_paths\": " + repr(changed).replace("'", '"') + "\n}\n",
        "README.md": "GCP-only Terraform with GCS remote state. Incremental upgrades are driven by changed intent paths.\n",
    }

