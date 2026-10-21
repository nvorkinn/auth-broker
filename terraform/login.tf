# The login screen, headed "Sign in" rather than the stock "Welcome to authentik!" (a flow needs a title, it can't
# be blank). Authentik's own blueprint sets it back if a later Authentik release changes that blueprint; a plan
# shows that as drift and an apply undoes it.
resource "authentik_flow" "authentication" {
  name        = "Welcome to authentik!"
  slug        = "default-authentication-flow"
  title       = "Sign in"
  designation = "authentication"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [background, compatibility_mode]
  }
}

import {
  to = authentik_flow.authentication
  id = "default-authentication-flow"
}

# Passkey login: a WebAuthn-only validation stage on the login screen's identification stage.
resource "authentik_stage_authenticator_validate" "passkey" {
  name                          = "default-authentication-passkey-validation"
  device_classes                = ["webauthn"]
  not_configured_action         = "deny"
  last_auth_threshold           = "seconds=0"
  webauthn_user_verification    = "required"
  webauthn_allowed_device_types = []
  webauthn_hints                = []
}

import {
  to = authentik_stage_authenticator_validate.passkey
  id = local.ids.stages["default-authentication-passkey-validation"]
}

resource "authentik_stage_authenticator_webauthn" "setup" {
  name                     = "default-authenticator-webauthn-setup"
  resident_key_requirement = "required"
  user_verification        = "required"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [configure_flow, friendly_name]
  }
}

import {
  to = authentik_stage_authenticator_webauthn.setup
  id = local.ids.stages["default-authenticator-webauthn-setup"]
}

# Show Google on the login screen: the source alone only exists, the identification stage decides which sources
# it lists.
resource "authentik_stage_identification" "authentication" {
  name              = "default-authentication-identification"
  user_fields       = ["email", "username"]
  recovery_flow     = authentik_flow.recovery.uuid
  enrollment_flow   = authentik_flow.enrollment.uuid
  webauthn_stage    = authentik_stage_authenticator_validate.passkey.id
  passwordless_flow = authentik_flow.passwordless.uuid
  sources           = [authentik_source_oauth.google.uuid]

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [case_insensitive_matching]
  }
}

import {
  to = authentik_stage_identification.authentication
  id = local.ids.stages["default-authentication-identification"]
}

# Brands: point both at the login and recovery flows; nikolaivorkinn.com carries the branding. A brand naming no
# authentication flow falls back to the first flow with that designation whose policies pass, by slug, so a flow that
# sorts first and has no policies (broker-passwordless, in passwordless.tf) would become the login screen. Naming the
# flow here keeps that from happening.
resource "authentik_brand" "default" {
  domain              = "authentik-default"
  flow_authentication = authentik_flow.authentication.uuid
  flow_recovery       = authentik_flow.recovery.uuid

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [branding_favicon, branding_logo, default, flow_invalidation, flow_user_settings]
  }
}

import {
  to = authentik_brand.default
  id = local.ids.brands["authentik-default"]
}

# The sign-in's look: the wallpaper and the CSS that draws the login box as the Countdown screen, both shipped in
# authentik-assets/ (served at /static/dist/assets/countdown/, deployed with the authentik service). The CSS also
# puts the wallpaper behind every flow, whatever background the flow names. Authentik keeps the CSS without its
# final newline, so it's chomped here, or every plan would show it changed.
resource "authentik_brand" "nikolaivorkinn" {
  domain                           = "nikolaivorkinn.com"
  branding_title                   = "Countdown"
  branding_logo                    = "/static/dist/assets/countdown/countdown-pill.svg"
  branding_favicon                 = "countdown-favicon.svg.svg"
  branding_default_flow_background = "/static/dist/assets/countdown/wallpaper.jpg"
  branding_custom_css              = chomp(file("${path.module}/../authentik-assets/countdown.css"))
  flow_authentication              = authentik_flow.authentication.uuid
  flow_recovery                    = authentik_flow.recovery.uuid
}

import {
  to = authentik_brand.nikolaivorkinn
  id = local.ids.brands["nikolaivorkinn.com"]
}
