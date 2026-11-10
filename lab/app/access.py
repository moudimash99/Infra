"""The Cloudflare Access check, done by the sites themselves.

Cloudflare Access normally asks for the login before a request reaches the homelab. But a new name works through the
tunnel before anyone adds it to Access, and from the home network Traefik can be reached directly. So each public
name here only answers a request that carries a valid Access token: signed by the team's keys, for one of our
Access applications, not expired. Anything else gets a 403 that says what to do.

Settings (env): ACCESS_TEAM (hannibalproto.cloudflareaccess.com), ACCESS_AUD (the Access application's AUD tag; a
comma list if the names sit in different applications), PUBLIC_HOSTS (the names to guard). Fail closed: without a
team, every public name answers 403. In-cluster names (lab.tools.svc, used by Homepage) aren't guarded: Traefik has
no route for them, so they can't be reached from outside.
"""
import asyncio
import json
import logging
import os
import threading
import time
import urllib.request

import jwt

log = logging.getLogger("lab.access")
TEAM = os.environ.get("ACCESS_TEAM", "").strip()
AUDS = [a.strip() for a in os.environ.get("ACCESS_AUD", "").split(",") if a.strip()]
PUBLIC_HOSTS = {h.strip().lower() for h in os.environ.get(
    "PUBLIC_HOSTS", "tastematch.machaka.net,deals.machaka.net,catcam.machaka.net").split(",") if h.strip()}

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


DENIED = ("<!DOCTYPE html><meta name=viewport content='width=device-width'><title>Login needed</title>"
          "<body style='font:16px system-ui;max-width:34rem;margin:3rem auto;padding:0 1rem'>"
          "<h1>Login needed</h1><p>This page only opens through the Cloudflare login.</p>"
          "<p>If you just set this address up: in Cloudflare Zero Trust → Access → Applications, add it to the "
          "homelab's self-hosted application, then reload.</p>").encode()


INTERNAL_HOSTS = {"lab.tools.svc", "lab.tools.svc.cluster.local"}   # Homepage's tiles, inside the cluster
DEV = os.environ.get("LAB_DEV") == "1"                               # a development machine: no login anywhere


def needs_token(host, path):
    """Fail closed: every request needs the login except the health check (kubelet probes) and the in-cluster
    summary API. A public name spelled another way (case, trailing dot, port) is still a public name."""
    if DEV:
        return False
    if path == "/healthz":
        return False
    h = host.split(":")[0].lower().rstrip(".")
    return not (h in INTERNAL_HOSTS and path.startswith("/api/"))


class AccessGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if needs_token(_header(scope, b"host"), scope.get("path", "")):
                token = _header(scope, b"cf-access-jwt-assertion") or _cookie(scope, "CF_Authorization")
                if not cached(token) and not await asyncio.to_thread(check, token):
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                            (b"cache-control", b"no-store")]})
                    await send({"type": "http.response.body", "body": DENIED})
                    return
        await self.app(scope, receive, send)
