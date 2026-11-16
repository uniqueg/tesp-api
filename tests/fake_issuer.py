"""A local OIDC issuer for unit tests: discovery, JWKS, userinfo, tokens."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeIssuer:
    """Serves discovery and JWKS on a loopback port and mints RS256 tokens."""

    def __init__(self, audience="tesp-api"):
        self.audience = audience
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"
        self.requests = []
        self.userinfo = {}
        self.userinfo_status = 200
        self.userinfo_type = "application/json"
        self.discovery_issuer = None
        self.userinfo_sub = None
        self.delay = {}
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                issuer.requests.append(self.path)
                time.sleep(issuer.delay.get(self.path, 0))
                if self.path == "/.well-known/openid-configuration":
                    body = {
                        "issuer": issuer.discovery_issuer or issuer.url,
                        "jwks_uri": issuer.url + "/jwks",
                        "userinfo_endpoint": issuer.url + "/userinfo",
                    }
                    return self._send(200, body)
                if self.path == "/jwks":
                    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(issuer.key.public_key()))
                    public.update({"kid": issuer.kid, "use": "sig", "alg": "RS256"})
                    return self._send(200, {"keys": [public]})
                if self.path == "/userinfo":
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    # Test issuer echoing the subject; nothing is trusted here.
                    # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
                    claims = jwt.decode(token, options={"verify_signature": False})
                    body = {"sub": issuer.userinfo_sub or claims.get("sub"), **issuer.userinfo}
                    return self._send(issuer.userinfo_status, body, issuer.userinfo_type)
                return self._send(404, {})

            def _send(self, status, body, content_type="application/json"):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def token(self, sub="alice", key=None, kid=None, **claims):
        """Mint a token; ``claims`` override the defaults."""
        now = int(time.time())
        payload = {"iss": self.url, "sub": sub, "aud": self.audience, "iat": now, "exp": now + 300, **claims}
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, key or self.key, algorithm="RS256", headers={"kid": kid or self.kid})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
