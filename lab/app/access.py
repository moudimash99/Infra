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
import logging
import os
import time

import jwt

log = logging.getLogger("lab.access")
TEAM = os.environ.get("ACCESS_TEAM", "").strip()
AUDS = [a.strip() for a in os.environ.get("ACCESS_AUD", "").split(",") if a.strip()]
PUBLIC_HOSTS = {h.strip().lower() for h in os.environ.get(
    "PUBLIC_HOSTS", "tastematch.machaka.net,deals.machaka.net,catcam.machaka.net").split(",") if h.strip()}

_jwks = jwt.PyJWKClient(f"https://{TEAM}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600) if TEAM else None
_good = {}          # token -> expiry: a page's images and clips don't each re-check the signature


def check(token):
    """True when the token is a valid Access token for us; logs why not otherwise."""
    if not token or not _jwks or not AUDS:
        return False
    now = time.time()
    if _good.get(token, 0) > now:
        return True
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
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


class AccessGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = _header(scope, b"host").split(":")[0].lower()
            if host in PUBLIC_HOSTS:
                token = _header(scope, b"cf-access-jwt-assertion") or _cookie(scope, "CF_Authorization")
                if not await asyncio.to_thread(check, token):
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                            (b"cache-control", b"no-store")]})
                    await send({"type": "http.response.body", "body": DENIED})
                    return
        await self.app(scope, receive, send)
