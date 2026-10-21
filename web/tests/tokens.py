"""Fake Cloudflare Access tokens for the tests: a key pair made at import, its public half in a
JWKS file the app reads instead of Cloudflare's (ACCESS_JWKS_FILE, set by conftest.py)."""

import json
import os
import tempfile
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

TEAM = "test.cloudflareaccess.com"
AUD = "test-audience"
KID = "test-key"
EMAIL = "admin@example.com"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)    # not the team's

JWKS_FILE = os.path.join(tempfile.mkdtemp(prefix="extractos-access-"), "certs.json")
with open(JWKS_FILE, "w") as f:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    json.dump({"keys": [dict(jwk, kid=KID, alg="RS256", use="sig")]}, f)


def token(key=KEY, kid=KID, algorithm="RS256", **claims):
    """A token as Cloudflare Access signs it; claims override the valid defaults."""
    now = int(time.time())
    body = {"aud": [AUD], "email": EMAIL, "iss": f"https://{TEAM}", "iat": now, "nbf": now, "exp": now + 3600,
            "sub": "user-id", "type": "app"}
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


def headers(**claims):
    return {"Cf-Access-Jwt-Assertion": token(**claims)}
