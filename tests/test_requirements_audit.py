import copy
import json
import sys
from pathlib import Path

import pytest

from harness.tools.process import run_command
from harness.tools.requirements_audit import audit_context_digest, verify_requirements_audit


def make_audit(root):
    state = {"metadata": {"research_direction": "Probability at zero must be 0.5."},
             "stages": {"coding": {"output": {"files": [{"path": "main.py", "content": "pass"}]}}}}
    run = run_command([sys.executable, "-c", 'print(\'HARNESS_METRICS={"p": 0.5}\')'],
                      root, 10, log_dir=root / "logs")
    for log in run["logs"].values():
        log["path"] = Path(log["path"]).relative_to(root).as_posix()
    run["role"] = "entry_point"
    record = {"version": 1, "context_sha256": audit_context_digest(state),
              "execution": {"success": True, "execution_kind": "entry_point", "evidence_version": 1,
                            "runs": [run], "analysis": {"metrics": run["emitted_metrics"]}},
              "checks": [{"id": "zero", "metric": "p", "expected": 0.5,
                          "requirement_quote": "Probability at zero must be 0.5."}]}
    return state, record


def save(root, record):
    (root / "requirements_audit.json").write_text(json.dumps(record), encoding="utf-8")


def test_optional_probe_and_passing_original_requirement(tmp_path):
    assert verify_requirements_audit(tmp_path, {}) == ([], [], None)
    state, record = make_audit(tmp_path)
    save(tmp_path, record)
    errors, manifest, digest = verify_requirements_audit(tmp_path, state)
    assert errors == [] and len(manifest) == 3 and len(digest) == 64
    changed = copy.deepcopy(state)
    changed["stages"]["paper_writing"] = {"status": "done"}
    assert audit_context_digest(changed) == audit_context_digest(state)


def test_registered_probe_cannot_be_silently_omitted(tmp_path):
    errors, _, _ = verify_requirements_audit(tmp_path, {"metadata": {"requirements_audit_required": True}})
    assert errors == ["Registered original-requirements audit is missing"]


def test_preserved_ipw_case_reproduces_original_question_failures(tmp_path):
    from experiments.replay_ipw_case import replay

    result = replay(tmp_path)
    assert result["same_metrics"] and result["metric_count"] == 36
    assert result["expected_failures_reproduced"]
    state = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    (tmp_path / "requirements_audit.json").unlink()
    assert "missing" in verify_requirements_audit(tmp_path, state)[0][0]


@pytest.mark.parametrize("mutation, diagnostic", [
    ("wrong", "observed=0.5, expected=0.95"), ("source", "stale"),
    ("direction", "stale"), ("metrics", "stale"), ("log", "mismatch"),
    ("cache", "analysis metrics"), ("quote", "quote the original"),
    ("boolean", "finite"), ("duplicate", "unique"), ("malformed", "object"),
])
def test_probe_rejects_wrong_stale_or_corrupted_evidence(tmp_path, mutation, diagnostic):
    state, record = make_audit(tmp_path)
    if mutation == "wrong":
        record["checks"][0]["expected"] = 0.95
    elif mutation == "source":
        state["stages"]["coding"]["output"]["files"][0]["content"] = "changed"
    elif mutation == "direction":
        state["metadata"]["research_direction"] = "changed question"
    elif mutation == "metrics":
        state["stages"]["code_execution"] = {"output": {"analysis": {"metrics": {"changed": 1}}}}
    elif mutation == "log":
        path = record["execution"]["runs"][0]["logs"]["stdout"]["path"]
        (tmp_path / path).write_text("tampered", encoding="utf-8")
    elif mutation == "cache":
        record["execution"]["analysis"]["metrics"] = {"p": 0.95}
    elif mutation == "quote":
        record["checks"][0]["requirement_quote"] = "a different task"
    elif mutation == "boolean":
        record["checks"][0]["expected"] = True
    elif mutation == "duplicate":
        record["checks"] *= 2
    elif mutation == "malformed":
        record = []
    save(tmp_path, record)
    errors, _, _ = verify_requirements_audit(tmp_path, state)
    assert errors and diagnostic in "; ".join(errors)
