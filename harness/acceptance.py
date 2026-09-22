"""Deterministic acceptance checks for a persisted research session.

The workflow checkpoint is the source of truth.  This module does not call an
LLM and does not reinterpret a completed pipeline as a valid scientific claim.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core.io import safe_path
from .tools.validation import comparison_metric_errors
from .agents.method_audit import verify_record


APPROVED_RECOMMENDATIONS = {"accept", "weak_accept"}


def _check(checks: list[dict[str, Any]], check_id: str, category: str,
           passed: bool, detail: str, *, required: bool = True) -> None:
    checks.append({
        "id": check_id,
        "category": category,
        "passed": bool(passed),
        "required": required,
        "detail": detail,
    })


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(checks: list[dict[str, Any]], manifest: list[dict[str, Any]],
              session_dir: Path, relative: str, expected: str,
              source: str) -> None:
    try:
        path = safe_path(session_dir, relative)
    except ValueError as exc:
        _check(checks, f"artifact:{relative}", "delivery", False, str(exc))
        return
    exists = path.is_file()
    matches = False
    error = ""
    if exists:
        try:
            matches = path.read_text(encoding="utf-8") == expected
        except (OSError, UnicodeError) as exc:
            error = str(exc)
        manifest.append({
            "path": relative.replace("\\", "/"),
            "source": source,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "matches_checkpoint": matches,
        })
    if matches:
        detail = "matches checkpoint output"
    elif error:
        detail = error
    elif not exists:
        detail = "missing"
    else:
        detail = "content differs from checkpoint output"
    _check(checks, f"artifact:{relative}", "delivery", exists and matches, detail)


def _numeric_metrics(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {key: float(item) for key, item in value.items()
            if isinstance(key, str) and isinstance(item, (int, float))
            and not isinstance(item, bool) and math.isfinite(item)}


def _metric_policy_ok(metrics: dict[str, float], policy: dict[str, Any]) -> tuple[bool, str]:
    required = policy.get("required_metric_keys", [])
    constraints = policy.get("metric_constraints", {})
    if not isinstance(required, list) or not isinstance(constraints, dict):
        return False, "invalid persisted metric policy"
    missing = [key for key in required if key not in metrics]
    violations: list[str] = []
    for key, limits in constraints.items():
        if key not in metrics or not isinstance(limits, dict):
            continue
        if isinstance(limits.get("min"), (int, float)) and metrics[key] < limits["min"]:
            violations.append(f"{key} < {limits['min']}")
        if isinstance(limits.get("max"), (int, float)) and metrics[key] > limits["max"]:
            violations.append(f"{key} > {limits['max']}")
    problems = [*(f"missing {key}" for key in missing), *violations]
    return not problems, "; ".join(problems) if problems else "persisted metric policy satisfied"


def evaluate_session(session_dir: str | Path, state: dict[str, Any],
                     expected_stages: list[str]) -> dict[str, Any]:
    """Evaluate a session without modifying it or running generated code."""
    root = Path(session_dir).resolve()
    checks: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    stages = state.get("stages", {}) if isinstance(state.get("stages"), dict) else {}

    _check(checks, "workflow:completed", "workflow", state.get("status") == "completed",
           f"checkpoint status is {state.get('status', 'missing')!r}")
    incomplete = [stage for stage in expected_stages
                  if stages.get(stage, {}).get("status") != "done"]
    _check(checks, "workflow:all-stages-done", "workflow", not incomplete,
           "all workflow stages are done" if not incomplete else f"not done: {', '.join(incomplete)}")
    direction = state.get("metadata", {}).get("research_direction")
    _check(checks, "workflow:research-question", "workflow",
           isinstance(direction, str) and bool(direction.strip()),
           "research direction persisted" if isinstance(direction, str) and direction.strip()
           else "research direction missing")

    execution = stages.get("code_execution", {}).get("output", {})
    execution = execution if isinstance(execution, dict) else {}
    _check(checks, "execution:succeeded", "execution", execution.get("success") is True,
           execution.get("error") or "execution succeeded")
    _check(checks, "execution:real-entry-point", "execution",
           execution.get("execution_kind") == "entry_point",
           f"execution kind is {execution.get('execution_kind', 'missing')!r}")
    metrics = _numeric_metrics(execution.get("analysis", {}).get("metrics"))
    _check(checks, "execution:numeric-metrics", "execution", bool(metrics),
           f"{len(metrics)} numeric metrics persisted" if metrics else "no numeric metrics persisted")
    policy_ok, policy_detail = _metric_policy_ok(metrics, execution.get("execution_policy", {}))
    _check(checks, "execution:metric-policy", "execution", policy_ok, policy_detail)
    comparison_keys = {"proposed_primary", "baseline_primary", "improvement_delta", "sample_count"}
    required_keys = execution.get("execution_policy", {}).get("required_metric_keys", [])
    comparison_required = isinstance(required_keys, list) and comparison_keys.issubset(required_keys)
    comparison_problems = comparison_metric_errors(metrics)
    comparison_present = comparison_keys.issubset(metrics)
    _check(checks, "execution:comparison-contract", "execution",
           comparison_present and not comparison_problems if comparison_required else True,
           "; ".join(comparison_problems) if comparison_problems
           else ("standard comparison metrics are arithmetically consistent" if comparison_present
                 else "standard comparison contract is not configured"),
           required=comparison_required)

    literature = stages.get("literature", {}).get("output", {})
    sources = literature.get("sources", []) if isinstance(literature, dict) else []
    identified = [source for source in sources if isinstance(source, dict)
                  and (source.get("arxiv_id") or source.get("doi") or source.get("url"))]
    _check(checks, "evidence:identified-sources", "evidence",
           bool(sources) and len(identified) == len(sources),
           f"{len(identified)}/{len(sources)} sources have stable identifiers")

    planning = stages.get("planning", {}).get("output", {})
    novelty = planning.get("novelty_hypothesis") if isinstance(planning, dict) else None
    _check(checks, "evidence:novelty-hypothesis", "evidence",
           isinstance(novelty, str) and bool(novelty.strip()),
           "novelty hypothesis persisted" if isinstance(novelty, str) and novelty.strip()
           else "novelty hypothesis missing")

    metadata = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
    revision_round = metadata.get("revision_round", 0)
    revision_history = metadata.get("revision_history", [])
    method_output = stages.get("method_design", {}).get("output", {})
    revision_response = method_output.get("revision_response") if isinstance(method_output, dict) else None
    consistency_audit = method_output.get("consistency_audit") if isinstance(method_output, dict) else None
    audit_trace_ok = True
    if isinstance(consistency_audit, dict) and "protocol_version" in consistency_audit:
        try:
            verify_record(method_output, consistency_audit)
        except (ValueError, TypeError, KeyError):
            audit_trace_ok = False
    revision_trace_ok = (
        isinstance(revision_round, int) and revision_round >= 0
        and audit_trace_ok
        and isinstance(revision_history, list) and len(revision_history) == revision_round
        and (revision_round == 0 or isinstance(revision_response, list) and bool(revision_response)
             and isinstance(consistency_audit, dict) and consistency_audit.get("valid") is True)
    )
    _check(checks, "trace:revision-lineage", "traceability", revision_trace_ok,
           f"revision round {revision_round} has matching history, response, and method audit"
           if revision_trace_ok else
           "revision round, history, revision_response, and method audit are inconsistent")

    writing = stages.get("paper_writing", {}).get("output", {})
    writing = writing if isinstance(writing, dict) else {}
    written_metrics = _numeric_metrics(writing.get("verified_metrics"))
    _check(checks, "trace:paper-metrics", "traceability", bool(metrics) and written_metrics == metrics,
           "paper metrics equal executed metrics" if bool(metrics) and written_metrics == metrics
           else "paper metrics do not exactly match executed metrics")
    _check(checks, "trace:evidence-scope", "traceability",
           writing.get("evidence_scope") == execution.get("execution_kind") and
           bool(execution.get("execution_kind")),
           f"paper scope={writing.get('evidence_scope')!r}, execution={execution.get('execution_kind')!r}")

    coding = stages.get("coding", {}).get("output", {})
    coding = coding if isinstance(coding, dict) else {}
    files = coding.get("files", []) if isinstance(coding.get("files"), list) else []
    for item in files:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str):
            _artifact(checks, manifest, root, f"code/{item['path']}", item["content"],
                      f"coding.files:{item['path']}")
    _check(checks, "delivery:code-manifest", "delivery", bool(files),
           f"{len(files)} generated code files declared" if files else "no generated code files declared")
    _artifact(checks, manifest, root, "code/requirements.txt", coding.get("dependencies", ""),
              "coding.dependencies")
    if writing:
        _artifact(checks, manifest, root, "output/paper.tex", writing.get("full_paper_latex", ""),
                  "paper_writing.full_paper_latex")
        _artifact(checks, manifest, root, "output/references.bib", writing.get("bibtex_entries", ""),
                  "paper_writing.bibtex_entries")
    documentation = stages.get("documentation", {}).get("output", {})
    if isinstance(documentation, dict):
        _artifact(checks, manifest, root, "README.md", documentation.get("readme", ""),
                  "documentation.readme")

    review = stages.get("self_review", {}).get("output", {})
    review = review if isinstance(review, dict) else {}
    recommendation = review.get("recommendation")
    _check(checks, "review:publication-recommendation", "scientific_review",
           recommendation in APPROVED_RECOMMENDATIONS,
           f"publication recommendation is {recommendation!r}", required=False)
    verdict = review.get("evidence_verdict")
    _check(checks, "review:evidence-verdict", "scientific_review",
           verdict in {"supported", "contradicted"},
           f"evidence verdict is {verdict!r}; accepted values are 'supported' or 'contradicted'")
    blockers = [item for item in review.get("weaknesses", []) if isinstance(item, dict)
                and (item.get("severity") == "critical"
                     or item.get("severity") == "major"
                     and item.get("category", "validity") == "validity")]
    _check(checks, "review:no-validity-blockers", "scientific_review", not blockers,
           "no critical or major validity weaknesses" if not blockers
           else f"{len(blockers)} critical/major validity weaknesses remain")

    recovery_events = sum(len(info.get("attempt_history", []))
                          for info in stages.values() if isinstance(info, dict))
    recovery_events += len(coding.get("repair_history", [])) if isinstance(coding.get("repair_history"), list) else 0
    _check(checks, "recovery:trace", "recovery", recovery_events > 0,
           f"{recovery_events} retry/repair events preserved" if recovery_events
           else "no retry or repair event occurred in this session", required=False)

    failed = [item["id"] for item in checks if item["required"] and not item["passed"]]
    categories: dict[str, dict[str, int | bool]] = {}
    for item in checks:
        category = categories.setdefault(item["category"], {"passed": 0, "failed": 0, "ok": True})
        bucket = "passed" if item["passed"] else "failed"
        category[bucket] += 1
        if item["required"] and not item["passed"]:
            category["ok"] = False
    checkpoint = root / "checkpoint.json"
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session": root.name,
        "checkpoint_updated_at": state.get("_updated_at"),
        "checkpoint_sha256": sha256_file(checkpoint) if checkpoint.is_file() else None,
        "revision_round": revision_round,
        "decision": "accepted" if not failed else "rejected",
        "scope": "research-session evidence package; not publication peer review",
        "required_checks_passed": not failed,
        "failed_required_checks": failed,
        "categories": categories,
        "checks": checks,
        "artifact_manifest": manifest,
    }


def write_report(session_dir: str | Path, report: dict[str, Any]) -> Path:
    path = safe_path(Path(session_dir).resolve(), "acceptance.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
