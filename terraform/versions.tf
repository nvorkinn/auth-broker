# Terraform itself is pinned here and in TF_VERSION in .github/workflows/terraform.yml; bump both together.
# The Authentik provider's versions follow Authentik's own (2026.8.x here), and it only talks to the same
# release, so bump it with AUTHENTIK_VERSION in .env and docker-compose.yml.
terraform {
  required_version = "~> 1.16.0"

  required_providers {
    authentik = {
      source  = "goauthentik/authentik"
      version = "~> 2026.8.0"
    }
    # Looks up the ids that imports.tf needs, see scripts/authentik_ids.py.
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
