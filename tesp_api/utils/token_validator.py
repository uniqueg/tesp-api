import copy
import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlsplit

import jwt
import requests
from jwt import PyJWKClient
from loguru import logger

from tesp_api.service.error import OAuth2TokenError

# Asymmetric algorithms only: the keys come from the issuer's JWKS.
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]
# Userinfo answers are cached per token until it expires, at most this long ...
USERINFO_MAX_AGE = 300
# ... and for at most this many tokens; the least recently used go first.
USERINFO_CACHE_SIZE = 4096


class TokenValidator:
    """
    Verifies OAuth2 JWT access tokens against a configured list of issuers.

    Only the configured issuers are contacted: the discovery document and
    the JWKS of each are fetched on first use and cached. A token must be
    signed by its issuer's key and name the configured audience. With more
    than one issuer, owners are '<iss>|<sub>' so that equal subjects of
    different issuers stay apart; with one issuer, the owner is 'sub'.

    With 'userinfo', the issuer's userinfo answer for a token is looked up
    too (identity resolution; its 'sub' must match the token's). 'timeout'
    bounds the discovery, JWKS and userinfo calls (seconds).
    """

    def __init__(self, issuers: Optional[List[str]], audience: Optional[Union[str, List[str]]],
                 timeout: float = 10, userinfo: bool = False):
        issuers = list(issuers or [])
        if not issuers or not all(isinstance(i, str) and i.startswith(("https://", "http://")) for i in issuers):
            raise ValueError("oauth.issuers must list the accepted issuer URLs")
        if not audience:
            raise ValueError("oauth.audience must name this service's audience")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("oauth.timeout must be a positive number of seconds")
        self.issuers = issuers
        self.audience = audience
        self.timeout = timeout
        self.userinfo_enabled = bool(userinfo)
        self._configs: Dict[str, Dict[str, Any]] = {}
        self._jwks_clients: Dict[str, PyJWKClient] = {}
        self._userinfo_cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
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

    def userinfo(self, token: str, claims: Dict[str, Any]) -> Dict[str, Any]:
        """
        The issuer's userinfo answer for a verified token. Cached per token
        hash until the token expires, at most USERINFO_MAX_AGE seconds. Any
        failure, a non-JSON answer and a 'sub' other than the token's raise
        OAuth2TokenError. Concurrent first requests with one token may each
        ask the issuer.
        """
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            cached = self._userinfo_cache.get(key)
            if cached is not None and cached[0] > now:
                self._userinfo_cache.move_to_end(key)
                return copy.deepcopy(cached[1])
        try:
            endpoint = self._userinfo_endpoint(claims['iss'])
            response = requests.get(endpoint, timeout=self.timeout, allow_redirects=False,
                                    headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            media_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            info = response.json() if response.status_code == 200 and media_type == "application/json" else None
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning(f"Userinfo lookup failed: {type(e).__name__}")
            raise OAuth2TokenError("Userinfo lookup failed.")
        if not isinstance(info, dict) or info.get('sub') != claims['sub']:
            logger.warning("Userinfo lookup failed: no JSON object with the token's sub")
            raise OAuth2TokenError("Userinfo lookup failed.")
        expires = claims.get('exp')
        until = min(now + USERINFO_MAX_AGE, float(expires) if isinstance(expires, (int, float)) else now)
        with self._lock:
            self._userinfo_cache[key] = (until, info)
            while len(self._userinfo_cache) > USERINFO_CACHE_SIZE:
                self._userinfo_cache.popitem(last=False)
        return copy.deepcopy(info)

    def check_userinfo(self) -> None:
        """At startup: every issuer must have a usable userinfo endpoint (ValueError otherwise)."""
        for issuer in self.issuers:
            try:
                self._userinfo_endpoint(issuer)
            except (requests.RequestException, ValueError) as e:
                raise ValueError(f"oauth.userinfo.enable is on, but issuer {issuer}: {e}")

    def _userinfo_endpoint(self, issuer: str) -> str:
        """The discovered endpoint, if https or as secure as the issuer URL."""
        endpoint = self._issuer_config(issuer).get('userinfo_endpoint')
        parts = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if parts is None or parts.scheme not in {"https", urlsplit(issuer).scheme} or not parts.netloc:
            raise ValueError("Discovery document has no usable 'userinfo_endpoint'.")
        return endpoint

    def _issuer_config(self, issuer: str) -> Dict[str, Any]:
        """The issuer's discovery document; fetched once (issuer must be configured)."""
        with self._lock:
            config = self._configs.get(issuer)
        if config is not None:
            return config
        response = requests.get(issuer.rstrip('/') + "/.well-known/openid-configuration",
                                timeout=self.timeout, allow_redirects=False)
        response.raise_for_status()
        config = response.json()
        if not isinstance(config, dict) or config.get('issuer') != issuer:
            raise ValueError("Discovery document names another issuer.")
        with self._lock:
            return self._configs.setdefault(issuer, config)

    def _jwks_client(self, issuer: str) -> PyJWKClient:
        with self._lock:
            client = self._jwks_clients.get(issuer)
        if client is not None:
            return client
        config = self._issuer_config(issuer)
        if not isinstance(config.get('jwks_uri'), str):
            raise ValueError("Discovery document has no 'jwks_uri'.")
        client = PyJWKClient(config['jwks_uri'], cache_keys=True, lifespan=300, timeout=self.timeout)
        with self._lock:
            return self._jwks_clients.setdefault(issuer, client)
