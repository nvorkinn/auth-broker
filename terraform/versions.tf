# Terraform itself is pinned here and in TF_VERSION in .github/workflows/terraform.yml; bump both together.
# Providers go in required_providers with a pessimistic constraint (e.g. "~> 6.0"). After adding or
# bumping one, refresh .terraform.lock.hcl for every platform it runs on (see README.md) and commit it.
terraform {
  required_version = "~> 1.16.0"

  required_providers {
  }
}
