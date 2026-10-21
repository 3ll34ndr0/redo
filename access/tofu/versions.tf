terraform {
  required_version = ">= 1.10"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.28"
    }
  }

  # Same bucket, lock and encryption as monitoring/tofu (see there); its own state file.
  # Environment: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (Contabo S3), AWS_ENDPOINT_URL_S3.
  backend "s3" {
    bucket       = "opentofu-states"
    key          = "redo/access.tfstate"
    region       = "us-east-1" # only used to sign requests; Contabo doesn't check it
    use_lockfile = true

    # Contabo isn't AWS: skip the AWS-only checks
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }

  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }
    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }
    state {
      method   = method.aes_gcm.state
      enforced = true
    }
    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}
