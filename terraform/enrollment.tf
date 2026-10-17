# Sign-up: username, email and password, then a link emailed to that address that has to be followed, then a second
# factor that has to be set up (a passkey or an authenticator app) before the account logs in. A passkey also lets
# that user sign in later without typing a username. Only offered to a visitor who was sent to sign in from the
# broker's /pair page: the gate reads where the login will return to. That keeps the sign-up off the Logs and
# Metrics logins and off the login page in general, but it isn't a security boundary, as anyone can forge the
# query. What keeps a new account out of Logs and Metrics is their group bindings. A new user is a plain internal
# user in no group, which the broker accepts.
resource "authentik_flow" "enrollment" {
  name               = "Broker sign-up"
  slug               = "broker-enrollment"
  title              = "Create your account"
  designation        = "enrollment"
  authentication     = "require_unauthenticated"
  layout             = "stacked"
  policy_engine_mode = "any"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [background, compatibility_mode]
  }
}

import {
  to = authentik_flow.enrollment
  id = "broker-enrollment"
}

resource "authentik_policy_expression" "enrollment_gate" {
  name       = "broker-enrollment-from-broker"
  expression = file("${path.module}/policies/broker-enrollment-from-broker.expression")
}

import {
  to = authentik_policy_expression.enrollment_gate
  id = local.ids.policies["broker-enrollment-from-broker"]
}

locals {
  enrollment_fields = {
    username        = { label = "Username", type = "username", order = 0 }
    email           = { label = "Email", type = "email", order = 1 }
    password        = { label = "Password", type = "password", order = 2 }
    password_repeat = { label = "Password (repeat)", type = "password", order = 3 }
  }
}

resource "authentik_stage_prompt_field" "enrollment" {
  for_each = local.enrollment_fields

  name        = "broker-enrollment-field-${replace(each.key, "_", "-")}"
  field_key   = each.key
  label       = each.value.label
  type        = each.value.type
  required    = true
  placeholder = each.value.label
  order       = each.value.order
}

import {
  for_each = local.enrollment_fields

  to = authentik_stage_prompt_field.enrollment[each.key]
  id = local.ids.prompts["broker-enrollment-field-${replace(each.key, "_", "-")}"]
}

resource "authentik_stage_prompt" "enrollment" {
  name = "broker-enrollment-prompt"
  fields = [
    for key in ["username", "email", "password", "password_repeat"] : authentik_stage_prompt_field.enrollment[key].id
  ]
  validation_policies = [local.ids.policies["default-password-change-password-policy"]]
}

import {
  to = authentik_stage_prompt.enrollment
  id = local.ids.stages["broker-enrollment-prompt"]
}

resource "authentik_stage_user_write" "enrollment" {
  name               = "broker-enrollment-user-write"
  user_creation_mode = "always_create"
  user_type          = "internal"
  # Inactive until the emailed link is followed (the email stage activates it).
  create_users_as_inactive = true
}

import {
  to = authentik_stage_user_write.enrollment
  id = local.ids.stages["broker-enrollment-user-write"]
}

# Email verification: a link goes to the address given, and the account only becomes active when it is followed.
# The SMTP settings are Authentik's global ones (AUTHENTIK_EMAIL__* in docker-compose.yml, with the password in
# secrets/authentik.env), the same ones the recovery flow uses.
resource "authentik_stage_email" "enrollment" {
  name                     = "broker-enrollment-email"
  use_global_settings      = true
  subject                  = "Confirm your email"
  template                 = "email/account_confirmation.html"
  token_expiry             = "minutes=30"
  activate_user_on_success = true

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [timeout]
  }
}

import {
  to = authentik_stage_email.enrollment
  id = local.ids.stages["broker-enrollment-email"]
}

# A second factor is mandatory: with none set up, the user has to set up a passkey or an authenticator app now (the
# stock setup stages), and can't carry on without one. The Google login's flows use this stage too.
resource "authentik_stage_authenticator_validate" "enrollment_mfa" {
  name                       = "broker-enrollment-mfa"
  device_classes             = ["webauthn", "totp"]
  not_configured_action      = "configure"
  last_auth_threshold        = "seconds=0"
  webauthn_user_verification = "required"
  configuration_stages = [
    authentik_stage_authenticator_webauthn.setup.id,
    local.ids.stages["default-authenticator-totp-setup"],
  ]
}

import {
  to = authentik_stage_authenticator_validate.enrollment_mfa
  id = local.ids.stages["broker-enrollment-mfa"]
}

resource "authentik_stage_user_login" "enrollment" {
  name = "broker-enrollment-user-login"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [geoip_binding, network_binding]
  }
}

import {
  to = authentik_stage_user_login.enrollment
  id = local.ids.stages["broker-enrollment-user-login"]
}
