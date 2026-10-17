# Input variables. Secret values come from TF_VAR_* environment variables set from GitHub secrets, never a committed .tfvars.
variable "authentik_url" {
  type        = string
  description = "Base URL of the Authentik server."
  default     = "https://sso.nikolaivorkinn.com"
}

variable "google_client_id" {
  type        = string
  description = "OAuth client id of the Google login source."
  sensitive   = true
}

variable "google_client_secret" {
  type        = string
  description = "OAuth client secret of the Google login source."
  sensitive   = true
}

variable "logs_client_secret" {
  type        = string
  description = "OAuth client secret of the Logs forward-auth provider."
  sensitive   = true
}

variable "metrics_client_secret" {
  type        = string
  description = "OAuth client secret of the Metrics forward-auth provider."
  sensitive   = true
}
