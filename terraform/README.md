# Terraform

Infrastructure managed as code. Nothing is managed here yet: this is the layout and the workflow, ready for the
first resource.

```
versions.tf    Terraform and provider versions
backend.tf     where state lives (not chosen yet, see below)
providers.tf   provider settings, once there is one
main.tf        resources, or a file per area (dns.tf, github.tf, ...) once there are several
variables.tf   inputs; secret ones come from GitHub secrets as TF_VAR_*
outputs.tf     values something outside Terraform needs
modules/       a folder per module, for anything used more than once
.terraform.lock.hcl   provider checksums, created by the first provider; committed
```

## Before the first resource: remote state

State is local until a backend is chosen, so the apply job refuses to run: local state on a CI runner is thrown
away with it, and Terraform would lose track of what it created. Pick a backend, fill in `backend.tf`, and put
its credentials in GitHub secrets.

## Adding a resource

1. If it needs a new provider, add it to `required_providers` in `versions.tf` with a `~>` constraint, then
   record its checksums for CI and your own machine and commit the lock file:

   ```sh
   cd terraform
   terraform init -backend=false
   terraform providers lock -platform=linux_amd64 -platform=darwin_arm64
   ```

   Its credentials go in GitHub secrets (read-only ones for the plan, and the `terraform` environment's for
   apply) and reach the provider as environment variables in `.github/workflows/terraform.yml`. Never in a file.
2. Add the resource. `terraform fmt`, `terraform validate` and `tflint` run on commit if you have pre-commit
   installed (`pre-commit install`; needs `terraform` and `tflint` on your PATH).
3. Open a PR. Checks run in parallel: formatting and validation, tflint, a Trivy misconfiguration scan, and a
   plan, which is posted as a comment on the PR and updated on each push.

## Applying

After the PR is merged: Actions > Terraform > Run workflow on main, with "apply" ticked. It reruns the checks,
plans main (the plan is in the run's summary), then waits for approval in the `terraform` environment. Approving
applies exactly that plan; if anything changed the state in between, Terraform rejects it as stale and nothing is
applied. Without "apply" ticked, the button just plans, which shows any drift.

The `terraform` environment needs a required reviewer (Settings > Environments). Without the environment GitHub
creates it on the first run with no protection, and apply would run unapproved.

## Upgrading

Dependabot opens PRs for providers and modules. Terraform itself is pinned in `versions.tf` and in `TF_VERSION`
in the workflow; bump both together.
