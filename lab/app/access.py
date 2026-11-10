"""The Cloudflare Access check, done by the sites themselves.

Cloudflare Access normally asks for the login before a request reaches the homelab. But a new name works through the
tunnel before anyone adds it to Access, and from the home network Traefik can be reached directly. So each public
name here only answers a request that carries a valid Access token (signed by the team's keys, for one of our Access
applications, not expired) or the lab's own cookie for that name.

The cookie comes from the bridge, for public names that aren't in the Access application yet: such a name sends the
browser to ACCESS_BRIDGE/lab-sso/start (lab.machaka.net, already in the application), where Cloudflare asks for the
login or already has it. The lab there hands back a one-use code good for a minute, and the public name's
/lab-sso/finish turns it into a cookie for that name only, ending when the Access login ends. The key that signs
codes and cookies is made at start and kept in memory: a restart only sends everyone through the redirects again.

Settings (env): ACCESS_TEAM (hannibalproto.cloudflareaccess.com), ACCESS_AUD (the Access application's AUD tag; a
comma list if the names sit in different applications), PUBLIC_HOSTS (the names to guard), ACCESS_BRIDGE (a name in
the Access application whose /lab-sso Traefik sends here; empty turns the bridge off). Fail closed: without a team,
every public name answers 403. In-cluster names (lab.tools.svc, used by Homepage) aren't guarded for /api/: Traefik
has no route for them, so they can't be reached from outside.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlencode

import jwt

log = logging.getLogger("lab.access")
TEAM = os.environ.get("ACCESS_TEAM", "").strip()
AUDS = [a.strip() for a in os.environ.get("ACCESS_AUD", "").split(",") if a.strip()]
PUBLIC_HOSTS = {h.strip().lower() for h in os.environ.get(
    "PUBLIC_HOSTS", "tastematch.machaka.net,deals.machaka.net,catcam.machaka.net").split(",") if h.strip()}
BRIDGE = os.environ.get("ACCESS_BRIDGE", "").strip().lower()
SSO = "/lab-sso"
COOKIE = "lab_session"

_good = {}          # token -> expiry: a page's images and clips don't each re-check the signature
_keys, _keys_at, _keys_lock = {}, 0.0, threading.Lock()
KEYS_EVERY = 300    # Cloudflare's signing keys are fetched at most every 5 minutes, so made-up key ids can't make
                    # every request refetch them


def _key(kid):
    global _keys, _keys_at
    with _keys_lock:
        if kid not in _keys and time.time() - _keys_at > KEYS_EVERY:
            _keys_at = time.time()      # also after a failed fetch: no retry storm, the old keys stay
            try:
                with urllib.request.urlopen(f"https://{TEAM}/cdn-cgi/access/certs", timeout=5) as r:
                    data = json.load(r)
                _keys = {k["kid"]: jwt.PyJWK(k).key for k in data.get("keys", []) if k.get("kid")}
            except Exception as e:
                log.warning("Access keys unreadable: %s", e)
        return _keys.get(kid)


def cached(token):
    return bool(token) and _good.get(token, 0) > time.time()


def check(token):
    """True when the token is a valid Access token for us; logs why not otherwise."""
    if not token or not TEAM or not AUDS:
        return False
    if cached(token):
        return True
    try:
        key = _key(jwt.get_unverified_header(token).get("kid"))
        if key is None:
            return False
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=AUDS, issuer=f"https://{TEAM}",
                            options={"require": ["exp", "iss", "aud"]})
    except Exception as e:
        log.warning("Access token refused: %s", e)
        return False
    if len(_good) > 500:
        _good.clear()
    _good[token] = float(claims["exp"])
    return True


# ---- the bridge's codes and cookies ---------------------------------------------------------------------------

_secret = secrets.token_bytes(32)   # signs codes and cookies; never leaves this process
_used = {}                          # code id -> expiry: each code works once


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _mac(body):
    return _b64(hmac.new(_secret, body.encode(), hashlib.sha256).digest())


def sign(data):
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    return f"{body}.{_mac(body)}"


def unsign(text, kind):
    """The data in a value sign() made, if it is of that kind and still good; else None."""
    try:
        body, mac = text.split(".")
        if not hmac.compare_digest(mac, _mac(body)):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("k") != kind or not data.get("e", 0) > time.time():
        return None
    return data


def safe_next(path):
    """A path on the same name ('/v/abc?x=1'); anything that could lead elsewhere ('//evil', '/\\evil') becomes '/'."""
    ok = (path.startswith("/") and not path.startswith(("//", "/\\")) and "\\" not in path and path.isascii()
          and path.isprintable() and len(path) <= 2000)
    return path if ok else "/"


def has_session(scope, host):
    data = unsign(_cookie(scope, COOKIE), "session")
    return bool(data) and data.get("h") == host


# ---- responses ------------------------------------------------------------------------------------------------

def _header(scope, name):
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return ""


def _cookie(scope, name):
    for part in _header(scope, b"cookie").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def _query(scope):
    return {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode("latin-1")).items()}


DENIED = ("<!DOCTYPE html><meta name=viewport content='width=device-width'><title>Login needed</title>"
          "<body style='font:16px system-ui;max-width:34rem;margin:3rem auto;padding:0 1rem'>"
          "<h1>Login needed</h1><p>This page only opens through the Cloudflare login.</p>"
          "<p>If you just set this address up: in Cloudflare Zero Trust → Access → Applications, add it to the "
          "homelab's self-hosted application, then reload.</p>").encode()


async def _send(send, status, headers, body=b""):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"cache-control", b"no-store"), *headers]})
    await send({"type": "http.response.body", "body": body})


async def _denied(send, again=False):
    body = DENIED + (b"<p><a href='/'>Open the page again</a> to sign in once more.</p>" if again else b"")
    await _send(send, 403, [(b"content-type", b"text/html; charset=utf-8")], body)


async def _redirect(send, location, cookie=None):
    headers = [(b"location", location.encode("latin-1"))]
    if cookie:
        headers.append((b"set-cookie", cookie.encode("latin-1")))
    await _send(send, 302, headers)


async def _start(scope, send, token):
    """On the bridge, past the Cloudflare login: a one-minute code for the public name that asked."""
    q = _query(scope)
    host = q.get("host", "").lower()
    if host not in PUBLIC_HOSTS:
        return await _denied(send)
    code = sign({"k": "code", "h": host, "n": safe_next(q.get("next", "/")), "x": _good.get(token, 0),
                 "e": time.time() + 60, "i": secrets.token_urlsafe(9)})
    await _redirect(send, f"https://{host}{SSO}/finish?" + urlencode({"code": code}))


async def _finish(scope, send, host):
    """On a public name: the code becomes this name's cookie, once."""
    data = unsign(_query(scope).get("code", ""), "code")
    now = time.time()
    if not data or data.get("h") != host or data.get("i") in _used or not data.get("x", 0) > now:
        if has_session(scope, host):         # this address reloaded after signing in
            return await _redirect(send, "/")
        return await _denied(send, again=True)
    for i, until in list(_used.items()):
        if until < now:
            del _used[i]
    _used[data["i"]] = data["e"]
    cookie = sign({"k": "session", "h": host, "e": data["x"]})
    await _redirect(send, safe_next(data.get("n", "/")),
                    f"{COOKIE}={cookie}; Path=/; Max-Age={int(data['x'] - now)}; Secure; HttpOnly; SameSite=Lax")


# ---- the gate -------------------------------------------------------------------------------------------------

INTERNAL_HOSTS = {"lab.tools.svc", "lab.tools.svc.cluster.local"}   # Homepage's tiles, inside the cluster
DEV = os.environ.get("LAB_DEV") == "1"                               # a development machine: no login anywhere


def _host(host):
    return host.split(":")[0].lower().rstrip(".")


def needs_token(host, path):
    """Fail closed: every request needs the login except the health check (kubelet probes) and the in-cluster
    summary API. A public name spelled another way (case, trailing dot, port) is still a public name."""
    if DEV:
        return False
    if path == "/healthz":
        return False
    return not (_host(host) in INTERNAL_HOSTS and path.startswith("/api/"))


class AccessGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and needs_token(_header(scope, b"host"), scope.get("path", "")):
            host, path = _host(_header(scope, b"host")), scope.get("path", "")
            bridged = bool(BRIDGE) and host in PUBLIC_HOSTS
            if bridged and path == SSO + "/finish":
                return await _finish(scope, send, host)
            token = _header(scope, b"cf-access-jwt-assertion") or _cookie(scope, "CF_Authorization")
            if cached(token) or await asyncio.to_thread(check, token):
                if BRIDGE and host == BRIDGE and path == SSO + "/start":
                    return await _start(scope, send, token)
            elif not (bridged and has_session(scope, host)):
                if bridged and scope.get("method") in ("GET", "HEAD"):
                    qs = scope.get("query_string", b"").decode("latin-1")
                    here = (scope.get("raw_path") or path.encode()).decode("latin-1") + (f"?{qs}" if qs else "")
                    return await _redirect(send, f"https://{BRIDGE}{SSO}/start?" + urlencode({"host": host, "next": here}))
                return await _denied(send)
        await self.app(scope, receive, send)
