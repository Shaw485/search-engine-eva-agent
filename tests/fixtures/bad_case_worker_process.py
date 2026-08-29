"""Subprocess-only fixture for Bad Case worker supervisor integration tests."""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path


def _result_fd() -> int:
    return int(os.environ["SEARCH_BAD_CASE_RESULT_FD"])


def _write(payload: object) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    framed = struct.pack(">I", len(encoded)) + encoded
    view = memoryview(framed)
    while view:
        view = view[os.write(_result_fd(), view) :]


def _mark(path: str, contents: str) -> None:
    Path(path).write_text(contents, encoding="utf-8")


def main() -> None:
    mode = sys.argv[1]
    if mode == "success":
        _write({"status": "ok"})
        return
    if mode == "secret_absent":
        _write({"secret_absent": "OPENAI_API_KEY" not in os.environ})
        return
    if mode == "stdout_private":
        print("private Query and private product title", flush=True)
        _write({"status": "ok"})
        return
    if mode == "sleep":
        _mark(sys.argv[2], str(os.getpid()))
        time.sleep(60)
        return
    if mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _mark(sys.argv[2], str(os.getpid()))
        while True:
            time.sleep(1)
    if mode == "grandchild":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(
            [sys.executable, "-I", __file__, "grandchild_leaf"],
            close_fds=True,
        )
        _mark(sys.argv[2], f"{os.getpid()} {child.pid}")
        while True:
            time.sleep(1)
    if mode == "grandchild_leaf":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    if mode == "malformed":
        os.write(_result_fd(), struct.pack(">I", 9) + b"{not-json")
        return
    if mode == "oversize":
        encoded = b"x" * (300 * 1024)
        os.write(_result_fd(), struct.pack(">I", len(encoded)))
        view = memoryview(encoded)
        while view:
            view = view[os.write(_result_fd(), view) :]
        return
    if mode == "raw_private_payload":
        _write(
            {
                "query_text": "private wireless mouse Query",
                "title": "private product title",
            }
        )
        return
    if mode == "run_lock":
        from search_quality.bad_cases.artifacts import bad_case_run_lock

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with bad_case_run_lock(Path(sys.argv[2])):
            _mark(sys.argv[3], str(os.getpid()))
            while True:
                time.sleep(1)
    if mode == "try_supervisor_lock":
        from search_quality.bad_cases.artifacts import BadCaseRunInProgress
        from search_quality.bad_cases.supervisor import bad_case_supervisor_lock

        try:
            with bad_case_supervisor_lock(Path(sys.argv[2])):
                _write({"status": "acquired"})
        except BadCaseRunInProgress:
            _write({"status": "busy"})
        return
    raise SystemExit(64)


if __name__ == "__main__":
    main()
