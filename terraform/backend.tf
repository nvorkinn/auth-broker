# State lives in an Oracle Cloud Object Storage bucket, through its S3-compatible API. What differs between setups
# is passed at init, never committed: the bucket, region and endpoint as -backend-config from the GitHub variables
# TF_STATE_BUCKET, TF_STATE_REGION and TF_STATE_ENDPOINT, and the credentials (an OCI customer secret key) as
# AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, from GitHub secrets. See README.md.
terraform {
  backend "s3" {
    key = "authentik/terraform.tfstate"

    # Oracle's S3 API has no AWS accounts, regions to validate or instance metadata, and wants path-style URLs.
    use_path_style              = true
    skip_region_validation      = true
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
