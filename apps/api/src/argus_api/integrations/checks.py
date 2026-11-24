"""Network validation of an integration (integrations spec 3.5).

Runs outside any write transaction and fails closed. The host is resolved once,
every address is vetted, and the connection goes to the vetted address with the
original name for Host and TLS (SNI and certificate check), so a second DNS answer
cannot redirect it. No redirects, no proxy from the environment, a deadline for
the whole check, and bounded reads. A detail never quotes a response body: a
server may echo the token it was sent.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx2 as httpx
from argus_core.accounts import GITHUB_HOST

REQUEST_TIMEOUT_S = 10.0
DEADLINE_S = 30.0
MAX_BODY = 64 * 1024
MCP_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
MCP_SUCCESS = "MCP response valid; credentials not explicitly rejected."
MCP_OAUTH = "MCP server requires OAuth sign-in, which Argus does not support."
REVIEWS = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")

Result = tuple[str, str]  # (ok | failed, detail)
Resolver = Callable[[str, int], Awaitable[list[str]]]


_credentialed = ContextVar("integration_credentialed_http", default=False)


class _HTTPLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _credentialed.get()


# httpcore2._trace.Trace formats raw response headers and exceptions; httpx2
# logs the untrusted reason phrase. Filtering at their emitting loggers prevents
# every handler from receiving them. Context-local suppression leaves unrelated
# HTTP traffic and concurrent requests' logging configuration untouched.
_http_filter = _HTTPLogFilter()
for _logger in (
    "httpx2",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
):
    logging.getLogger(_logger).addFilter(_http_filter)


@contextmanager
def _quiet_http():
    scope = _credentialed.set(True)
    try:
        yield
    finally:
        _credentialed.reset(scope)


class _InvalidReply(Exception):
    pass


class _Refused(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class _OAuthRequired(Exception):
    """The server answered 401/403 with an RFC 9728 OAuth challenge instead of
    rejecting the token: sign-in, which Argus does not offer, not a bad token."""


def _split_challenges(value: str) -> list[str]:
    """Split a WWW-Authenticate header on commas outside quoted strings, where a
    backslash escapes the next character (RFC 7230 quoted-string)."""
    parts: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for ch in value:
        if escaped:
            escaped = False
        elif quoted and ch == "\\":
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def oauth_challenge(value: str | None) -> bool:
    """Whether a WWW-Authenticate value carries an RFC 9728 OAuth challenge: a
    Bearer challenge naming the protected-resource metadata URL. A bare Bearer
    challenge (or none) means the token itself was rejected.

    Mirrored by `oauthChallenge` in `integrations/nanoclaw/argus-integrations-mcp.ts`;
    keep their test cases in step."""
    if not value:
        return False
    scheme: str | None = None
    params: set[str] = set()
    for raw in _split_challenges(value):
        part = raw.strip()
        if not part:
            continue
        token, space, rest = part.partition(" ")
        if space and "=" not in token:
            # A new challenge: `Scheme params...`; continuation params carry no
            # scheme of their own.
            if scheme == "bearer" and "resource_metadata" in params:
                return True
            scheme, params = token.lower(), set()
            part = rest.strip()
            if not part:
                continue
        name, eq, _ = part.partition("=")
        if eq:
            params.add(name.strip().lower())
    return scheme == "bearer" and "resource_metadata" in params


def internal_address(host: str) -> bool:
    """Loopback, link-local (cloud metadata), unspecified, multicast or reserved,
    IPv4-mapped forms included: never a target. Private ranges are allowed: an
    MCP server or API server is often on one."""
    name = host.strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK)
    )


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port)
    return [str(info[4][0]) for info in infos]


class _Pinned:
    """Requests to one vetted address, named as the configured host."""

    def __init__(self, client: httpx.AsyncClient, url: str, address: str) -> None:
        original = httpx.URL(url)
        self.client = client
        self.url = original.copy_with(host=address)
        self.host = original.host
        self.netloc = original.netloc.decode()

    async def send(
        self, method: str, headers: dict[str, str], body: dict | None = None
    ) -> tuple[int, bytes, str, str | None]:
        request = self.client.build_request(
            method,
            self.url,
            headers={**headers, "Host": self.netloc},
            json=body,
            extensions={"sni_hostname": self.host},
        )
        res = await self.client.send(request, stream=True)
        try:
            data = b""
            content_type = (
                res.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            async for chunk in res.aiter_bytes():
                data += chunk
                if len(data) > MAX_BODY:
                    raise ValueError("response too large")
                if content_type == "text/event-stream" and body and "id" in body:
                    # A final SSE response need not be followed by EOF. Close our
                    # response stream once its complete matching event arrives.
                    try:
                        _rpc(data, content_type, body["id"])
                    except _InvalidReply:
                        pass
                    else:
                        break
            # The challenge header only decides between two fixed details; it is
            # never quoted into one.
            www_auth = res.headers.get("www-authenticate")
            return (
                res.status_code,
                data,
                content_type,
                (www_auth if isinstance(www_auth, str) else None),
            )
        finally:
            await res.aclose()


async def check(
    kind: str,
    config: dict[str, Any],
    access: str,
    token: str,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolver | None = None,
    repository: tuple[str, str, str] | None = None,
) -> Result:
    """`repository` is the project's (host, owner, repo), for a forge only."""
    if kind == "forge":
        assert repository is not None
        url = forge_url(*repository)
    elif kind == "mcp":
        url = config["url"]
    else:
        url = config["server"].rstrip("/") + REVIEWS
    target = httpx.URL(url)
    try:
        with _quiet_http():
            async with asyncio.timeout(DEADLINE_S):
                addresses = await (resolve or _resolve)(target.host, target.port or 443)
                if not addresses or any(internal_address(a) for a in addresses):
                    return (
                        "failed",
                        "the host resolves to a loopback or link-local address",
                    )
                async with httpx.AsyncClient(
                    transport=transport,
                    timeout=REQUEST_TIMEOUT_S,
                    follow_redirects=False,
                    trust_env=False,
                    verify=_verify(config) if kind == "kubernetes" else True,
                ) as client:
                    pinned = _Pinned(client, url, addresses[0])
                    if kind == "mcp":
                        return await _mcp(pinned, token)
                    if kind == "forge":
                        assert repository is not None
                        return await _forge(pinned, config, access, token, repository)
                    return await _kubernetes(pinned, config["namespace"], access, token)
    except _InvalidReply:
        return "failed", {
            "mcp": "invalid MCP response",
            "forge": "invalid forge response",
        }.get(kind, "invalid Kubernetes authorization response")
    except _OAuthRequired:
        return "failed", MCP_OAUTH
    except _Refused as exc:
        if exc.status in (401, 403):
            return "failed", f"token rejected (HTTP {exc.status})"
        return "failed", f"unexpected HTTP {exc.status}"
    except TimeoutError:
        return "failed", f"no answer within {DEADLINE_S:g} s"
    except (httpx.HTTPError, ssl.SSLError, OSError, ValueError) as exc:
        return "failed", f"unreachable: {type(exc).__name__}"


def _messages(data: bytes, content_type: str) -> list[Any]:
    try:
        if content_type == "application/json":
            return [json.loads(data)]
        if content_type != "text/event-stream":
            raise _InvalidReply
        messages = []
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        # SSE dispatches only at a blank line, never an incomplete final event.
        for event in text.split("\n\n")[:-1]:
            lines = [
                line[5:].removeprefix(" ")
                for line in event.split("\n")
                if line.startswith("data:")
            ]
            if lines:
                messages.append(json.loads("\n".join(lines)))
        return messages
    except (ValueError, UnicodeError, RecursionError):
        raise _InvalidReply from None


def _rpc(data: bytes, content_type: str, request_id: str) -> dict[str, Any]:
    """Decode one response, allowing request-related notifications in SSE."""
    messages = _messages(data, content_type)
    response = None
    for message in messages:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise _InvalidReply
        if "id" not in message and isinstance(message.get("method"), str):
            if (
                content_type != "text/event-stream"
                or "result" in message
                or "error" in message
            ):
                raise _InvalidReply
            continue
        if (
            message.get("id") != request_id
            or "method" in message
            or ("result" in message) == ("error" in message)
            or response is not None
        ):
            raise _InvalidReply
        response = message
    if response is None:
        raise _InvalidReply
    return response


def _modern_error(data: bytes, content_type: str) -> bool:
    # Modern HTTP rejection errors can omit the id. Do not require a matching
    # response id to recognize them and accidentally downgrade on a modern error.
    try:
        messages = [json.loads(data)]
    except (ValueError, UnicodeError, RecursionError):
        try:
            messages = _messages(data, content_type)
        except _InvalidReply:
            return False
    return any(
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and isinstance(message.get("error"), dict)
        and message["error"].get("code") in (-32020, -32021, -32022)
        for message in messages
    )


async def _mcp(pinned: _Pinned, token: str) -> Result:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL,
        "Mcp-Method": "server/discover",
    }
    body = {
        "jsonrpc": "2.0",
        "id": "argus-discover",
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL,
                "io.modelcontextprotocol/clientInfo": {"name": "argus", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    status, data, content_type, www_auth = await pinned.send("POST", headers, body)
    # Streamable HTTP's era fallback: a legacy server commonly rejects modern
    # metadata or an unknown method with 400/404/405. Authentication rejection and
    # recognized modern errors must never trigger a legacy request.
    if status in (400, 404, 405) and not _modern_error(data, content_type):
        legacy_version = LEGACY_PROTOCOLS[0]
    else:
        if not 200 <= status < 300:
            if status in (401, 403) and oauth_challenge(www_auth):
                raise _OAuthRequired
            raise _Refused(status)
        result = _rpc(data, content_type, body["id"]).get("result")
        if not isinstance(result, dict) or not isinstance(
            result.get("capabilities"), dict
        ):
            raise _InvalidReply
        versions = result.get("supportedVersions")
        if (
            result.get("resultType") != "complete"
            or not isinstance(versions, list)
            or not versions
            or not all(isinstance(v, str) and v for v in versions)
        ):
            raise _InvalidReply
        if MCP_PROTOCOL in versions:
            return "ok", MCP_SUCCESS
        legacy_version = next((v for v in LEGACY_PROTOCOLS if v in versions), None)
        if legacy_version is None:
            raise _InvalidReply

    # The 2025 transport version header applies after initialization, once the
    # version has been negotiated. Mcp-Method belongs only to modern discovery.
    headers.pop("Mcp-Method")
    headers.pop("MCP-Protocol-Version")
    body = {
        "jsonrpc": "2.0",
        "id": "argus-initialize",
        "method": "initialize",
        "params": {
            "protocolVersion": legacy_version,
            "capabilities": {},
            "clientInfo": {"name": "argus", "version": "1"},
        },
    }
    status, data, content_type, www_auth = await pinned.send("POST", headers, body)
    if not 200 <= status < 300:
        if status in (401, 403) and oauth_challenge(www_auth):
            raise _OAuthRequired
        raise _Refused(status)
    result = _rpc(data, content_type, body["id"]).get("result")
    if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
        raise _InvalidReply
    info = result.get("serverInfo")
    if (
        result.get("protocolVersion") != legacy_version
        or not isinstance(info, dict)
        or not isinstance(info.get("name"), str)
        or not info["name"]
        or not isinstance(info.get("version"), str)
        or not info["version"]
    ):
        raise _InvalidReply
    return "ok", MCP_SUCCESS


def forge_url(host: str, owner: str, repo: str) -> str:
    """The REST address of a repository (GitHub repositories spec 3.3)."""
    if host == GITHUB_HOST:
        return f"https://api.{GITHUB_HOST}/repos/{owner}/{repo}"
    return f"https://{host}/api/v1/repos/{owner}/{repo}"


def forge_headers(host: str, username: str | None, token: str) -> dict[str, str]:
    """Bearer for GitHub; Basic `username:token` for Gitea, as workers send."""
    if host == GITHUB_HOST:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    basic = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {"Authorization": f"Basic {basic}", "Accept": "application/json"}


async def _forge(
    pinned: _Pinned,
    config: dict[str, Any],
    access: str,
    token: str,
    repository: tuple[str, str, str],
) -> Result:
    """One GET of the project's repository (forge integrations spec 3.3). Both
    forges return `permissions` only to an authenticated caller, so its presence
    is what proves the token was accepted; a public answer proves nothing."""
    host, owner, repo = repository
    headers = forge_headers(host, config.get("username"), token)
    status, data, _, _ = await pinned.send("GET", headers)
    if status == 404:
        return "failed", "repository not found for this token"
    if not 200 <= status < 300:
        raise _Refused(status)
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise _InvalidReply from None
    if not isinstance(parsed, dict):
        raise _InvalidReply
    full_name = parsed.get("full_name")
    if not isinstance(full_name, str) or full_name.lower() != f"{owner}/{repo}":
        return "failed", "the forge answered for another repository"
    permissions = parsed.get("permissions")
    if not isinstance(permissions, dict):
        return "failed", "cannot prove the token was accepted"
    if access == "read_write" and permissions.get("push") is not True:
        return "failed", "token lacks push access"
    if permissions.get("pull") is not True and permissions.get("push") is not True:
        return "failed", "token lacks read access"
    return "ok", f"{owner}/{repo} on {host}: {access.replace('_', ' ')} access"


def _verify(config: dict[str, Any]) -> ssl.SSLContext | bool:
    ca = config.get("ca_data")
    if not ca:
        return True
    return ssl.create_default_context(cadata=base64.b64decode(ca).decode())


async def _kubernetes(
    pinned: _Pinned, namespace: str, access: str, token: str
) -> Result:
    headers = {"Authorization": f"Bearer {token}"}

    async def allowed(attrs: dict[str, str]) -> bool:
        review = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": attrs},
        }
        status, data, _, _ = await pinned.send("POST", headers, review)
        if not 200 <= status < 300:
            raise _Refused(status)
        try:
            parsed = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise _InvalidReply from None
        result = parsed.get("status") if isinstance(parsed, dict) else None
        if (
            not isinstance(result, dict)
            or type(result.get("allowed")) is not bool
            or "evaluationError" in result
        ):
            raise _InvalidReply
        return result["allowed"]

    if not await allowed({"namespace": namespace, "verb": "list", "resource": "pods"}):
        return "failed", f"cannot list pods in namespace {namespace}"
    # ResourceAttributes.namespace omitted means all namespaces for namespaced
    # resources (Kubernetes authorization/v1). These are samples, not an RBAC proof.
    broad = [
        (
            f"{verb} {resource} in all namespaces",
            {"verb": verb, "group": group, "resource": resource},
        )
        for group, resource in (
            ("", "pods"),
            ("", "secrets"),
            ("", "configmaps"),
            ("apps", "deployments"),
            ("apps", "statefulsets"),
            ("apps", "daemonsets"),
            ("batch", "jobs"),
            ("batch", "cronjobs"),
        )
        for verb in ("get", "list", "watch")
    ]
    broad.extend(
        (f"{verb} pods in all namespaces", {"verb": verb, "resource": "pods"})
        for verb in ("create", "patch", "delete")
    )
    broad.extend(
        (
            f"create {resource}/{subresource} in all namespaces",
            {"verb": "create", "resource": resource, "subresource": subresource},
        )
        for resource, subresource in (
            ("pods", "exec"),
            ("pods", "attach"),
            ("pods", "portforward"),
            ("serviceaccounts", "token"),
        )
    )
    broad.extend(
        (
            (
                "create clusterrolebindings",
                {
                    "verb": "create",
                    "group": "rbac.authorization.k8s.io",
                    "resource": "clusterrolebindings",
                },
            ),
            (
                "do anything in all namespaces",
                {"verb": "*", "group": "*", "resource": "*"},
            ),
        )
    )
    for label, attrs in broad:
        if await allowed(attrs):
            return "failed", f"too broad: the token can {label}; bind a namespaced Role"
    if access == "read_only":
        for verb in ("create", "patch", "delete"):
            attrs = {"namespace": namespace, "verb": verb, "resource": "pods"}
            if await allowed(attrs):
                return (
                    "failed",
                    f"not read-only: the token can {verb} pods in {namespace}",
                )
    return (
        "ok",
        f"explicit checks passed for target namespace {namespace} ({access}); "
        "full RBAC isolation not verified",
    )
