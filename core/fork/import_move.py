"""Files leaving the import folder are copied, verified, then deleted.

Upstream moves a file with a rename (same disk) or an unverified copy
(another disk). For the import folder the fork is more careful: the file is
copied next to its destination under a temporary name, the copy is read back
and compared with the original (size and SHA-256), and only then is it put in
place and the original removed. A copy that does not match is thrown away and
the import fails with the original untouched. Folders of the import folder
left empty by this are removed (never the import folder itself).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from core.fork import config
from utils.logging_config import get_logger

logger = get_logger("fork.import_move")

_CHUNK = 1024 * 1024
_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}


def staging_root() -> Optional[str]:
    try:
        from core.imports.paths import config_root_path
        from core.settings import config_manager

        root = config_root_path(config_manager.get("import.staging_path", "./Staging") or "")
        return os.path.realpath(root) if root else None
    except Exception as exc:
        logger.debug("import folder not resolved: %s", exc)
        return None


def applies(src: Any) -> Optional[str]:
    """The import folder, when ``src`` is a file inside it and the careful
    move is switched on; else None."""
    if not config.get("import.copy_verify"):
        return None
    root = staging_root()
    if not root or not os.path.isdir(root):
        return None
    real = os.path.realpath(str(src))
    if not os.path.isfile(real) or not real.startswith(root.rstrip(os.sep) + os.sep):
        return None
    return root


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest()


def remove_empty_parents(start: str, root: str) -> int:
    """Remove ``start`` and its parents while they are empty (system litter
    such as .DS_Store does not count), stopping below ``root``."""
    removed = 0
    root = os.path.normpath(root)
    current = os.path.normpath(start)
    while current != root and current.startswith(root + os.sep):
        try:
            names = os.listdir(current)
            if any(name.lower() not in _JUNK for name in names):
                break
            for name in names:
                os.remove(os.path.join(current, name))
            os.rmdir(current)
        except OSError as exc:
            logger.debug("folder %s kept: %s", current, exc)
            break
        removed += 1
        current = os.path.dirname(current)
    return removed


def copy_verify_delete(src: Any, dst: Any, root: str) -> None:
    """Copy ``src`` to ``dst``, prove the copy is identical, then delete
    ``src`` and the import sub-folders it leaves empty. Raises ``OSError`` —
    with the original intact and nothing at ``dst`` changed — when the copy
    cannot be made or does not match."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".ssync-tmp", dir=dst.parent)
    tmp = Path(tmp_name)
    try:
        sha = hashlib.sha256()
        with open(src, "rb") as f_src, os.fdopen(fd, "wb") as f_dst:
            fd = -1
            for chunk in iter(lambda: f_src.read(_CHUNK), b""):
                sha.update(chunk)
                f_dst.write(chunk)
            f_dst.flush()
            os.fsync(f_dst.fileno())
        size = src.stat().st_size
        if tmp.stat().st_size != size or _digest(tmp) != sha.hexdigest():
            raise OSError(f"The copy of {src.name} does not match the original; nothing was changed")
        try:
            shutil.copystat(str(src), str(tmp))
        except OSError:
            pass
        try:
            from core.imports.file_ops import _apply_publish_mode

            _apply_publish_mode(tmp, dst.parent)
        except Exception as exc:
            logger.debug("permissions not widened for %s: %s", dst, exc)
        os.replace(str(tmp), str(dst))   # appears whole, under its final name
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    logger.info("Imported by verified copy: %s -> %s (%s bytes)", src, dst, size)
    try:
        src.unlink()
    except OSError as exc:
        logger.warning("Copied and verified, but the original could not be deleted: %s (%s)", src, exc)
        return
    remove_empty_parents(str(src.parent), root)


__all__ = ["applies", "copy_verify_delete", "remove_empty_parents", "staging_root"]
