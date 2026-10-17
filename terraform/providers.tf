# The API token comes from the AUTHENTIK_TOKEN environment variable, never from a file or a variable.
provider "authentik" {
  url = var.authentik_url
}
