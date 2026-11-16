"""
Authorization: a built-in claim rule and an optional decision service.

Both are off by default; when both are set, both must allow.

- The claim rule (`authz.required_claim`) admits a caller if one claim
  holds one of the configured values or starts with one of the prefixes.
- The decision service (`authz.decision_service`) is an OpenID AuthZEN 1.0
  Access Evaluation endpoint that gets one question per decision, in the
  'tes-authzen/1' profile: the subject (owner id, 'iss', 'sub',
  'client_id', 'scope' and allowlisted claims; never the token), the TES
  operationId as action, and a 'tes_task' (get, cancel) or a
  'tes_task_set' 'own'/'all' (create, list) as resource. Without it, the
  built-in owner rule applies: callers see and cancel only their own tasks.

Any failure of the decision service denies with 503.
"""
import asyncio
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException, status
from loguru import logger

from tesp_api.config.properties import properties, replacing_list
from tesp_api.repository.model.task import TesTask

PROFILE = "tes-authzen/1"
DEFAULT_SERVICE_ID = "fi.muni.cz.tesp"
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True)
class Principal:
    """
    The caller: owner id (None without authentication), validated token
    claims and, with oauth.userinfo.enable, the issuer's userinfo answer.
    """

    id: Optional[str]
    claims: Dict[str, Any] = field(default_factory=dict)
    userinfo: Dict[str, Any] = field(default_factory=dict)

    def claim(self, name: str) -> Any:
        """Exact top-level name before dotted path; token before userinfo."""
        for source in (self.claims, self.userinfo):
            if source.get(name) is not None:
                return source[name]
        for source in (self.claims, self.userinfo):
            value: Any = source
            for part in name.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if value is not None:
                return value
        return None


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task[{task_id}] not found")


class Authz:
    """The claim rule and, if configured, the decision service."""

    def __init__(self, settings: Dict[str, Any], auth_enabled: bool, service_id: str):
        rule = dict(settings.get("required_claim") or {})
        pdp = dict(settings.get("decision_service") or {})
        self._claim = rule.get("name")
        self._values = _string_list(rule.get("values"), "authz.required_claim.values")
        self._prefixes = tuple(_string_list(rule.get("prefixes"), "authz.required_claim.prefixes"))
        self._url = pdp.get("url")
        self._timeout = pdp.get("timeout", 2)
        self._allowlist = _string_list(pdp.get("claims"), "authz.decision_service.claims")
        self._service_id = service_id
        self._headers: Dict[str, str] = {}

        if (self._claim or self._values or self._prefixes or pdp) and not auth_enabled:
            raise ValueError("authz is configured but authentication is disabled")
        if (self._values or self._prefixes) and not self._claim:
            raise ValueError("authz.required_claim.name is not set")
        if self._claim and not (self._values or self._prefixes):
            raise ValueError("authz.required_claim needs values or prefixes")
        if "" in self._values or "" in self._prefixes:
            raise ValueError("authz.required_claim values and prefixes must not be empty")
        if pdp and not self._url:
            raise ValueError("authz.decision_service.url is not set")
        if not self._url:
            return
        url = urlsplit(self._url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("authz.decision_service.url must be an http(s) URL with a host")
        if isinstance(self._timeout, bool) or not isinstance(self._timeout, (int, float)) or self._timeout <= 0:
            raise ValueError("authz.decision_service.timeout must be a positive number of seconds")
        if pdp.get("credential_file"):
            try:
                with open(pdp["credential_file"]) as credential_file:
                    credential = credential_file.read().strip()
            except OSError as e:
                raise ValueError(f"authz.decision_service.credential_file is unreadable: {e}")
            if not credential:
                raise ValueError("authz.decision_service.credential_file is empty")
            self._headers["Authorization"] = "Bearer " + credential
        if url.scheme == "http" and not _is_loopback(url.hostname):
            logger.warning("The decision service URL uses plain http to a non-loopback host; "
                           "secure the connection, e.g. with mesh mTLS")

    def gate(self, principal: Principal) -> None:
        """Apply the claim rule; raise 403 unless it is off or admits."""
        if not self._claim:
            return
        value = principal.claim(self._claim)
        values = [value] if isinstance(value, str) else value
        if not (isinstance(values, list) and all(isinstance(v, str) for v in values)
                and any(v in self._values or v.startswith(self._prefixes) for v in values)):
            raise _forbidden()

    async def check_create(self, principal: Principal, task: TesTask) -> None:
        """Raise 403 unless the decision service (if any) allows creation."""
        if self._url is None:
            return
        resource = {"type": "tes_task_set", "id": "own", "properties": {"task": project(task)}}
        if not await self._ask(principal, "CreateTask", resource):
            raise _forbidden()

    async def may_read(self, principal: Principal, task_id: str, meta: Optional[Dict[str, Any]], view: str) -> bool:
        """Whether the caller may read the task; asked even if it is missing."""
        if self._url is None:
            return _owns(principal, meta)
        return await self._ask(principal, "GetTask", _task_resource(task_id, meta), {"view": view})

    async def check_cancel(self, principal: Principal, task_id: str, meta: Optional[Dict[str, Any]]) -> None:
        """
        Raise 404 or 403 unless the caller may cancel the task. A denied or
        missing cancel also asks GetTask, so missing and foreign tasks cost
        the same calls; 403 only if the task exists and may be read.
        """
        if self._url is None:
            if not _owns(principal, meta):
                raise _not_found(task_id)
            return
        resource = _task_resource(task_id, meta)
        if await self._ask(principal, "CancelTask", resource) and meta is not None:
            return
        if await self._ask(principal, "GetTask", resource) and meta is not None:
            raise _forbidden()
        raise _not_found(task_id)

    async def lists_all(self, principal: Principal, view: str) -> bool:
        """True: list all tasks; False: only the caller's; 403 if neither."""
        if self._url is None:
            return False
        list_all, list_own = await asyncio.gather(*(
            self._ask(principal, "ListTasks", {"type": "tes_task_set", "id": task_set}, {"view": view})
            for task_set in ("all", "own")))
        if not (list_all or list_own):
            raise _forbidden()
        return list_all

    def _subject(self, principal: Principal) -> Dict[str, Any]:
        """The AuthZEN subject: validated identity, never the token."""
        token = principal.claims
        properties = {key: value for key, value in (
            ("iss", token.get("iss")),
            ("sub", token.get("sub")),
            ("client_id", token.get("azp") or token.get("client_id")),
        ) if value is not None}
        scope = token.get("scope", token.get("scp"))
        if isinstance(scope, str):
            scope = scope.split()
        if isinstance(scope, list):
            properties["scope"] = scope
        properties["claims"] = {
            name: value for name in self._allowlist if (value := principal.claim(name)) is not None}
        return {"type": "user", "id": principal.id, "properties": properties}

    async def _ask(self, principal: Principal, action: str, resource: Dict[str, Any],
                   action_properties: Optional[Dict[str, Any]] = None) -> bool:
        """Ask the decision service once; any failure raises 503."""
        request_id = str(uuid.uuid4())
        body = {
            "subject": self._subject(principal),
            "action": {"name": action, "properties": action_properties or {}},
            "resource": resource,
            "context": {"tes": {"profile": PROFILE, "service_id": self._service_id}},
        }
        answer: Any = None
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            # One session per evaluation: simple and safe across event loops,
            # at the cost of a new connection per call.
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._url, json=body, allow_redirects=False,
                                        headers={**self._headers, "X-Request-ID": request_id}) as response:
                    cause = f"status {response.status}"
                    if response.status == 200:
                        answer = json.loads(await response.read())
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            cause = type(e).__name__
        decision = answer.get("decision") if isinstance(answer, dict) else None
        if not isinstance(decision, bool):
            logger.error(f"Decision service failed ({cause}) for {action} on {resource['type']} "
                         f"{resource['id']}, request {request_id}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Authorization service unavailable", headers={"Retry-After": "1"})
        logger.info(f"Decision {decision} for {action} on {resource['type']} {resource['id']}, request {request_id}")
        return decision


def strip_url(url: str) -> Optional[str]:
    """
    Remove credentials, query and fragment from a URL, or return None to
    omit it. A plain string algorithm (profile 'tes-authzen/1'): URL
    parsers differ between languages, and falling back to the raw value
    would leak.
    """
    if any(c in url for c in "\t\n\r"):
        return None  # WHATWG clients delete these before parsing.
    scheme = _SCHEME.match(url)
    if not scheme:
        return None if "@" in url or "?" in url else url
    start = scheme.end()
    slash = url.find("/", start)
    end = len(url) if slash < 0 else slash
    region, rest = url[start:end], url[end:]
    query = _first_of(region, "?#")
    at = region.rfind("@")
    if at >= 0 and query >= 0 and at > query or "\\" in region:
        return None  # ambiguous; WHATWG clients read a backslash as '/'.
    cut = _first_of(rest, "?#")
    if not region and "@" in (rest if cut < 0 else rest[:cut]):
        return None  # empty authority, '@' in the path: WHATWG skips the extra slashes.
    if query >= 0:
        region, rest = region[:query], region[query:] + rest
    authority = region[region.rfind("@") + 1:]
    cut = _first_of(rest, "?#")
    return url[:start] + authority + (rest if cut < 0 else rest[:cut])


def project(task: TesTask) -> Dict[str, Any]:
    """
    The fixed projection of a submitted task for CreateTask. Leaves out what
    may hold secrets: commands, environments, input contents and URL
    credentials.
    """
    resources = task.resources
    return _without_none({
        "name": task.name,
        "tags": task.tags,
        "resources": resources and _without_none({
            "cpu_cores": resources.cpu_cores,
            "ram_gb": resources.ram_gb,
            "disk_gb": resources.disk_gb,
            "preemptible": resources.preemptible,
            "zones": resources.zones,
        }),
        "volumes": task.volumes and [str(volume) for volume in task.volumes],
        "executors": [{"image": executor.image} for executor in task.executors],
        "inputs": task.inputs and [_file(item) for item in task.inputs],
        "outputs": task.outputs and [_file(item) for item in task.outputs],
    })


_authz: Optional[Authz] = None


def get_authz() -> Authz:
    """The hook for the configured settings; built once, on first use."""
    global _authz
    if _authz is None:
        settings = _lower_keys(properties.get("authz") or {})
        for section, name in (("required_claim", "values"), ("required_claim", "prefixes"),
                              ("decision_service", "claims")):
            value = replacing_list(properties, f"authz.{section}.{name}")
            if value is not None:
                settings.setdefault(section, {})[name] = _lower_keys(value)
        auth_enabled = bool(properties.oauth.enable or properties.basic_auth.enable)
        _authz = Authz(settings, auth_enabled, service_id())
    return _authz


def service_id() -> str:
    """The service-info ID, also sent to the decision service."""
    return properties.get("service_info.id") or DEFAULT_SERVICE_ID


def _lower_keys(value: Any) -> Any:
    """Plain dicts and lists with lower-case keys (Dynaconf upper-cases env keys)."""
    if isinstance(value, dict):
        return {str(key).lower(): _lower_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_lower_keys(item) for item in value]
    return value


def _owns(principal: Principal, meta: Optional[Dict[str, Any]]) -> bool:
    """The built-in owner rule; without authentication every task is everyone's."""
    return meta is not None and (principal.id is None or meta["owner"] == principal.id)


def _task_resource(task_id: str, meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A 'tes_task'; no properties if missing, no owner if unknown."""
    properties = {key: value for key, value in (meta or {}).items() if key != "owner" or value}
    return {"type": "tes_task", "id": task_id, "properties": properties}


def _file(item: Any) -> Dict[str, Any]:
    return _without_none({
        "url": strip_url(str(item.url)) if item.url else None,
        "path": str(item.path),
        "type": item.type.value if item.type else None,
    })


def _without_none(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _first_of(value: str, characters: str) -> int:
    positions = [i for i in (value.find(c) for c in characters) if i >= 0]
    return min(positions) if positions else -1


def _string_list(value: Any, key: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of strings")
    return list(value)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
