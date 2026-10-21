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

# Login with a one-time code sent by email: nothing to set up (no OAuth app, no secret).
# An account has at most one: if it was already created in the dashboard, import it once:
#   tofu import cloudflare_zero_trust_access_identity_provider.email_code accounts/<account_id>/<idp_id>
resource "cloudflare_zero_trust_access_identity_provider" "email_code" {
  account_id = var.account_id
  name       = "One-time PIN"
  type       = "onetimepin"
  config     = {}
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
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.email_code.id]
  auto_redirect_to_identity = true
  session_duration          = "24h"
  app_launcher_visible      = false
}

output "aud" {
  description = "The app checks tokens against this (env ACCESS_AUD in k8s/deployment.yaml)"
  value       = cloudflare_zero_trust_access_application.admin.aud
}
