"""Private, crash-safe adapter state. One process owns each state directory."""

import fcntl
import hashlib
import json
import os
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("Another adapter owns this state directory") from None

    def close(self):
        self.lock.close()

    def path(self, kind, key):
        if kind not in {"runs", "batches", "executions", "rows", "traces"}:
            raise ValueError("Unknown state kind")
        return self.root / kind / (digest(str(key)) + ".json")

    def get(self, kind, key):
        path = self.path(kind, key)
        return json.loads(path.read_text()) if path.exists() else None

    def all(self, kind):
        return [json.loads(path.read_text()) for path in sorted((self.root / kind).glob("*.json"))]

    def put(self, kind, key, value):
        path = self.path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
