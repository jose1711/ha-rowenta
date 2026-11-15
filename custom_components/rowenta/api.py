"""Groupe SEB cloud client (RCU login, Cognito, DCP profile)."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp

from .const import (
    APP_VERSION,
    COGNITO_ENDPOINT,
    COGNITO_LOGIN_KEY,
    COGNITO_POOL_ID,
    IOT_API,
    LOGIN_URL,
    RCU_BASE,
    RCU_CLIENT_ID,
    SEB_API,
    SEB_API_KEY,
)

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=30)

# Network/protocol failures that must surface as SebConnectionError:
# aiohttp errors, timeouts while reading the body, malformed/non-JSON bodies
# (JSONDecodeError is a ValueError) and unexpected payload shapes.
_TRANSPORT_ERRORS = (
    aiohttp.ClientError,
    TimeoutError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)


# OAuth errors that mean the refresh token is no longer valid.
AUTH_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


class SebError(Exception):
    """Generic SEB cloud error."""


class SebAuthError(SebError):
    """Invalid or revoked credentials."""


class SebConnectionError(SebError):
    """Network or server error."""


@dataclass(slots=True)
class Appliance:
    """Paired appliance from the user profile."""

    id: str
    thing_type: str
    name: str
    appliance_model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def topic_base(self) -> str:
        return f"{self.thing_type}/{self.id}"

    @property
    def shadow_base(self) -> str:
        return f"$aws/things/{self.thing_type}-{self.id}/shadow"


@dataclass(slots=True)
class AwsCredentials:
    """Temporary AWS credentials from Cognito."""

    access_key: str
    secret_key: str
    session_token: str
    expiration: float  # unix time


@dataclass(slots=True)
class Market:
    """Market (country) from the app configuration."""

    name: str  # e.g. GS_FR
    perimeter: str  # e.g. ROWENTA_FR


_REDIRECT_RE = re.compile(r"gsmoduser://tokens[#?]([^\s'\"<>]+)")


def parse_redirect(text: str) -> dict[str, str]:
    """Extract tokens from the gsmoduser:// redirect URL."""
    match = _REDIRECT_RE.search(text.strip())
    if not match:
        raise ValueError("no_redirect")
    params = {k: v[0] for k, v in parse_qs(match.group(1)).items()}
    if "refresh_token" not in params:
        raise ValueError("no_refresh_token")
    return params


def user_id_from_tokens(tokens: dict[str, Any]) -> str | None:
    """User ID from the Salesforce `id` URL (…/id/<org>/<user>)."""
    ident = tokens.get("id")
    if not ident:
        return None
    return urlsplit(ident).path.rstrip("/").rsplit("/", 1)[-1] or None


_JS_LOCATION_RE = re.compile(r"window\.location(?:\.href\s*=\s*|\.replace\()\s*[\"']([^\"']+)[\"']")
_FORM_RE = re.compile(r'<form id="([^"]+)"[^>]*action="([^"]+)"')
_VIEWSTATE_RE = re.compile(
    r'<input type="hidden"\s+id="(com\.salesforce\.visualforce\.[A-Za-z]+)" name="[^"]+" value="([^"]*)"'
)
_A4J_RE = re.compile(r"A4J\.AJAX\.Submit\('([^']+)',null,\{'similarityGroupingId':'([^']+)'")
_LOGIN_ACTION_RE = re.compile(
    r"loginAF=function\(userEmail,password\)\{A4J\.AJAX\.Submit\('([^']+)',null,"
    r"\{'similarityGroupingId':'([^']+)'"
)
_META_LOCATION_RE = re.compile(r'<meta name="Location" content="([^"]+)"')
_BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Mobile Safari/537.36"
)
_MAX_LOGIN_STEPS = 12


def _find_redirect(text: str) -> str | None:
    clean = text.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    match = _REDIRECT_RE.search(clean)
    return match.group(0) if match else None


async def _a4j_submit(
    session: aiohttp.ClientSession,
    page: str,
    page_url: str,
    form_id: str,
    action_id: str,
    extra: dict[str, str] | None = None,
) -> str | None:
    """Submit a Visualforce A4J action; return the redirect URL, or None if the
    server answered normally without one (e.g. rejected credentials).

    A missing form or an HTTP error is a server/format problem, never "wrong password".
    """
    form = next((m for m in _FORM_RE.finditer(page) if m.group(1) == form_id), None)
    if form is None:
        raise SebConnectionError(f"login: form {form_id!r} not found")
    data = {
        "AJAXREQUEST": "_viewRoot",
        form_id: form_id,
        **{m.group(1): unescape(m.group(2)) for m in _VIEWSTATE_RE.finditer(page)},
        **(extra or {}),
        action_id: action_id,
    }
    async with session.post(
        urljoin(page_url, unescape(form.group(2))),
        data=data,
        allow_redirects=False,
        headers={"Referer": page_url, "Origin": RCU_BASE},
    ) as resp:
        if resp.status >= 400:
            raise SebConnectionError(f"login: HTTP {resp.status}")
        body = await resp.text()
        nxt = resp.headers.get("Ajax-Location") or resp.headers.get("Location")
    if not nxt and (meta := _META_LOCATION_RE.search(body)):
        nxt = unescape(meta.group(1))
    return urljoin(page_url, nxt) if nxt else None


async def async_login(email: str, password: str) -> dict[str, str]:
    """Log in with e-mail and password (the app's Salesforce Visualforce login).

    Mimics the app webview: ContinuationForExpId → authorize → /Login (A4J) →
    frontdoor → LoginFlow (A4J) → RemoteAccessAuthorizationPage → gsmoduser://tokens#…
    The password is never stored. Uses its own session (cookies), closed afterwards.
    """
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(), headers={"User-Agent": _BROWSER_UA}, timeout=TIMEOUT
    ) as session:
        try:
            async with session.get(LOGIN_URL) as resp:
                text = await resp.text()
            match = _JS_LOCATION_RE.search(text)
            if not match:
                raise SebConnectionError("login: authorize redirect not found")
            async with session.get(unescape(match.group(1))) as resp:
                page = await resp.text()
                page_url = str(resp.url)

            action = _LOGIN_ACTION_RE.search(page)
            if not action:
                raise SebConnectionError("login: unknown login page format")
            url = await _a4j_submit(
                session, page, page_url, action.group(1), action.group(2),
                {"userEmail": email, "password": password},
            )
            if not url:
                # The login form was accepted (HTTP 2xx) but did not redirect.
                raise SebAuthError("login: invalid e-mail or password")

            for _ in range(_MAX_LOGIN_STEPS):
                if url.startswith("gsmoduser://"):
                    return parse_redirect(url)
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        url = urljoin(url, resp.headers["Location"])
                        continue
                    text = await resp.text()
                if redirect := _find_redirect(text):
                    return parse_redirect(redirect)
                if match := _JS_LOCATION_RE.search(text):
                    url = urljoin(url, unescape(match.group(1)))
                    continue
                # Visualforce page that submits an A4J action by itself (e.g. LoginFlow)
                a4j = _A4J_RE.search(text)
                if a4j and _FORM_RE.search(text):
                    if nxt := await _a4j_submit(session, text, url, a4j.group(1), a4j.group(2)):
                        url = nxt
                        continue
                break
        except _TRANSPORT_ERRORS as err:
            raise SebConnectionError(f"login: {err!r}") from err
    raise SebConnectionError("login: no tokens received")


async def async_get_market(session: aiohttp.ClientSession, country: str, lang: str = "en") -> Market:
    """Find the market and profilePerimeter for a country."""
    url = f"{SEB_API}/common-api/v3/configurations/{SEB_API_KEY}"
    params = {"version": APP_VERSION, "country": country.upper(), "lang": lang}
    try:
        async with session.get(url, params=params, headers={"apiKey": SEB_API_KEY}, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise SebConnectionError(f"configuration: HTTP {resp.status}")
            data = await resp.json(content_type=None)
        markets = data.get("markets") or []
    except _TRANSPORT_ERRORS as err:
        raise SebConnectionError(f"configuration: {err!r}") from err
    wanted = country.upper()
    for market in markets:
        if not isinstance(market, dict):
            continue
        rcu, name, perimeter = market.get("rcuMarket"), market.get("name"), market.get("profilePerimeter")
        if not isinstance(name, str) or not isinstance(perimeter, str):
            continue
        if (isinstance(rcu, str) and rcu.upper() == wanted) or name.endswith(f"_{wanted}"):
            return Market(name, perimeter)
    raise SebError(f"Country {country} is not supported")


class SebCloud:
    """Authentication and the REST part of the SEB cloud."""

    def __init__(self, session: aiohttp.ClientSession, refresh_token: str, perimeter: str) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self.perimeter = perimeter
        self._access_token: str | None = None
        self._id_token: str | None = None
        self._token_time = 0.0
        self.user_id: str | None = None
        # Called with the new refresh token if the server ever rotates it.
        self.on_refresh_token: Callable[[str], None] | None = None

    async def async_refresh_tokens(self) -> None:
        """Refresh the access and id token (the id token is valid for minutes only)."""
        # Form body, not query string: the refresh token must never end up in a URL
        # (proxy/server logs, aiohttp error messages).
        form = {
            "grant_type": "refresh_token",
            "client_id": RCU_CLIENT_ID,
            "refresh_token": self._refresh_token,
        }
        try:
            async with self._session.post(
                f"{RCU_BASE}/services/oauth2/token", data=form, timeout=TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
                # Only a rejected refresh token means re-authentication; other
                # errors (rate limit, maintenance, …) are temporary.
                if resp.status in (400, 401) and data.get("error") in AUTH_ERRORS:
                    raise SebAuthError(data.get("error_description") or data["error"])
                if resp.status != 200:
                    raise SebConnectionError(f"oauth: HTTP {resp.status}")
            access_token = data["access_token"]
        except _TRANSPORT_ERRORS as err:
            raise SebConnectionError(f"oauth: {err!r}") from err
        self._access_token = access_token
        self._id_token = data.get("id_token")
        self._token_time = time.monotonic()
        self.user_id = user_id_from_tokens(data) or self.user_id
        # Salesforce does not rotate refresh tokens today; persist it if that changes.
        new_token = data.get("refresh_token")
        if new_token and new_token != self._refresh_token:
            self._refresh_token = new_token
            if self.on_refresh_token:
                self.on_refresh_token(new_token)

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    async def _ensure_access_token(self) -> str:
        if self._access_token is None or time.monotonic() - self._token_time > 600:
            await self.async_refresh_tokens()
        assert self._access_token
        return self._access_token

    def _dcp_headers(self, token: str) -> dict[str, str]:
        return {
            "ApiKey": SEB_API_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Gseb-App-Version": APP_VERSION,
            "X-Consumer-Channel": "MOBILE_APP_ANDROID",
            "Authorization": f"Bearer {token}",
            "Grant-Type": "RCUToken",
            "DCP-Context": self.perimeter,
        }

    async def _dcp_get(
        self, name: str, url: str, params: dict[str, str] | None = None, *, missing: Any = None
    ) -> dict[str, Any]:
        """GET a DCP resource as JSON.

        A 401/403 is retried once with a freshly refreshed access token. Whether the
        login itself is still valid is decided by that refresh (SebAuthError on a
        rejected refresh token); a request that is still refused afterwards is a
        server-side problem (blocked app version, WAF, …), not a reason to re-authenticate.
        `missing` is returned for a 404 when given.
        """
        token = await self._ensure_access_token()
        for attempt in (1, 2):
            try:
                async with self._session.get(
                    url, params=params, headers=self._dcp_headers(token), timeout=TIMEOUT
                ) as resp:
                    status = resp.status
                    data = await resp.json(content_type=None) if status == 200 else None
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise SebConnectionError(f"{name}: {err!r}") from err
            if status in (401, 403) and attempt == 1:
                await self.async_refresh_tokens()
                assert self._access_token
                token = self._access_token
                continue
            if status == 404 and missing is not None:
                return missing
            if status != 200:
                raise SebConnectionError(f"{name}: HTTP {status}")
            if not isinstance(data, dict):
                raise SebConnectionError(f"{name}: unexpected response")
            return data
        raise SebConnectionError(f"{name}: request rejected")  # pragma: no cover

    async def async_get_profile(self) -> dict[str, Any]:
        return await self._dcp_get("profiles/me", f"{SEB_API}/common-api/profiles/me")

    async def async_get_gyro_map(self, appl: Appliance, map_id: int | str) -> dict[str, Any]:
        """Gyro cleaning map: {mapId, timestamp, map: [{index, x, y, type}]}."""
        return await self._dcp_get(
            "map",
            f"{IOT_API}/vacuum/v1/map/{appl.thing_type}/{appl.id}",
            {"mapId": str(map_id), "pageSize": "-1", "index": "1"},
            missing={"mapId": map_id, "map": []},
        )

    async def async_get_appliances(self) -> list[Appliance]:
        """Paired IoT appliances (SEB platform vacuums)."""
        profile = await self.async_get_profile()
        result: list[Appliance] = []
        for product in (profile.get("household") or {}).get("products") or []:
            iot = product.get("iot") or {}
            appl_id, thing_type = iot.get("functionalId"), iot.get("thingType")
            if not appl_id or not thing_type or not thing_type.startswith("VAC"):
                continue
            result.append(
                Appliance(
                    id=appl_id,
                    thing_type=thing_type,
                    name=product.get("name") or thing_type,
                    appliance_model=product.get("appliance"),
                    raw=product,
                )
            )
        return result

    async def _cognito(self, target: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityService.{target}",
        }
        try:
            async with self._session.post(
                COGNITO_ENDPOINT, data=json.dumps(body), headers=headers, timeout=TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status in (400, 401, 403):
                    # The id token was issued seconds ago by a successful refresh, so even
                    # NotAuthorized is not a login problem: retry with backoff instead.
                    kind = str(data.get("__type", ""))
                    raise SebConnectionError(f"cognito {target}: {kind or resp.status}")
                if resp.status != 200:
                    raise SebConnectionError(f"cognito {target}: HTTP {resp.status}")
        except _TRANSPORT_ERRORS as err:
            raise SebConnectionError(f"cognito {target}: {err!r}") from err
        if not isinstance(data, dict):
            raise SebConnectionError(f"cognito {target}: unexpected response")
        return data

    async def async_get_aws_credentials(self) -> AwsCredentials:
        """Exchange a fresh id token for AWS credentials (Cognito developer identity)."""
        await self.async_refresh_tokens()  # the id token lives ~2 min, always use a fresh one
        if not self._id_token:
            raise SebAuthError("id_token missing (openid scope?)")
        logins = {COGNITO_LOGIN_KEY: self._id_token}
        ident = await self._cognito("GetId", {"IdentityPoolId": COGNITO_POOL_ID, "Logins": logins})
        try:
            creds = await self._cognito(
                "GetCredentialsForIdentity", {"IdentityId": ident["IdentityId"], "Logins": logins}
            )
            c = creds["Credentials"]
            return AwsCredentials(
                access_key=c["AccessKeyId"],
                secret_key=c["SecretKey"],
                session_token=c["SessionToken"],
                expiration=float(c.get("Expiration") or 0),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise SebConnectionError(f"cognito: unexpected response {err!r}") from err
