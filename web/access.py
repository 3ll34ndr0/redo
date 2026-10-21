"""Who is asking for an /admin page: Cloudflare Access's signed token.

Cloudflare Access (access/tofu/) asks for the login before a request to /admin reaches the
server, and adds a token signed by the team (header Cf-Access-Jwt-Assertion, RS256). The
server is also reachable without Cloudflare (Traefik on the VPS's address), so the header
alone proves nothing: every request is checked against the team's public keys, the
application's audience (ACCESS_AUD), the issuer and the expiry.

Roles: the Access policy decides who gets a token for this application, so a valid token
means the admin role (group "unaudio admins" in access/tofu). Not configured
(ACCESS_TEAM_DOMAIN or ACCESS_AUD empty): nobody is an admin.
"""

import json
import os

import jwt

TEAM_DOMAIN = os.getenv("ACCESS_TEAM_DOMAIN", "")   # e.g. unaudio.cloudflareaccess.com
AUD = os.getenv("ACCESS_AUD", "")                   # access/tofu output "aud"
# Tests: the team's keys from a file instead of Cloudflare (a token still needs the private key)
JWKS_FILE = os.getenv("ACCESS_JWKS_FILE", "")
HEADER = "Cf-Access-Jwt-Assertion"


class FileKeys:
    """A JWKS file, same interface as jwt.PyJWKClient."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            self.keys = {k["kid"]: jwt.PyJWK(k) for k in json.load(f)["keys"]}

    def get_signing_key_from_jwt(self, token):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.keys:
            raise jwt.PyJWKClientError(f"unknown key {kid!r}")
        return self.keys[kid]


class Verifier:
    def __init__(self, team_domain=TEAM_DOMAIN, aud=AUD, jwks_file=JWKS_FILE):
        self.issuer = f"https://{team_domain}" if team_domain else ""
        self.aud = aud
        self.enabled = bool(team_domain and aud)
        if jwks_file:
            self.keys = FileKeys(jwks_file)
        elif self.enabled:
            # Keys cached an hour; an unknown key id (Cloudflare rotates them) fetches again
            self.keys = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True,
                                        lifespan=3600, timeout=5)
        else:
            self.keys = None

    def identity(self, token):
        """The token's claims if it's valid for this application, else None."""
        if not (self.enabled and token):
            return None
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            return jwt.decode(token, key.key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                              options={"require": ["exp", "iat", "aud", "iss"]}, leeway=30)
        except (jwt.PyJWTError, OSError):
            return None

    def admin(self, headers):
        """The admin's email, or None: the request must carry a valid token."""
        claims = self.identity(headers.get(HEADER, ""))
        return (claims.get("email") or claims.get("sub") or "admin") if claims else None
