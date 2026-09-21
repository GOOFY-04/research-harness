"""Shared persistence, path and model-output helpers."""
import json
import os
import re
import tempfile
from pathlib import Path, PureWindowsPath
from contextlib import contextmanager
from time import monotonic, sleep


@contextmanager
def file_lock(path: Path, timeout: float = 0):
    """Nonblocking OS lock; released by the OS even after process termination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if monotonic() >= deadline:
                    raise RuntimeError(f"Resource is already in use: {path}") from exc
                sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def safe_path(base: str | Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("Empty file path")
    portable = relative.replace("\\", "/")
    win = PureWindowsPath(relative)
    parts = portable.split("/")
    if win.drive or win.root or ":" in portable or any(
        p in ("", ".", "..") or p.endswith((" ", ".")) or PureWindowsPath(p).is_reserved() for p in parts
    ):
        raise ValueError(f"Unsafe path: {relative}")
    root = Path(base).resolve()
    target = (root / portable).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError(f"Path escapes output directory: {relative}")
    return target


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows virus scanners and indexers can briefly open the destination
        # without delete sharing, causing os.replace to raise WinError 5. Keep
        # the atomic replace semantics and retry only that narrow error class.
        delays = (0.02, 0.05, 0.1, 0.2)
        for attempt in range(len(delays) + 1):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == len(delays):
                    raise
                sleep(delays[attempt])
    finally:
        Path(name).unlink(missing_ok=True)


def strip_outer_fence(text: str, languages: tuple[str, ...]) -> str:
    """Only remove a fence enclosing the entire response, not inner code blocks."""
    text = text.strip()
    lines = text.splitlines()
    if len(lines) >= 2:
        first = re.fullmatch(r"(`{3,}|~{3,})([\w+-]*)\s*", lines[0])
        if first and first[2].lower() in ("", *languages) and lines[-1].strip() == first[1]:
            return "\n".join(lines[1:-1]).strip()
    return text


def validate_result(output: object) -> None:
    if not isinstance(output, dict):
        raise ValueError("Stage output must be a JSON object")
    if output.get("parse_error") or output.get("success") is False or output.get("error"):
        raise ValueError(str(output.get("error") or "Invalid stage output: parse_error or success=false"))


class OutputValidationError(ValueError):
    def __init__(self, message, output):
        super().__init__(message)
        self.output = output
