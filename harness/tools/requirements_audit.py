"""Optional independent executable checks of explicit original research constraints.

These supplement model review and cannot override other acceptance gates.
"""
import hashlib
import json
import math
from pathlib import Path

from harness.tools.execution_evidence import verify_execution_evidence


def audit_context_digest(state):
    coding = state.get("stages", {}).get("coding", {}).get("output") or {}
    execution = state.get("stages", {}).get("code_execution", {}).get("output") or {}
    context = {
        "direction": state.get("metadata", {}).get("research_direction"),
        "coding": {key: coding.get(key) for key in ("files", "dependencies", "entry_point", "experiment_contract")},
        "metrics": execution.get("analysis", {}).get("metrics", {}),
        "logs": [run.get("logs") for run in execution.get("runs", [])],
    }
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def verify_requirements_audit(session_dir, state):
    path = Path(session_dir) / "requirements_audit.json"
    if not path.exists():
        if state.get("metadata", {}).get("requirements_audit_required"):
            return ["Registered original-requirements audit is missing"], [], None
        return [], [], None
    manifest = []
    digest = None
    try:
        with path.open("rb") as file:
            raw = file.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("requirements audit exceeds size limit")
        digest = hashlib.sha256(raw).hexdigest()
        record = json.loads(raw)
        if record.get("version") != 1 or record.get("context_sha256") != audit_context_digest(state):
            raise ValueError("Requirements audit is stale or has an unsupported version")
        execution = record.get("execution")
        if not isinstance(execution, dict) or execution.get("success") is not True:
            raise ValueError("Independent constraint probe did not execute successfully")
        errors, evidence = verify_execution_evidence(session_dir, execution)
        if errors:
            raise ValueError("Constraint probe evidence: " + "; ".join(errors))
        manifest.extend(evidence)
        checks = record.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError("Independent constraint checks are missing")
        metrics = execution.get("analysis", {}).get("metrics", {})
        direction = state.get("metadata", {}).get("research_direction", "")
        seen, failures = set(), []
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"].strip():
                raise ValueError("Constraint check needs an id")
            if check["id"] in seen:
                raise ValueError("Constraint check ids must be unique")
            seen.add(check["id"])
            quote = check.get("requirement_quote")
            if not isinstance(quote, str) or not quote.strip() or quote not in direction:
                raise ValueError("Constraint check must quote the original research direction exactly")
            observed, expected = metrics.get(check.get("metric")), check.get("expected")
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in (observed, expected)):
                raise ValueError("Constraint check requires finite expected and measured values")
            if not math.isclose(observed, expected, rel_tol=1e-9, abs_tol=1e-12):
                failures.append(f"{check['id']}: observed={observed}, expected={expected}; requirement: {quote}")
        manifest.append({"path": path.name, "bytes": len(raw), "sha256": digest,
                         "source": "independent original-requirements audit"})
        return failures, manifest, digest
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return [str(exc)], manifest, digest
