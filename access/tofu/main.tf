# Cloudflare Access in front of unaudio's /admin pages (reviewing clip reports).
# Cloudflare asks for the login before the request reaches the server, then sends the app a
# signed token (Cf-Access-Jwt-Assertion) that the app checks against this application's
# audience (output "aud"): the server is also reachable without Cloudflare, so the header
# alone proves nothing.
#
# Roles: a role = an Access group + a policy + the paths it opens. Only "admin" for now
# (/admin); another role (e.g. a reviewer who can't change other settings) gets its own group
# and policy, and the app tells them apart by path or by the email in the token.

provider "cloudflare" {
  api_token = var.cloudflare_token
}

# Login with a one-time code sent by email: nothing to set up (no OAuth app, no secret). The
# account has at most one, and turning Zero Trust on created it: looked up, not managed here.
data "cloudflare_zero_trust_access_identity_providers" "all" {
  account_id = var.account_id
}

locals {
  email_code = one([for idp in data.cloudflare_zero_trust_access_identity_providers.all.result : idp.id if idp.type == "onetimepin"])
}

resource "cloudflare_zero_trust_access_group" "admins" {
  account_id = var.account_id
  name       = "unaudio admins"
  include    = [for email in var.admin_emails : { email = { email = email } }]
}

resource "cloudflare_zero_trust_access_policy" "admins" {
  account_id       = var.account_id
  name             = "unaudio admins"
  decision         = "allow"
  include          = [{ group = { id = cloudflare_zero_trust_access_group.admins.id } }]
  session_duration = "24h"
}

resource "cloudflare_zero_trust_access_application" "admin" {
  account_id = var.account_id
  name       = "unaudio admin"
  type       = "self_hosted"
  # A path covers everything below it (/admin/reports, /admin/api/...)
  destinations              = [for host in var.hosts : { type = "public", uri = "${host}/admin" }]
  policies                  = [{ id = cloudflare_zero_trust_access_policy.admins.id, precedence = 1 }]
  allowed_idps              = [local.email_code]
  auto_redirect_to_identity = true
  session_duration          = "24h"
  app_launcher_visible      = false
}

output "aud" {
  description = "The app checks tokens against this (env ACCESS_AUD in k8s/deployment.yaml)"
  value       = cloudflare_zero_trust_access_application.admin.aud
}
