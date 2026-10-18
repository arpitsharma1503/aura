"""Small helpers for crash-safe JSON files in ``data/``."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via temp file + rename so a power cut never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def make_private(db_path: Path) -> None:
    """chmod 600 a SQLite database and its WAL/SHM side files, if present.

    SQLite keeps an existing file's mode, so files created before AURA set a
    private umask would otherwise stay world-readable.
    """
    for suffix in ("", "-wal", "-shm"):
        f = db_path.with_name(db_path.name + suffix)
        try:
            if f.exists():
                f.chmod(0o600)
        except OSError:
            pass
