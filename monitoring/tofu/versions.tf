terraform {
  required_version = ">= 1.10"

  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.47"
    }
  }

  # State in Contabo Object Storage (S3-compatible), bucket opentofu-states, locked during
  # runs (lock file next to it) and ENCRYPTED by OpenTofu (passphrase TOFU_STATE_PASSPHRASE).
  # Credentials and endpoint come from the environment, not from here:
  #   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   Contabo S3 credentials
  #   AWS_ENDPOINT_URL_S3                         e.g. https://usa1.contabostorage.com
  backend "s3" {
    bucket       = "opentofu-states"
    key          = "redo/grafana.tfstate"
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
