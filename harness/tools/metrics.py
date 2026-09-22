"""Capture bounded numeric evidence independently of the human-readable log tail."""
import json
import math


def flatten_numeric_metrics(value, prefix=""):
    flattened = {}
    if not isinstance(value, dict):
        return flattened
    for key, item in value.items():
        if not isinstance(key, str) or not key or "." in key:
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flattened.update(flatten_numeric_metrics(item, path))
        elif (isinstance(item, (int, float)) and not isinstance(item, bool)
              and math.isfinite(item)):
            flattened[path] = item
    return flattened


def parse_metric_line(line):
    try:
        if line.startswith("HARNESS_METRICS="):
            value = json.loads(line.partition("=")[2])
        elif line.startswith("{"):
            envelope = json.loads(line)
            value = envelope.get("HARNESS_METRICS") if isinstance(envelope, dict) else None
        else:
            return None
        return flatten_numeric_metrics(value) if isinstance(value, dict) else None
    except (ValueError, RecursionError, OverflowError):
        return None


def capture_metrics(stream, line_limit=1_048_576):
    """Scan a file without loading unbounded log lines or retaining raw sample arrays."""
    stream.seek(0)
    metrics, errors = {}, []
    while line := stream.readline(line_limit + 1):
        if len(line) > line_limit:
            if (line.startswith(b"HARNESS_METRICS=")
                    or line.startswith(b"{") and b'"HARNESS_METRICS"' in line[:1024]):
                metrics = {}
                errors.append(f"metric line exceeds {line_limit} bytes; save raw samples separately")
                errors = errors[:10]
            while line and not line.endswith(b"\n"):
                line = stream.readline(line_limit + 1)
            continue
        values = parse_metric_line(line.decode("utf-8", errors="replace"))
        if values is not None:
            # Each envelope is a complete observation. Never assemble a result
            # from incompatible fields emitted by separate experiment runs.
            metrics = values
        elif line.startswith(b"HARNESS_METRICS="):
            metrics = {}
            errors = (errors + ["invalid HARNESS_METRICS JSON object"])[:10]
    return metrics, errors
