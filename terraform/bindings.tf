# Which stages each flow runs, in order. A flow's stages are found by where they sit (flow and order), because a
# binding has no name of its own.
locals {
  # Created by the first apply, not imported (see google.tf), so their bindings are created too.
  new_flows = ["broker-source-authentication", "broker-source-enrollment"]

  flows = {
    "default-recovery-flow"        = authentik_flow.recovery
    "broker-enrollment"            = authentik_flow.enrollment
    "broker-source-authentication" = authentik_flow.source_authentication
    "broker-source-enrollment"     = authentik_flow.source_enrollment
  }

  flow_bindings = {
    recovery_identification = { flow = "default-recovery-flow", order = 10, re_evaluate_policies = true }
    recovery_email          = { flow = "default-recovery-flow", order = 20, re_evaluate_policies = true }
    recovery_mfa            = { flow = "default-recovery-flow", order = 21, re_evaluate_policies = true }
    recovery_prompt         = { flow = "default-recovery-flow", order = 30, re_evaluate_policies = false }
    recovery_user_write     = { flow = "default-recovery-flow", order = 40, re_evaluate_policies = false }
    recovery_user_login     = { flow = "default-recovery-flow", order = 100, re_evaluate_policies = false }

    enrollment_prompt     = { flow = "broker-enrollment", order = 10, re_evaluate_policies = false }
    enrollment_user_write = { flow = "broker-enrollment", order = 20, re_evaluate_policies = false }
    enrollment_email      = { flow = "broker-enrollment", order = 25, re_evaluate_policies = false }
    enrollment_mfa        = { flow = "broker-enrollment", order = 30, re_evaluate_policies = false }
    enrollment_user_login = { flow = "broker-enrollment", order = 100, re_evaluate_policies = false }

    source_authentication_mfa   = { flow = "broker-source-authentication", order = 10, re_evaluate_policies = false }
    source_authentication_login = { flow = "broker-source-authentication", order = 100, re_evaluate_policies = false }

    # Asks for a username only when Google didn't give one (see the policy binding on it, below).
    source_enrollment_prompt = { flow = "broker-source-enrollment", order = 10, re_evaluate_policies = true }
    source_enrollment_write  = { flow = "broker-source-enrollment", order = 20, re_evaluate_policies = false }
    source_enrollment_mfa    = { flow = "broker-source-enrollment", order = 30, re_evaluate_policies = false }
    source_enrollment_login  = { flow = "broker-source-enrollment", order = 100, re_evaluate_policies = false }
  }

  flow_binding_stages = {
    recovery_identification = authentik_stage_identification.recovery.id
    recovery_email          = authentik_stage_email.recovery.id
    recovery_mfa            = authentik_stage_authenticator_validate.recovery.id
    recovery_prompt         = authentik_stage_prompt.recovery.id
    recovery_user_write     = authentik_stage_user_write.recovery.id
    recovery_user_login     = authentik_stage_user_login.recovery.id

    enrollment_prompt     = authentik_stage_prompt.enrollment.id
    enrollment_user_write = authentik_stage_user_write.enrollment.id
    enrollment_email      = authentik_stage_email.enrollment.id
    enrollment_mfa        = authentik_stage_authenticator_validate.enrollment_mfa.id
    enrollment_user_login = authentik_stage_user_login.enrollment.id

    source_authentication_mfa   = authentik_stage_authenticator_validate.enrollment_mfa.id
    source_authentication_login = local.ids.stages["default-source-authentication-login"]

    source_enrollment_prompt = local.ids.stages["default-source-enrollment-prompt"]
    source_enrollment_write  = local.ids.stages["default-source-enrollment-write"]
    source_enrollment_mfa    = authentik_stage_authenticator_validate.enrollment_mfa.id
    source_enrollment_login  = local.ids.stages["default-source-enrollment-login"]
  }
}

resource "authentik_flow_stage_binding" "this" {
  for_each = local.flow_bindings

  target               = local.flows[each.value.flow].uuid
  stage                = local.flow_binding_stages[each.key]
  order                = each.value.order
  evaluate_on_plan     = true
  re_evaluate_policies = each.value.re_evaluate_policies
}

import {
  for_each = { for key, binding in local.flow_bindings : key => binding if !contains(local.new_flows, binding.flow) }

  to = authentik_flow_stage_binding.this[each.key]
  id = local.ids.flow_bindings["${local.ids.flows[each.value.flow]}/${each.value.order}"]
}

# Who may start a flow, or reach a stage or application: the bound policy or group, in order.
resource "authentik_policy_binding" "recovery_skip_if_restored" {
  target  = authentik_flow_stage_binding.this["recovery_identification"].id
  policy  = authentik_policy_expression.recovery_skip_if_restored.id
  order   = 0
  enabled = true
  negate  = false
  timeout = 30
}

import {
  to = authentik_policy_binding.recovery_skip_if_restored
  id = local.ids.policy_bindings["${local.ids.flow_bindings["${local.ids.flows["default-recovery-flow"]}/10"]}/0/${local.ids.policies["default-recovery-skip-if-restored"]}"]
}

resource "authentik_policy_binding" "enrollment_gate" {
  target  = authentik_flow.enrollment.uuid
  policy  = authentik_policy_expression.enrollment_gate.id
  order   = 0
  enabled = true
  negate  = false
  timeout = 30
}

import {
  to = authentik_policy_binding.enrollment_gate
  id = local.ids.policy_bindings["${local.ids.flows["broker-enrollment"]}/0/${local.ids.policies["broker-enrollment-from-broker"]}"]
}

resource "authentik_policy_binding" "source_authentication_if_sso" {
  target  = authentik_flow.source_authentication.uuid
  policy  = local.ids.policies["default-source-authentication-if-sso"]
  order   = 0
  enabled = true
  negate  = false
  timeout = 30
}

resource "authentik_policy_binding" "source_enrollment_if_sso" {
  target  = authentik_flow.source_enrollment.uuid
  policy  = local.ids.policies["default-source-enrollment-if-sso"]
  order   = 0
  enabled = true
  negate  = false
  timeout = 30
}

resource "authentik_policy_binding" "source_enrollment_if_username" {
  target  = authentik_flow_stage_binding.this["source_enrollment_prompt"].id
  policy  = local.ids.policies["default-source-enrollment-if-username"]
  order   = 0
  enabled = true
  negate  = false
  timeout = 30
}
