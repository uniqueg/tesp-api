import threading
from typing import Any, Dict, List, Optional, Union

import jwt
import requests
from jwt import PyJWKClient

from tesp_api.service.error import OAuth2TokenError

# Asymmetric algorithms only: the keys come from the issuer's JWKS.
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]
HTTP_TIMEOUT = 10


class TokenValidator:
    """
    Verifies OAuth2 JWT access tokens against a configured list of issuers.

    Only the configured issuers are contacted: the discovery document and
    the JWKS of each are fetched on first use and cached. A token must be
    signed by its issuer's key and name the configured audience. With more
    than one issuer, owners are '<iss>|<sub>' so that equal subjects of
    different issuers stay apart; with one issuer, the owner is 'sub'.
    """

    def __init__(self, issuers: Optional[List[str]], audience: Optional[Union[str, List[str]]]):
        issuers = list(issuers or [])
        if not issuers or not all(isinstance(i, str) and i.startswith(("https://", "http://")) for i in issuers):
            raise ValueError("oauth.issuers must list the accepted issuer URLs")
        if not audience:
            raise ValueError("oauth.audience must name this service's audience")
        self.issuers = issuers
        self.audience = audience
        self._jwks_clients: Dict[str, PyJWKClient] = {}
        self._lock = threading.Lock()

    def owner_id(self, claims: Dict[str, Any]) -> str:
        if len(self.issuers) > 1:
            return f"{claims['iss']}|{claims['sub']}"
        return claims['sub']

    def verify(self, token: Optional[str]) -> Dict[str, Any]:
        if not token:
            raise OAuth2TokenError("Missing bearer token.")
        try:
            # Only picks one of the configured issuers; the token is verified below
            # against that issuer's keys, issuer and audience.
            # nosemgrep: python.jwt.security.unverified-jwt-decode.unverified-jwt-decode
            unverified_iss = jwt.decode(token, options={"verify_signature": False}).get('iss')
        except jwt.PyJWTError as e:
            raise OAuth2TokenError(f"Failed to decode token. Ex: {str(e)}")
        if unverified_iss not in self.issuers:
            raise OAuth2TokenError("Token issuer is not accepted.")

        try:
            signing_key = self._jwks_client(unverified_iss).get_signing_key_from_jwt(token).key
        except (jwt.PyJWTError, requests.RequestException, ValueError, KeyError) as e:
            raise OAuth2TokenError(f"Failed to obtain issuer signing key. Ex: {str(e)}")

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=ALGORITHMS,
                issuer=unverified_iss,
                audience=self.audience,
                options={"require": ["iss", "sub", "aud", "exp"]},
            )
        except jwt.PyJWTError as e:
            raise OAuth2TokenError(f"Failed to verify token. Ex: {str(e)}")
        if not isinstance(claims.get('sub'), str) or not claims['sub']:
            raise OAuth2TokenError("Missing 'sub' parameter in JWT token payload.")
        return claims

    def _jwks_client(self, issuer: str) -> PyJWKClient:
        with self._lock:
            client = self._jwks_clients.get(issuer)
        if client is not None:
            return client
        response = requests.get(issuer.rstrip('/') + "/.well-known/openid-configuration", timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        config = response.json()
        if config.get('issuer') != issuer:
            raise ValueError("Discovery document names another issuer.")
        if not isinstance(config.get('jwks_uri'), str):
            raise ValueError("Discovery document has no 'jwks_uri'.")
        client = PyJWKClient(config['jwks_uri'], cache_keys=True, lifespan=300, timeout=HTTP_TIMEOUT)
        with self._lock:
            return self._jwks_clients.setdefault(issuer, client)
