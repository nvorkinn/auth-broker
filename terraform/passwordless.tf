# Passkey sign-in: an explicit link on the login screen, beside the autofill that webauthn_stage gives the
# identification stage. New in this config, so no import blocks — the first apply creates both.
resource "authentik_flow" "passwordless" {
  name               = "Broker passkey sign-in"
  slug               = "broker-passwordless"
  title              = "Sign in with a passkey"
  designation        = "authentication"
  authentication     = "require_unauthenticated"
  layout             = "stacked"
  policy_engine_mode = "any"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [background, compatibility_mode]
  }
}

resource "authentik_stage_user_login" "passwordless" {
  name = "broker-passwordless-user-login"

  # Not owned by this config, so left as Authentik has it.
  lifecycle {
    ignore_changes = [geoip_binding, network_binding]
  }
}
