"""Bounded private process records; timestamps never imply infinite readiness."""

import json
import os
import tempfile
import time
from pathlib import Path

MAX_RECORD_BYTES = 4 * 1024 * 1024


def runtime_dir() -> Path:
    from src.config import settings

    path = Path(settings.data_dir) / "sync-runtime"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_record(path, record: dict) -> None:
    path = Path(path)
    data = json.dumps(record, separators=(",", ":")).encode()
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError("sync record exceeds bounded size")
    fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_record(path) -> dict:
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_RECORD_BYTES + 1)
        if len(data) > MAX_RECORD_BYTES:
            return {}
        result = json.loads(data)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def heartbeat(**fields) -> dict:
    return {
        "pid": os.getpid(),
        "identity": os.environ.get("EMAILSERVER_PROCESS_IDENTITY", ""),
        "heartbeat": time.monotonic(), "timestamp": time.time(), **fields,
    }


def fresh(record: dict, timeout: float, *, pid=None, identity=None) -> bool:
    age = time.monotonic() - record.get("heartbeat", float("-inf"))
    return bool(
        0 <= age < timeout
        and record.get("pid")
        and record.get("identity")
        and (pid is None or record["pid"] == pid)
        and (identity is None or record["identity"] == identity)
    )
