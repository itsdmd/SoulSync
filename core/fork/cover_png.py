"""``cover.png`` before ``cover.jpg``.

"Download cover.jpg to album folder" writes whatever bytes the art source
served under the name ``cover.jpg``. With the switch ``cover.prefer_png`` on:

* while that file is being fetched, a PNG version of the cover is tried first
  where the source can serve one (Apple's image CDN by extension, the Cover
  Art Archive's original); a source that has none keeps its JPEG;
* a cover that turns out to be PNG data is saved as ``cover.png``, never as a
  PNG hiding under the name ``cover.jpg``;
* a folder that already has a ``cover.png`` is not given a ``cover.jpg``.

Nothing is converted: a JPEG stays a JPEG (re-encoding it would only make a
bigger file with no more detail).
"""

from __future__ import annotations

import os
import re
import threading
import urllib.request
from typing import Any, Callable, List, Optional, Tuple

from core.fork import config, hooks
from utils.logging_config import get_logger

logger = get_logger("fork.cover_png")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_BYTES = 30 * 1024 * 1024
_state = threading.local()


def enabled() -> bool:
    return hooks._active() and bool(config.get("cover.prefer_png"))


def png_variants(url: str) -> List[str]:
    """Addresses worth asking for a PNG of the cover at ``url``, best first."""
    if not url:
        return []
    out: List[str] = []
    if "mzstatic.com" in url:
        out.append(re.sub(r"\.jpe?g(?=$|\?)", ".png", url, flags=re.I))
    elif "coverartarchive.org" in url:
        # the original, whatever was uploaded; the -250/-500/-1200 thumbnails are always JPEG
        out.append(re.sub(r"/front-\d+$", "/front", url))
    return [u for u in out if u != url or "coverartarchive.org" in u]


def fetch_png(url: str) -> Optional[Tuple[bytes, str]]:
    """``(bytes, "image/png")`` when ``url`` or one of its PNG variants serves a
    PNG, else None. A candidate that announces anything else is not read."""
    for candidate in png_variants(url):
        try:
            request = urllib.request.Request(candidate, headers={"Accept": "image/png,image/*;q=0.5"})
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                if (response.info().get_content_type() or "") != "image/png":
                    continue
                data = response.read(_MAX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 - a miss only costs the shortcut
            logger.debug("No PNG at %s: %s", candidate, exc)
            continue
        if data.startswith(PNG_MAGIC) and len(data) <= _MAX_BYTES:
            return data, "image/png"
    return None


def fetch_art(upstream: Callable[[str], Any], url: str) -> Any:
    """The art fetcher's answer, a PNG one first while a cover file is being written."""
    if getattr(_state, "sidecar", False) and url:
        try:
            from core.metadata.artwork import _upgrade_art_url

            wanted = _upgrade_art_url(url)
        except Exception:  # noqa: BLE001
            wanted = url
        found = fetch_png(wanted)
        if found:
            return found
    return upstream(url)


def settle(folder: str) -> bool:
    """Rename a ``cover.jpg`` that holds PNG data to ``cover.png``."""
    jpg = os.path.join(folder or "", "cover.jpg")
    try:
        if not os.path.isfile(jpg):
            return False
        with open(jpg, "rb") as handle:
            if not handle.read(8).startswith(PNG_MAGIC):
                return False
        os.replace(jpg, os.path.join(folder, "cover.png"))
    except OSError as exc:
        logger.debug("cover.jpg not renamed in %s: %s", folder, exc)
        return False
    logger.info("Cover is a PNG: saved as cover.png in %s", folder)
    return True


def download(upstream: Callable[..., Any], album_info: Any, target_dir: str, context: Any, force: bool) -> Any:
    """``download_cover_art`` with the PNG preference applied."""
    if not enabled() or not target_dir:
        return upstream(album_info, target_dir, context, force)
    if os.path.isfile(os.path.join(target_dir, "cover.png")):
        return None                                  # already has one: no cover.jpg beside it
    _state.sidecar = True
    try:
        return upstream(album_info, target_dir, context, force)
    finally:
        _state.sidecar = False
        settle(target_dir)
