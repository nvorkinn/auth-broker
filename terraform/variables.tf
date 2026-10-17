# Input variables. Secret values come from TF_VAR_* environment variables set from GitHub secrets, never a committed .tfvars.
# An unset secret reaches Terraform as an empty string, which would blank the credential in Authentik, so each must be non-empty.
variable "authentik_url" {
  type        = string
  description = "Base URL of the Authentik server."
  default     = "https://sso.nikolaivorkinn.com"
}

variable "google_client_id" {
  type        = string
  description = "OAuth client id of the Google login source."
  sensitive   = true

  validation {
    condition     = length(var.google_client_id) > 0
    error_message = "google_client_id must not be empty: set the TF_VAR_GOOGLE_CLIENT_ID secret."
  }
}

variable "google_client_secret" {
  type        = string
  description = "OAuth client secret of the Google login source."
  sensitive   = true

  validation {
    condition     = length(var.google_client_secret) > 0
    error_message = "google_client_secret must not be empty: set the TF_VAR_GOOGLE_CLIENT_SECRET secret."
  }
}

variable "logs_client_secret" {
  type        = string
  description = "OAuth client secret of the Logs forward-auth provider."
  sensitive   = true

  validation {
    condition     = length(var.logs_client_secret) > 0
    error_message = "logs_client_secret must not be empty: set the TF_VAR_LOGS_CLIENT_SECRET secret."
  }
}

variable "metrics_client_secret" {
  type        = string
  description = "OAuth client secret of the Metrics forward-auth provider."
  sensitive   = true

  validation {
    condition     = length(var.metrics_client_secret) > 0
    error_message = "metrics_client_secret must not be empty: set the TF_VAR_METRICS_CLIENT_SECRET secret."
  }
}
