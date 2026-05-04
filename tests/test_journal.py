import json
from pathlib import Path
from threading import Thread

from src.journal import Journal


def test_writes_jsonl(tmp_path: Path):
    j = Journal(str(tmp_path / "j.jsonl"))
    j.write("test_event", foo="bar", n=42)
    j.write("another", x=[1, 2, 3])
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    e2 = json.loads(lines[1])
    assert e1["event"] == "test_event" and e1["foo"] == "bar" and e1["n"] == 42
    assert e2["event"] == "another" and e2["x"] == [1, 2, 3]
    assert "ts" in e1 and "ts" in e2


def test_creates_parent_dir(tmp_path: Path):
    p = tmp_path / "deep" / "nested" / "j.jsonl"
    j = Journal(str(p))
    j.write("hi")
    assert p.exists()


def test_handles_non_serializable_via_default(tmp_path: Path):
    j = Journal(str(tmp_path / "j.jsonl"))

    class Weird:
        def __str__(self) -> str:
            return "weird-thing"

    j.write("event", thing=Weird())
    line = (tmp_path / "j.jsonl").read_text().strip()
    entry = json.loads(line)
    assert entry["thing"] == "weird-thing"


def test_thread_safe_append(tmp_path: Path):
    j = Journal(str(tmp_path / "j.jsonl"))

    def worker(n: int):
        for i in range(20):
            j.write("e", n=n, i=i)

    threads = [Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    assert len(lines) == 8 * 20
    # every line is valid JSON (no torn writes)
    for ln in lines:
        json.loads(ln)
