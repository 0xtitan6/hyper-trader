import json
import threading
import time
from pathlib import Path
from typing import Any


class Journal:
    """Append-only JSONL trade journal. Each call to `write` appends one line.

    Thread-safe. Caller is responsible for not putting non-JSON-serializable values
    in fields; falls back to str() for unknowns.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        entry = {"ts": time.time(), "event": event, **fields}
        line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
