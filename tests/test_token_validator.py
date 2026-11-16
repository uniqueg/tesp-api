"""Unit tests for OAuth2 token validation (no services needed)."""

import asyncio

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tesp_api.repository.task_repository import owner_migration
from tesp_api.service.error import OAuth2TokenError
from tesp_api.utils.token_validator import TokenValidator
from tests.fake_issuer import FakeIssuer


@pytest.fixture
def issuer():
    fake = FakeIssuer()
    yield fake
    fake.close()


@pytest.fixture
def other_issuer():
    fake = FakeIssuer()
    yield fake
    fake.close()


def test_valid_token_gives_claims_and_sub_owner(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    claims = validator.verify(issuer.token("alice"))
    assert claims["sub"] == "alice"
    assert validator.owner_id(claims) == "alice"


def test_unlisted_issuer_is_rejected_without_contacting_it(issuer, other_issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    with pytest.raises(OAuth2TokenError):
        validator.verify(other_issuer.token("mallory"))
    assert other_issuer.requests == []


def test_wrong_or_missing_audience_is_rejected(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token("alice", aud="another-api"))
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token("alice", aud=None))


def test_audience_list_in_token_is_accepted(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    assert validator.verify(issuer.token("alice", aud=["x", "tesp-api"]))["sub"] == "alice"


def test_foreign_signature_is_rejected(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token("alice", key=foreign))


def test_expired_token_is_rejected(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token("alice", exp=1))


def test_unsigned_token_is_rejected(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    # An unsigned token on purpose: the validator must reject it.
    # nosemgrep: python.jwt.security.jwt-none-alg.jwt-python-none-alg
    token = jwt.encode({"iss": issuer.url, "sub": "alice", "aud": "tesp-api", "exp": 2**31}, None, algorithm="none")
    with pytest.raises(OAuth2TokenError):
        validator.verify(token)


def test_missing_sub_is_rejected(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token(sub=None))


def test_discovery_issuer_mismatch_is_rejected(issuer):
    issuer.discovery_issuer = "https://elsewhere.example.org"
    validator = TokenValidator([issuer.url], "tesp-api")
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token("alice"))


def test_discovery_is_cached(issuer):
    validator = TokenValidator([issuer.url], "tesp-api")
    validator.verify(issuer.token("alice"))
    validator.verify(issuer.token("bob"))
    assert issuer.requests.count("/.well-known/openid-configuration") == 1


def test_issuer_with_trailing_slash_matches(issuer):
    issuer.discovery_issuer = issuer.url + "/"
    validator = TokenValidator([issuer.url + "/"], "tesp-api")
    claims = validator.verify(issuer.token("alice", iss=issuer.url + "/"))
    assert claims["sub"] == "alice"


def test_several_issuers_give_issuer_qualified_owners(issuer, other_issuer):
    validator = TokenValidator([issuer.url, other_issuer.url], "tesp-api")
    claims = validator.verify(other_issuer.token("auth0|42"))
    assert validator.owner_id(claims) == other_issuer.url + "|auth0|42"
    assert validator.owner_id(validator.verify(issuer.token("alice"))) == issuer.url + "|alice"


@pytest.mark.parametrize(
    ("issuers", "audience"),
    [([], "tesp-api"), (None, "tesp-api"), (["https://idp.example.org"], ""), (["https://idp.example.org"], None),
     ([""], "tesp-api"), (["idp.example.org"], "tesp-api")],
)
def test_incomplete_configuration_is_refused(issuers, audience):
    with pytest.raises(ValueError):
        TokenValidator(issuers, audience)


class FakeCollection:
    def __init__(self):
        self.calls = []

    async def update_many(self, query, update):
        self.calls.append((query, update))


def test_owner_migration_prefixes_unqualified_owners():
    tasks = FakeCollection()
    asyncio.run(owner_migration(tasks, "https://a.example.org", ["https://a.example.org", "https://b.example.org"]))
    ((query, update),) = tasks.calls
    assert query["author"]["$type"] == "string"
    assert query["author"]["$not"].pattern == r"^(?:https://a\.example\.org\||https://b\.example\.org\|)"
    assert update == [{"$set": {"author": {"$concat": ["https://a.example.org|", "$author"]}}}]
