# The terraform ruleset ships with tflint. Add a provider's ruleset here (plugin "aws" { ... }) when one exists for
# a provider this config uses.
config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "all"
}
