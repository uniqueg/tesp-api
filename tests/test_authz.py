"""Unit tests for the authorization hook (no services needed).

Case numbers refer to the conformance cases of the TES AuthZ profile
"tes-authzen/1" (TES bring-your-own AuthZ spec, section 8.2).
"""

import asyncio
import json
import threading
import sys
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from bson.objectid import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pymonad.promise import Promise

from tesp_api.api import authz as authz_module
from tesp_api.api.authz import Authz, Principal, strip_url
from tesp_api.api.endpoints import endpoint_utils, task_endpoints
from tesp_api.config.properties import get_properties, replacing_list
from tesp_api.repository.model.task import RegisteredTesTask, TesTaskState
from tesp_api.utils.functional import maybe_of
from tesp_api.utils.token_validator import TokenValidator
from tests.fake_issuer import FakeIssuer

VO = "urn:geant:lifescience-ri.eu:group:example-vo"
SUBGROUP = VO + ":analysts#login.aai.lifescience-ri.eu"
NEAR_MISS = "urn:geant:lifescience-ri.eu:group:example-vox#login.aai.lifescience-ri.eu"
RULE = {"name": "eduperson_entitlement", "prefixes": [VO + ":"]}
SERVICE_ID = "tes-authz-test"
TASK = {"name": "t", "executors": [{"image": "docker.io/library/alpine:3.20", "command": ["true"]}]}


async def _import_app():
    # aiohttp's connector (Pulsar client) needs a running loop at import.
    from tesp_api.tesp_api import app
    return app


APP = asyncio.run(_import_app())


# Fakes ---------------------------------------------------------------------


class FakePdp:
    """A recording AuthZEN PDP with a scripted answer per request."""

    def __init__(self):
        self.requests = []
        self.answer = lambda body: {"decision": True}
        pdp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                pdp.requests.append({"body": body, "headers": dict(self.headers)})
                answer = pdp.answer(body)
                if answer.get("delay"):
                    time.sleep(answer["delay"])
                if answer.get("close"):
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                status = answer.get("status", 200)
                data = answer["raw"].encode() if "raw" in answer else json.dumps(answer.get("body", answer)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/access/v1/evaluation"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def decide(self, default=True, **rules):
        """Answer ``default``, or per ``"<action>"``/``"<action>/<resource id>"`` rules."""

        def answer(body):
            action = body["action"]["name"]
            key = f"{action}/{body['resource']['id']}"
            decision = rules.get(key, rules.get(action, default))
            return decision if isinstance(decision, dict) else {"decision": decision}

        self.answer = answer

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeRepository:
    """In-memory stand-in for ``task_repository`` (same Promise interface)."""

    def __init__(self):
        self.tasks = {}

    @staticmethod
    def _promise(value):
        return Promise(lambda resolve, reject: resolve(value))

    def create_task(self, task):
        stored = task.copy(update={"id": ObjectId()})
        self.tasks[stored.id] = stored
        return self._promise(stored.id)

    def get_task(self, author, query):
        task = self.tasks.get(query["_id"])
        if task is not None and author.maybe(False, lambda a: task.author != a):
            task = None
        return self._promise(maybe_of(task))

    async def get_task_meta(self, task_id):
        task = self.tasks.get(ObjectId(task_id)) if ObjectId.is_valid(task_id) else None
        if task is None:
            return None
        return {"owner": task.author, "state": task.state.value, "tags": task.tags or {}}

    def get_tasks(self, author, page_size, page_token, search_query):
        tasks = sorted(self.tasks.values(), key=lambda t: t.id)
        tasks = [t for t in tasks if author.maybe(True, lambda a: t.author == a)]
        tasks = [t for t in tasks if page_token.maybe(True, lambda p: t.id > p)]
        size = page_size.maybe(None, lambda x: x)
        tasks = tasks[:size] if size else tasks
        return self._promise((tasks, tasks[-1].id if size and len(tasks) == size else None))

    def cancel_task(self, author, task_id):
        task = self.tasks.get(task_id)
        if task is not None and author.maybe(True, lambda a: task.author == a):
            self.tasks[task_id] = task.copy(update={"state": TesTaskState.CANCELED})
            return self._promise(maybe_of(task_id))
        return self._promise(maybe_of(None))

    def add(self, author, **fields):
        task = RegisteredTesTask(**TASK, state="QUEUED", author=author, **fields)
        self.tasks[task.id] = task
        return str(task.id)


# Fixtures ------------------------------------------------------------------


@pytest.fixture
def issuer():
    fake = FakeIssuer()
    yield fake
    fake.close()


@pytest.fixture
def pdp():
    fake = FakePdp()
    yield fake
    fake.close()


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepository()
    monkeypatch.setattr(task_endpoints, "task_repository", fake)
    monkeypatch.setattr(task_endpoints, "dispatch_event", lambda *args, **kwargs: None)
    return fake


@pytest.fixture
def oauth_on():
    endpoint_utils.properties.set("oauth.enable", True)
    yield
    endpoint_utils.properties.set("oauth.enable", False)


@pytest.fixture
def tes(monkeypatch, oauth_on, issuer, repo, pdp):
    """Start the API with a given hook configuration; returns a client factory."""
    def start(pdp_on=False, userinfo=False, **settings):
        validator = TokenValidator([issuer.url], "tesp-api", timeout=0.5, userinfo=userinfo)
        monkeypatch.setattr(endpoint_utils, "_token_validator", validator)
        if pdp_on:
            settings.setdefault("decision_service", {})["url"] = pdp.url
        monkeypatch.setattr(authz_module, "_authz", Authz(settings, auth_enabled=True, service_id=SERVICE_ID))
        return Api(TestClient(APP), issuer)

    return start


class Api:
    def __init__(self, client, issuer):
        self.client = client
        self.issuer = issuer

    def call(self, method, path, sub="alice", body=None, headers=None, **claims):
        headers = dict(headers or {})
        if sub is not None:
            headers["Authorization"] = "Bearer " + self.issuer.token(sub, **claims)
        response = self.client.request(method, "/v1" + path, json=body, headers=headers)
        return response.status_code, response.json() if response.content else None

    def create(self, sub="alice", task=None, **claims):
        return self.call("POST", "/tasks", sub, task or TASK, **claims)


ALICE = {"sub": "alice", "eduperson_entitlement": [SUBGROUP], "scope": "openid eduperson_entitlement", "azp": "cli"}
CAROL = {"sub": "carol", "eduperson_entitlement": [NEAR_MISS], "scope": "openid", "azp": "cli"}


# URL stripping -------------------------------------------------------------


URL_CASES = json.loads((Path(__file__).parent / "golden" / "url-stripping.json").read_text())


@pytest.mark.parametrize("case", URL_CASES, ids=[c["input"] for c in URL_CASES])
def test_strip_url(case):
    assert strip_url(case["input"]) == case["expected"]




# Claim lookup and rule -----------------------------------------------------


def test_lookup_exact_name_before_path_token_before_userinfo():
    p = Principal("a", {"a.b": "exact", "a": {"b": "path"}, "x": "t"}, {"x": "u", "y": "u", "c": {"d": "u"}})
    assert p.claim("a.b") == "exact"
    assert p.claim("x") == "t"
    assert p.claim("y") == "u"
    assert p.claim("c.d") == "u"
    assert p.claim("missing") is None
    assert Principal("a", {"a": {"b": "path"}}).claim("a.b") == "path"


@pytest.mark.parametrize(
    ("value", "allowed"),
    [([SUBGROUP], True), (SUBGROUP, True), ([NEAR_MISS], False), ([1, SUBGROUP], False), (None, False),
     ({"x": SUBGROUP}, False), ([VO + "#login"], True)],
)
def test_gate(value, allowed):
    rule = dict(RULE, values=[VO + "#login"])
    authz = Authz({"required_claim": rule}, auth_enabled=True, service_id=SERVICE_ID)
    claims = {} if value is None else {"eduperson_entitlement": value}
    if allowed:
        authz.gate(Principal("a", claims))
    else:
        with pytest.raises(HTTPException) as e:
            authz.gate(Principal("a", claims))
        assert e.value.status_code == 403


# Startup checks (case 23 and the fail-safe checks) ---------------------------


@pytest.mark.parametrize(
    ("settings", "auth"),
    [
        ({"decision_service": {"url": "http://127.0.0.1:1/e"}}, False),
        ({"required_claim": {"name": "g", "values": ["a"]}}, False),
        ({"required_claim": {"name": "g"}}, True),
        ({"required_claim": {"name": "g", "values": [""]}}, True),
        ({"required_claim": {"name": "g", "prefixes": [""]}}, True),
        ({"required_claim": {"name": "g", "values": "a"}}, True),
        ({"decision_service": {"timeout": 3}}, True),
        ({"decision_service": {"claims": ["g"]}}, True),
        ({"decision_service": {"url": "ftp://pdp/e"}}, True),
        ({"decision_service": {"url": "http:///e"}}, True),
        ({"decision_service": {"url": "http://pdp/e", "credential_file": "/nonexistent"}}, True),
        ({"decision_service": {"url": "http://pdp/e", "timeout": 0}}, True),
    ],
)
def test_startup_refuses(settings, auth):
    with pytest.raises(ValueError):
        Authz(settings, auth_enabled=auth, service_id=SERVICE_ID)


def test_empty_credential_file_is_refused(tmp_path):
    path = tmp_path / "credential"
    path.write_text("\n")
    with pytest.raises(ValueError):
        Authz({"decision_service": {"url": "http://pdp/e", "credential_file": str(path)}}, True, SERVICE_ID)


def test_nothing_configured_without_auth_is_fine():
    Authz({}, auth_enabled=False, service_id=SERVICE_ID)


# Conformance cases ---------------------------------------------------------


def test_case_02_service_info_open(tes):
    assert tes().call("GET", "/service-info", sub=None)[0] == 200


def test_case_05_and_16_rule_fails_pdp_not_called(tes, pdp, repo):
    api = tes(pdp_on=True, required_claim=RULE)
    task_id = repo.add("alice")
    assert api.create(**CAROL)[0] == 403
    assert api.call("GET", f"/tasks/{task_id}", **CAROL)[0] == 403
    assert api.call("GET", "/tasks", **CAROL)[0] == 403
    assert api.call("POST", f"/tasks/{task_id}:cancel", **CAROL)[0] == 403
    assert pdp.requests == []


def test_case_04_and_04b_prefix(tes):
    api = tes(required_claim=RULE)
    assert api.create(**ALICE)[0] == 200
    assert api.create(**CAROL)[0] == 403


def test_case_05b_rule_without_pdp_or_allowlist(tes):
    api = tes(required_claim={"name": "eduperson_entitlement", "values": [SUBGROUP]})
    assert api.create(**ALICE)[0] == 200


@pytest.mark.parametrize("name", ["ctx.groups", "https://e.org/groups"])
def test_case_05c_dotted_path_and_uri_name(tes, name):
    claims = {"ctx": {"groups": ["g1"]}} if name == "ctx.groups" else {"https://e.org/groups": ["g1"]}
    api = tes(required_claim={"name": name, "values": ["g1"]})
    assert api.create("u", **claims)[0] == 200
    assert api.create(**ALICE)[0] == 403


def test_case_05d_client_credentials_without_claim(tes):
    api = tes(required_claim=RULE)
    assert api.create("protes", client_id="protes", scope="openid")[0] == 403


def test_case_06b_userinfo_claims(tes, pdp, issuer):
    """Userinfo fills claims missing from the token; the token's value wins."""
    issuer.userinfo = {"eduperson_entitlement": [SUBGROUP], "groups": ["from-userinfo"]}
    api = tes(pdp_on=True, userinfo=True, required_claim=RULE,
              decision_service={"claims": ["eduperson_entitlement", "groups"]})
    assert api.create("u", groups=["from-token"])[0] == 200
    assert pdp.requests[0]["body"]["subject"]["properties"]["claims"] == {
        "eduperson_entitlement": [SUBGROUP], "groups": ["from-token"]}
    issuer.userinfo = {"eduperson_entitlement": [NEAR_MISS]}
    assert api.create("v", eduperson_entitlement=[SUBGROUP])[0] == 200
    assert api.create("w")[0] == 403


def test_case_20_userinfo_failure_is_401(tes, pdp, issuer):
    """The failure kinds are covered in test_token_validator; here the mapping to 401."""
    issuer.userinfo_sub = "mallory"
    assert tes(pdp_on=True, userinfo=True).create(**ALICE)[0] == 401
    assert pdp.requests == []


def test_case_21_userinfo_once_per_token(tes, issuer):
    api = tes(userinfo=True)
    token = issuer.token("alice")
    for _ in range(2):
        response = api.client.post("/v1/tasks", json=TASK, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
    assert issuer.requests.count("/userinfo") == 1


def test_case_07_create_request_shape(tes, pdp, repo):
    api = tes(pdp_on=True, decision_service={"claims": ["eduperson_entitlement"]})
    submit = {
        "id": "client-chosen-id",
        "state": "COMPLETE",
        "author": "bob",
        "name": "hello",
        "tags": {"project": "demo"},
        "volumes": ["/vol/a"],
        "resources": {"cpu_cores": 1, "ram_gb": 1.0, "disk_gb": 5.0, "preemptible": False, "zones": []},
        "executors": [{"image": "docker.io/library/alpine:3.20", "command": ["echo", "secret"],
                       "env": {"TOKEN": "do-not-send"}}],
        "inputs": [{"path": "/data/in.txt", "type": "FILE", "url": "s3://bucket/in.txt?X-Amz-Signature=do-not-send"},
                   {"path": "/data/inline.txt", "type": "FILE", "content": "do-not-send"}],
        "outputs": [{"path": "/data/out", "type": "DIRECTORY",
                     "url": "https://user:do-not-send@storage.example.org/out/"}],
    }
    status, body = api.call("POST", "/tasks", body=submit, **ALICE)
    assert status == 200 and body["id"] != "client-chosen-id"
    (recorded,) = pdp.requests
    assert recorded["body"] == {
        "subject": {"type": "user", "id": "alice", "properties": {
            "iss": api.issuer.url, "sub": "alice", "client_id": "cli", "scope": ["openid", "eduperson_entitlement"],
            "claims": {"eduperson_entitlement": [SUBGROUP]}}},
        "action": {"name": "CreateTask", "properties": {}},
        "resource": {"type": "tes_task_set", "id": "own", "properties": {"task": {
            "name": "hello",
            "tags": {"project": "demo"},
            "resources": {"cpu_cores": 1, "ram_gb": 1.0, "disk_gb": 5.0, "preemptible": False, "zones": []},
            "volumes": ["/vol/a"],
            "executors": [{"image": "docker.io/library/alpine:3.20"}],
            "inputs": [{"url": "s3://bucket/in.txt", "path": "/data/in.txt", "type": "FILE"},
                       {"path": "/data/inline.txt", "type": "FILE"}],
            "outputs": [{"url": "https://storage.example.org/out/", "path": "/data/out", "type": "DIRECTORY"}],
        }}},
        "context": {"tes": {"profile": "tes-authzen/1", "service_id": SERVICE_ID}},
    }
    assert "do-not-send" not in json.dumps(recorded)
    assert "Authorization" not in recorded["headers"]
    stored = repo.tasks[ObjectId(body["id"])]
    assert stored.author == "alice" and stored.state.value == "QUEUED"


def test_case_08_pdp_denies_create(tes, pdp, repo):
    pdp.decide(False)
    assert tes(pdp_on=True).create(**ALICE)[0] == 403
    assert repo.tasks == {}


def test_case_09_pdp_allows_foreign_get(tes, repo):
    task_id = repo.add("bob")
    assert tes(pdp_on=True).call("GET", f"/tasks/{task_id}", **ALICE)[0] == 200


def test_case_10_pdp_denies_get_and_cancel(tes, pdp, repo):
    task_id = repo.add("bob")
    pdp.decide(False)
    api = tes(pdp_on=True)
    assert api.call("GET", f"/tasks/{task_id}", **ALICE)[0] == 404
    assert api.call("POST", f"/tasks/{task_id}:cancel", **ALICE)[0] == 404
    assert repo.tasks[ObjectId(task_id)].state.value == "QUEUED"


def test_case_10b_get_allowed_cancel_denied(tes, pdp, repo):
    task_id = repo.add("bob", tags={"k": "v"})
    pdp.decide(True, CancelTask=False)
    assert tes(pdp_on=True).call("POST", f"/tasks/{task_id}:cancel", **ALICE)[0] == 403
    cancel, get = pdp.requests
    resource = {"type": "tes_task", "id": task_id, "properties": {"owner": "bob", "state": "QUEUED", "tags": {"k": "v"}}}
    assert cancel["body"]["resource"] == get["body"]["resource"] == resource
    assert cancel["body"]["action"] == {"name": "CancelTask", "properties": {}}
    assert get["body"]["action"] == {"name": "GetTask", "properties": {}}
    assert repo.tasks[ObjectId(task_id)].state.value == "QUEUED"


def test_cancel_allowed_cancels(tes, repo):
    task_id = repo.add("bob")
    assert tes(pdp_on=True).call("POST", f"/tasks/{task_id}:cancel", **ALICE)[0] == 200
    assert repo.tasks[ObjectId(task_id)].state.value == "CANCELED"


@pytest.mark.parametrize("decision", [True, False])
@pytest.mark.parametrize("task_id", ["no-such-task", str(ObjectId())])
def test_case_11_missing_task(tes, pdp, decision, task_id):
    pdp.decide(decision)
    assert tes(pdp_on=True).call("GET", f"/tasks/{task_id}?view=BASIC", **ALICE)[0] == 404
    (recorded,) = pdp.requests
    assert recorded["body"]["resource"] == {"type": "tes_task", "id": task_id, "properties": {}}
    assert recorded["body"]["action"] == {"name": "GetTask", "properties": {"view": "BASIC"}}


def test_case_11b_pdp_down_same_answer(tes, pdp, repo):
    task_id = repo.add("alice")
    pdp.decide({"close": True})
    api = tes(pdp_on=True)
    assert api.call("GET", f"/tasks/{task_id}", **ALICE)[0] == 503
    assert api.call("GET", "/tasks/no-such-task", **ALICE)[0] == 503


@pytest.mark.parametrize("decision", [True, False])
def test_case_11c_cancel_missing_task(tes, pdp, decision):
    pdp.decide(decision)
    assert tes(pdp_on=True).call("POST", "/tasks/no-such-task:cancel", **ALICE)[0] == 404
    actions = [r["body"]["action"]["name"] for r in pdp.requests]
    assert actions == (["CancelTask", "GetTask"])
    assert all(r["body"]["resource"]["properties"] == {} for r in pdp.requests)
    request_ids = [r["headers"]["X-Request-ID"] for r in pdp.requests]
    assert len(set(request_ids)) == 2


def _pages(api, size, **who):
    ids, token = [], None
    for _ in range(20):
        status, page = api.call("GET", f"/tasks?page_size={size}" + (f"&page_token={token}" if token else ""), **who)
        assert status == 200 and len(page["tasks"]) <= size
        ids += [t["id"] for t in page["tasks"]]
        token = page.get("next_page_token")
        if not token:
            return ids
    raise AssertionError("paging did not end")


def test_case_12_all_scope(tes, repo, pdp):
    created = [repo.add(owner) for owner in ("alice", "bob", "alice", "bob", "alice")]
    api = tes(pdp_on=True)
    assert sorted(_pages(api, 2, **ALICE)) == sorted(created)
    lists = [r["body"] for r in pdp.requests[:2]]
    assert {b["resource"]["id"] for b in lists} == {"all", "own"}
    assert all("properties" not in b["resource"] and b["action"]["properties"] == {"view": "MINIMAL"} for b in lists)
    assert len({r["headers"]["X-Request-ID"] for r in pdp.requests}) == len(pdp.requests)


def test_case_13_own_scope(tes, repo, pdp):
    created = [(owner, repo.add(owner)) for owner in ("alice", "bob", "alice", "bob", "alice")]
    pdp.decide(True, **{"ListTasks/all": False})
    ids = _pages(tes(pdp_on=True), 2, **ALICE)
    assert sorted(ids) == sorted(i for owner, i in created if owner == "alice")


def test_case_14_both_sets_denied(tes, pdp):
    pdp.decide(False)
    assert tes(pdp_on=True).call("GET", "/tasks", **ALICE)[0] == 403
    assert {r["body"]["resource"]["id"] for r in pdp.requests} == {"all", "own"}


@pytest.mark.parametrize(
    "answer",
    [{"delay": 1.5, "decision": True}, {"status": 500, "body": {}}, {"decision": "true"}, {"body": {}},
     {"raw": "not json"}, {"status": 302, "body": {"decision": True}}, {"close": True}],
    ids=["timeout", "500", "string-true", "no-decision", "not-json", "redirect", "closed"],
)
def test_case_15_pdp_failure_is_503(tes, pdp, repo, answer):
    pdp.decide(answer)
    api = tes(pdp_on=True, decision_service={"timeout": 0.5})
    assert api.create(**ALICE)[0] == 503
    assert repo.tasks == {}


@pytest.mark.parametrize(("method", "path"), [("GET", "/tasks/{id}"), ("POST", "/tasks/{id}:cancel"), ("GET", "/tasks")])
def test_case_15_every_action(tes, pdp, repo, method, path):
    task_id = repo.add("alice")
    pdp.decide({"status": 500, "body": {}})
    assert tes(pdp_on=True).call(method, path.format(id=task_id), **ALICE)[0] == 503
    assert repo.tasks[ObjectId(task_id)].state.value == "QUEUED"


def test_case_17_rule_passes_pdp_denies(tes, pdp):
    pdp.decide(False)
    assert tes(pdp_on=True, required_claim=RULE).create(**ALICE)[0] == 403
    assert len(pdp.requests) == 1


def test_case_18_subject_from_token_only(tes, pdp):
    api = tes(pdp_on=True)
    headers = {"X-Request-ID": "client-request-id", "X-User": "bob"}
    task = dict(TASK, tags={"owner": "bob"})
    assert api.call("POST", "/tasks", body=task, headers=headers, **ALICE)[0] == 200
    (recorded,) = pdp.requests
    assert recorded["body"]["subject"]["id"] == "alice"
    assert recorded["headers"]["X-Request-ID"] not in (None, "client-request-id")


def test_case_19_allowlist_and_no_token(tes, pdp):
    api = tes(pdp_on=True, decision_service={"claims": ["eduperson_entitlement", "missing"]})
    assert api.create(**dict(ALICE, email="a@example.org", groups=[NEAR_MISS]))[0] == 200
    (recorded,) = pdp.requests
    assert recorded["body"]["subject"]["properties"]["claims"] == {"eduperson_entitlement": [SUBGROUP]}
    assert "eyJ" not in json.dumps(recorded["body"])


def test_subject_leaves_out_unknown_keys(tes, pdp):
    api = tes(pdp_on=True)
    assert api.create("bare")[0] == 200
    (recorded,) = pdp.requests
    assert recorded["body"]["subject"] == {
        "type": "user", "id": "bare", "properties": {"iss": api.issuer.url, "sub": "bare", "claims": {}}}


def test_credential_is_sent(tes, pdp, tmp_path):
    path = tmp_path / "credential"
    path.write_text("pep-secret\n")
    assert tes(pdp_on=True, decision_service={"credential_file": str(path)}).create(**ALICE)[0] == 200
    assert pdp.requests[0]["headers"]["Authorization"] == "Bearer pep-secret"


def test_legacy_task_without_owner_has_no_owner_property(tes, pdp, repo):
    task_id = repo.add(None)
    tes(pdp_on=True).call("GET", f"/tasks/{task_id}", **ALICE)
    assert pdp.requests[0]["body"]["resource"]["properties"] == {"state": "QUEUED", "tags": {}}


def test_case_22_builtin_owner_rule(tes, repo):
    api = tes()
    bob_task = api.create("bob")[1]["id"]
    alice_task = api.create(**ALICE)[1]["id"]
    assert api.call("GET", f"/tasks/{bob_task}", **ALICE)[0] == 404
    assert api.call("GET", f"/tasks/{alice_task}", **ALICE)[0] == 200
    assert api.call("POST", f"/tasks/{bob_task}:cancel", **ALICE)[0] == 404
    assert api.call("POST", "/tasks/no-such-task:cancel", **ALICE)[0] == 404
    assert [t["id"] for t in api.call("GET", "/tasks", "bob")[1]["tasks"]] == [bob_task]
    assert api.call("POST", f"/tasks/{alice_task}:cancel", **ALICE)[0] == 200
    assert repo.tasks[ObjectId(bob_task)].state.value == "QUEUED"


def test_settings_from_environment(monkeypatch):
    """Lists come as JSON arrays in environment variables (commas allowed)."""
    monkeypatch.setenv("TESP_API_OAUTH__ENABLE", "true")
    monkeypatch.setenv("TESP_API_AUTHZ__REQUIRED_CLAIM__NAME", "g")
    monkeypatch.setenv("TESP_API_AUTHZ__REQUIRED_CLAIM__PREFIXES", '["urn:a:vo:", "x,y"]')
    monkeypatch.setattr(authz_module, "properties", get_properties())
    monkeypatch.setattr(authz_module, "_authz", None)
    authz = authz_module.get_authz()
    authz.gate(Principal("a", {"g": ["urn:a:vo:sub"]}))
    authz.gate(Principal("a", {"g": "x,y"}))
    with pytest.raises(HTTPException):
        authz.gate(Principal("a", {"g": ["urn:a:vox"]}))


def test_case_01_nothing_configured_without_auth(monkeypatch, repo):
    """Without authentication and authz, every caller sees and cancels every task, as before."""
    monkeypatch.setattr(authz_module, "_authz", Authz({}, auth_enabled=False, service_id=SERVICE_ID))
    client = TestClient(APP)
    task_id = repo.add(None)
    assert client.get(f"/v1/tasks/{task_id}").status_code == 200
    assert [t["id"] for t in client.get("/v1/tasks").json()["tasks"]] == [task_id]
    assert client.post(f"/v1/tasks/{task_id}:cancel").status_code == 200


def test_case_03_and_06_exact_value(tes):
    api = tes(required_claim={"name": "groups", "values": ["a"]})
    assert api.create("u", groups="a")[0] == 200
    assert api.create("u", groups=["b", "a"])[0] == 200
    assert api.create("u", groups=["ab"])[0] == 403


def test_case_13b_scope_change_between_pages(tes, repo, pdp):
    """Keyset cursor: the next page applies the scope of the new evaluation."""
    for owner in ("alice", "bob", "alice", "bob"):
        repo.add(owner)
    pdp.decide(True, **{"ListTasks/all": False})
    api = tes(pdp_on=True)
    first = api.call("GET", "/tasks?page_size=1", **ALICE)[1]
    assert [repo.tasks[ObjectId(t["id"])].author for t in first["tasks"]] == ["alice"]
    pdp.decide(True)
    rest = api.call("GET", f"/tasks?page_token={first['next_page_token']}", **ALICE)[1]["tasks"]
    assert [repo.tasks[ObjectId(t["id"])].author for t in rest] == ["bob", "alice", "bob"]


def test_environment_lists_replace_file_lists(monkeypatch, tmp_path):
    """An environment list replaces the file's list instead of widening it."""
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text('[default]\noauth.enable = true\noauth.issuers = ["https://old.example.org"]\n'
                             'authz.required_claim.name = "g"\nauthz.required_claim.values = ["vo-a"]\n')
    monkeypatch.setenv("TESP_API_OAUTH__ISSUERS", '["https://new.example.org"]')
    monkeypatch.setenv("TESP_API_AUTHZ__REQUIRED_CLAIM__VALUES", '["vo-b"]')
    settings = get_properties([str(settings_file)])
    assert settings.get("authz.required_claim.values") == ["vo-a", "vo-b"]  # what merging alone would give
    monkeypatch.setattr(authz_module, "properties", settings)
    monkeypatch.setattr(authz_module, "_authz", None)
    authz = authz_module.get_authz()
    authz.gate(Principal("a", {"g": "vo-b"}))
    with pytest.raises(HTTPException):
        authz.gate(Principal("a", {"g": "vo-a"}))
    assert replacing_list(settings, "oauth.issuers") == ["https://new.example.org"]
    monkeypatch.delenv("TESP_API_OAUTH__ISSUERS")
    assert replacing_list(get_properties([str(settings_file)]), "oauth.issuers") == ["https://old.example.org"]


def test_token_validator_from_settings(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text('[default]\noauth.issuers = ["https://idp.example.org"]\noauth.audience = ["svc"]\n'
                             'oauth.timeout = 10\noauth.userinfo.enable = true\n')
    monkeypatch.setenv("TESP_API_OAUTH__TIMEOUT", "3.5")
    monkeypatch.setenv("TESP_API_OAUTH__AUDIENCE", '["tesp"]')
    monkeypatch.setattr(endpoint_utils, "properties", get_properties([str(settings_file)]))
    monkeypatch.setattr(endpoint_utils, "_token_validator", None)
    validator = endpoint_utils.get_token_validator()
    assert (validator.issuers, validator.audience, validator.timeout, validator.userinfo_enabled) == (
        ["https://idp.example.org"], ["tesp"], 3.5, True)
    monkeypatch.setenv("TESP_API_OAUTH__USERINFO__ENABLE", "false")
    monkeypatch.setattr(endpoint_utils, "properties", get_properties([str(settings_file)]))
    monkeypatch.setattr(endpoint_utils, "_token_validator", None)
    assert endpoint_utils.get_token_validator().userinfo_enabled is False


class FakeStartupRepository:
    def __init__(self):
        self.migrations = []

    async def init(self):
        pass

    async def migrate_owners(self, issuers):
        self.migrations.append(issuers)


@pytest.fixture
def startup(monkeypatch, oauth_on):
    """Run the app's startup event with a fake repository; returns the repository."""
    app_module = sys.modules["tesp_api.tesp_api"]
    repository = FakeStartupRepository()
    monkeypatch.setattr(app_module, "task_repository", repository)
    monkeypatch.setattr(authz_module, "_authz", Authz({}, auth_enabled=True, service_id=SERVICE_ID))

    def run(validator):
        monkeypatch.setattr(endpoint_utils, "_token_validator", validator)
        asyncio.run(app_module.startup_event())
        return repository

    return run


def test_startup_refuses_oauth_without_issuers(startup):
    with pytest.raises(ValueError):
        startup(None)


def test_startup_refuses_unusable_userinfo(startup, issuer):
    validator = TokenValidator([issuer.url], "tesp-api", userinfo=True)
    validator._issuer_config(issuer.url)["userinfo_endpoint"] = "ftp://x/userinfo"
    with pytest.raises(ValueError):
        startup(validator)


def test_startup_checks_owner_migration(startup):
    issuers = ["https://a.example.org", "https://b.example.org"]
    assert startup(TokenValidator(issuers, "tesp-api")).migrations == [issuers]
    assert startup(TokenValidator(issuers[:1], "tesp-api")).migrations == [issuers, issuers[:1]]
