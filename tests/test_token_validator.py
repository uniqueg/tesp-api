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


def test_timeout_bounds_discovery(issuer):
    issuer.delay["/.well-known/openid-configuration"] = 1.5
    validator = TokenValidator([issuer.url], "tesp-api", timeout=0.5)
    with pytest.raises(OAuth2TokenError):
        validator.verify(issuer.token("alice"))


@pytest.mark.parametrize("timeout", [0, -1, "x", True])
def test_invalid_timeout_is_refused(timeout):
    with pytest.raises(ValueError):
        TokenValidator(["https://idp.example.org"], "tesp-api", timeout=timeout)


def _userinfo(issuer, token=None, **options):
    validator = TokenValidator([issuer.url], "tesp-api", userinfo=True, **options)
    token = token or issuer.token("alice")
    return validator.userinfo(token, validator.verify(token))


def test_userinfo_returns_claims(issuer):
    issuer.userinfo = {"eduperson_entitlement": ["urn:x"]}
    assert _userinfo(issuer) == {"sub": "alice", "eduperson_entitlement": ["urn:x"]}


def test_userinfo_is_cached_per_token(issuer):
    validator = TokenValidator([issuer.url], "tesp-api", userinfo=True)
    token = issuer.token("alice")
    for _ in range(3):
        validator.userinfo(token, validator.verify(token))
    assert issuer.requests.count("/userinfo") == 1
    other = issuer.token("alice", jti="other")
    validator.userinfo(other, validator.verify(other))
    assert issuer.requests.count("/userinfo") == 2


@pytest.mark.parametrize("failure", ["sub", "status", "jwt", "timeout"])
def test_userinfo_failures_are_rejected(issuer, failure):
    if failure == "sub":
        issuer.userinfo_sub = "mallory"
    elif failure == "status":
        issuer.userinfo_status = 500
    elif failure == "jwt":
        issuer.userinfo_type = "application/jwt"
    else:
        issuer.delay["/userinfo"] = 1.5
    with pytest.raises(OAuth2TokenError):
        _userinfo(issuer, timeout=0.5)


def test_userinfo_needs_a_usable_endpoint(issuer):
    validator = TokenValidator([issuer.url], "tesp-api", userinfo=True)
    token = issuer.token("alice")
    claims = validator.verify(token)
    validator._issuer_config(issuer.url)["userinfo_endpoint"] = "ftp://elsewhere/userinfo"
    with pytest.raises(OAuth2TokenError):
        validator.userinfo(token, claims)


class FakeCollection:
    def __init__(self, documents=None):
        self.calls = []
        self.documents = documents or {}

    async def update_many(self, query, update):
        self.calls.append((query, update))

    async def update_one(self, query, update, upsert=False):
        self.calls.append((query, update))
        self.documents.setdefault(query["_id"], {}).update(update["$set"])

    async def find_one(self, query):
        return self.documents.get(query["_id"])


ISSUERS = ["https://a.example.org", "https://b.example.org"]


def test_owner_migration_prefixes_unqualified_owners_and_records_it():
    tasks, meta = FakeCollection(), FakeCollection()
    asyncio.run(owner_migration(tasks, meta, ISSUERS))
    ((query, update),) = tasks.calls
    assert query["author"]["$type"] == "string"
    assert update == [{"$set": {"author": {"$concat": ["https://a.example.org|", "$author"]}}}]
    qualified = query["author"]["$not"]
    # Already qualified owners, also with issuers configured earlier, are skipped.
    for owner in ("https://b.example.org|x", "https://a.example.org|auth0|1", "https://a.example.org/|x",
                  "http://old.example.org|x"):
        assert qualified.match(owner)
    for owner in ("x", "auth0|1", "https://a.example.org", "user@example.org|x"):
        assert not qualified.match(owner)
    assert meta.documents["owners"] == {"issuer_qualified": True}


def test_owner_migration_refuses_going_back_to_one_issuer():
    tasks, meta = FakeCollection(), FakeCollection({"owners": {"issuer_qualified": True}})
    with pytest.raises(RuntimeError):
        asyncio.run(owner_migration(tasks, meta, ISSUERS[:1]))
    assert tasks.calls == []


def test_owner_migration_with_one_issuer_does_nothing():
    tasks, meta = FakeCollection(), FakeCollection()
    asyncio.run(owner_migration(tasks, meta, ISSUERS[:1]))
    assert tasks.calls == [] and meta.documents == {}
