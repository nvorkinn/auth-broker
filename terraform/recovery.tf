# Recovery flow: email link, then TOTP or WebAuthn, then a new password. Stock objects that this repo customises.
resource "authentik_flow" "recovery" {
  name               = "Default recovery flow"
  slug               = "default-recovery-flow"
  title              = "Reset your password"
  designation        = "recovery"
  authentication     = "require_unauthenticated"
  layout             = "stacked"
  policy_engine_mode = "any"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [background, compatibility_mode]
  }
}

import {
  to = authentik_flow.recovery
  id = "default-recovery-flow"
}

resource "authentik_stage_prompt_field" "recovery_password" {
  name        = "default-recovery-field-password"
  field_key   = "password"
  label       = "Password"
  type        = "password"
  required    = true
  placeholder = "Password"
  order       = 0
}

import {
  to = authentik_stage_prompt_field.recovery_password
  id = local.ids.prompts["default-recovery-field-password"]
}

resource "authentik_stage_prompt_field" "recovery_password_repeat" {
  name        = "default-recovery-field-password-repeat"
  field_key   = "password_repeat"
  label       = "Password (repeat)"
  type        = "password"
  required    = true
  placeholder = "Password (repeat)"
  order       = 1
}

import {
  to = authentik_stage_prompt_field.recovery_password_repeat
  id = local.ids.prompts["default-recovery-field-password-repeat"]
}

resource "authentik_policy_expression" "recovery_skip_if_restored" {
  name       = "default-recovery-skip-if-restored"
  expression = <<-EOT
    return bool(request.context.get('is_restored', True))
  EOT
}

import {
  to = authentik_policy_expression.recovery_skip_if_restored
  id = local.ids.policies["default-recovery-skip-if-restored"]
}

resource "authentik_stage_email" "recovery" {
  name                     = "default-recovery-email"
  use_global_settings      = true
  subject                  = "Password reset"
  template                 = "email/password_reset.html"
  token_expiry             = "minutes=30"
  activate_user_on_success = false

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [timeout]
  }
}

import {
  to = authentik_stage_email.recovery
  id = local.ids.stages["default-recovery-email"]
}

resource "authentik_stage_authenticator_validate" "recovery" {
  name                       = "default-recovery-mfa"
  device_classes             = ["totp", "webauthn"]
  not_configured_action      = "deny"
  last_auth_threshold        = "seconds=0"
  webauthn_user_verification = "preferred"
}

import {
  to = authentik_stage_authenticator_validate.recovery
  id = local.ids.stages["default-recovery-mfa"]
}

resource "authentik_stage_user_write" "recovery" {
  name               = "default-recovery-user-write"
  user_creation_mode = "never_create"
  user_type          = "external"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [create_users_as_inactive]
  }
}

import {
  to = authentik_stage_user_write.recovery
  id = local.ids.stages["default-recovery-user-write"]
}

resource "authentik_stage_identification" "recovery" {
  name                      = "default-recovery-identification"
  user_fields               = ["email", "username"]
  case_insensitive_matching = true
  pretend_user_exists       = true
  show_matched_user         = true
}

import {
  to = authentik_stage_identification.recovery
  id = local.ids.stages["default-recovery-identification"]
}

resource "authentik_stage_user_login" "recovery" {
  name = "default-recovery-user-login"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [geoip_binding, network_binding]
  }
}

import {
  to = authentik_stage_user_login.recovery
  id = local.ids.stages["default-recovery-user-login"]
}

resource "authentik_stage_prompt" "recovery" {
  name = "default-recovery-prompt-password"
  fields = [
    authentik_stage_prompt_field.recovery_password.id,
    authentik_stage_prompt_field.recovery_password_repeat.id,
  ]
  validation_policies = [local.ids.policies["default-password-change-password-policy"]]
}

import {
  to = authentik_stage_prompt.recovery
  id = local.ids.stages["default-recovery-prompt-password"]
}
