"""Pictures from elsewhere (BGG box art for Taste Match, Okkazeo's for Deals), fetched once, shrunk and kept on disk.

BGG's own thumbnails are 150 px, too small for a phone's screen, and the originals can be megabytes. So a site asks
for a picture by its own key (a game id) and a size; the lab looks the address up in its data (never a URL from
the request: no open proxy), fetches it once, saves a JPEG of that size under LAB_DB's folder/img, and serves that
file from then on. A picture that can't be had answers 404 for a while, and the page shows its placeholder.

Use from a site:
    @app.get("/img/{key}-{size}.jpg")
    async def picture(key: int, size: str):
        return await images.serve("bgg", key, size, lambda: url_of(key))
"""
import asyncio
import io
import logging
import os
import time
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

from .config import LAB_DB

log = logging.getLogger("lab.images")
CACHE = os.path.join(os.path.dirname(LAB_DB) or ".", "img")
SIZES = {"s": 200, "m": 480, "l": 960}          # longest side, px
HOSTS = ("cf.geekdo-images.com", "okkazeo.com", "www.okkazeo.com", "i.ytimg.com")
MAX_BYTES = 20 * 2**20
RETRY_AFTER = 6 * 3600                          # a failed picture is tried again after this

_http = httpx.AsyncClient(timeout=httpx.Timeout(20), follow_redirects=True,
                          headers={"User-Agent": "machaka-lab/1.0 (+https://lab.machaka.net)"})
_locks: dict[str, asyncio.Lock] = {}
_failed: dict[str, float] = {}


def _shrink(data, px):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((px, px), Image.Resampling.LANCZOS)
    if img.mode not in ("RGB", "L"):
        back = Image.new("RGB", img.size, (255, 255, 255))      # transparent PNGs on white, like the box
        back.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = back
    out = io.BytesIO()
    img.save(out, "JPEG", quality=84, optimize=True, progressive=True)
    return out.getvalue()


async def serve(kind, key, size, url_of):
    """The picture `kind`/`key` at `size` (s, m, l). url_of() gives its source address (may block: it runs in a
    thread); None means there is none."""
    if size not in SIZES or not str(key).replace("-", "").isalnum():
        raise HTTPException(404)
    name = f"{kind}-{key}-{size}.jpg"
    path = os.path.join(CACHE, name)
    headers = {"Cache-Control": "public, max-age=2592000, immutable"}
    if os.path.exists(path):
        return FileResponse(path, media_type="image/jpeg", headers=headers)
    if time.time() - _failed.get(name, 0) < RETRY_AFTER:
        raise HTTPException(404)
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        if os.path.exists(path):
            return FileResponse(path, media_type="image/jpeg", headers=headers)
        try:
            url = await asyncio.to_thread(url_of)
            if not url:
                raise ValueError("no picture in the data")
            if urlparse(url).hostname not in HOSTS:
                raise ValueError(f"not a known picture host: {urlparse(url).hostname}")
            r = await _http.get(url)
            r.raise_for_status()
            if urlparse(str(r.url)).hostname not in HOSTS or len(r.content) > MAX_BYTES:
                raise ValueError("redirected elsewhere or too big")
            data = await asyncio.to_thread(_shrink, r.content, SIZES[size])
            os.makedirs(CACHE, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as e:      # noqa: BLE001 - any failure: no picture for a while, the page shows its placeholder
            _failed[name] = time.time()
            log.info("picture %s unavailable: %s", name, e)
            raise HTTPException(404)
        finally:
            _locks.pop(name, None)
    return FileResponse(path, media_type="image/jpeg", headers=headers)
