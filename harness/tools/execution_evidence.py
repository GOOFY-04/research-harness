"""Verify archived process output independently of cached metrics and display tails.

Hashes detect inconsistent artifacts; they are not a signature or a sandbox.
"""
import hashlib
from pathlib import Path

from harness.core.io import safe_path
from harness.tools.metrics import capture_metrics


def verify_execution_evidence(session_dir, execution):
    errors, manifest = [], []
    runs = execution.get("runs")
    if execution.get("evidence_version") != 1 or not isinstance(runs, list) or not runs:
        return ["Missing archived execution evidence; rerun code_execution"], manifest
    seen = set()
    final_metrics = None
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            errors.append(f"run {index}: invalid record")
            continue
        logs = run.get("logs")
        if not isinstance(logs, dict):
            errors.append(f"run {index}: missing logs")
            continue
        for stream in ("stdout", "stderr"):
            label = f"run {index} {stream}"
            record = logs.get(stream)
            try:
                if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                    raise ValueError("missing log record")
                relative = record["path"]
                path = safe_path(Path(session_dir).resolve(), relative)
                if path in seen:
                    raise ValueError("log reused by another stream or run")
                seen.add(path)
                with path.open("rb") as handle:
                    digest, size = hashlib.sha256(), 0
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                    if type(record.get("bytes")) is not int or size != record["bytes"]:
                        raise ValueError("byte count mismatch")
                    if digest.hexdigest() != record.get("sha256"):
                        raise ValueError("SHA256 mismatch")
                    if stream == "stdout":
                        metrics, capture_errors = capture_metrics(handle)
                        if capture_errors or metrics != run.get("emitted_metrics"):
                            raise ValueError("archived metrics differ from run record or are malformed")
                        if index == len(runs) - 1:
                            final_metrics = metrics
                manifest.append({"path": relative, "bytes": size,
                                 "sha256": digest.hexdigest(), "source": label})
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"{label}: {exc}")
        if execution.get("success") is True and (
                run.get("success") is not True or run.get("returncode") != 0
                or run.get("timed_out") is not False):
            errors.append(f"run {index}: successful execution contains failed process")
    last = runs[-1] if isinstance(runs[-1], dict) else {}
    if last.get("role") != execution.get("execution_kind"):
        errors.append("Final process role does not match execution kind")
    if final_metrics is None or final_metrics != execution.get("analysis", {}).get("metrics"):
        errors.append("Final archived stdout does not establish analysis metrics")
    return errors, manifest
