# Google login and sign-up: the stock source flows, plus the same second factor the sign-up asks for
# (broker-enrollment-mfa), before the user is logged in. Without it a Google login is the whole login, and skips the
# passkey or authenticator app. A user with neither has to set one up first.
resource "authentik_flow" "source_authentication" {
  name               = "Broker Google login"
  slug               = "broker-source-authentication"
  title              = "Sign in"
  designation        = "authentication"
  authentication     = "require_unauthenticated"
  layout             = "stacked"
  policy_engine_mode = "any"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [background, compatibility_mode]
  }
}

import {
  to = authentik_flow.source_authentication
  id = "broker-source-authentication"
}

resource "authentik_flow" "source_enrollment" {
  name               = "Broker Google sign-up"
  slug               = "broker-source-enrollment"
  title              = "Choose a username"
  designation        = "enrollment"
  authentication     = "none"
  layout             = "stacked"
  policy_engine_mode = "any"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [background, compatibility_mode]
  }
}

import {
  to = authentik_flow.source_enrollment
  id = "broker-source-enrollment"
}

# Google as a federated login source. A Google user is matched to an existing account by email.
resource "authentik_source_oauth" "google" {
  name                = "Google"
  slug                = "google"
  enabled             = true
  provider_type       = "google"
  consumer_key        = var.google_client_id
  consumer_secret     = var.google_client_secret
  authentication_flow = authentik_flow.source_authentication.uuid
  enrollment_flow     = authentik_flow.source_enrollment.uuid
  user_matching_mode  = "email_link"
  user_path_template  = "goauthentik.io/sources/%(slug)s"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [pkce, authorization_url, access_token_url, profile_url, oidc_jwks_url]
  }
}

import {
  to = authentik_source_oauth.google
  id = "google"
}
