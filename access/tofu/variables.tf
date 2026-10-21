variable "cloudflare_token" {
  description = "API token: Account → Access: Apps and Policies, Access: Organizations, Identity Providers, and Groups (Edit). GitHub secret CLOUDFLARE_ACCESS_TOKEN"
  type        = string
  sensitive   = true
}

variable "account_id" {
  description = "Cloudflare account (dashboard URL or the zone's Overview page). GitHub variable CLOUDFLARE_ACCOUNT_ID"
  type        = string
}

variable "admin_emails" {
  description = "Members of the admin role. GitHub secret UNAUDIO_ADMIN_EMAILS, a JSON list (secret: the public repo's Action logs would show them)"
  type        = list(string)
  sensitive   = true
}

variable "hosts" {
  description = "Sites whose /admin pages are behind the login"
  type        = list(string)
  default     = ["unaudio.marso.ar", "unaudio-dev.marso.ar"]
}

variable "state_passphrase" {
  description = "Encrypts access.tfstate (at least 16 characters). GitHub secret TOFU_STATE_PASSPHRASE"
  type        = string
  sensitive   = true
}
