import json

from harness.acceptance import evaluate_session, write_report


STAGES = ["planning", "literature", "method_design", "coding", "code_execution",
          "self_review", "paper_writing", "documentation"]


def completed_state():
    outputs = {
        "planning": {"novelty_hypothesis": "A falsifiable difference"},
        "literature": {"sources": [{"title": "Source", "arxiv_id": "2401.00001"}]},
        "method_design": {},
        "coding": {"files": [{"path": "main.py", "content": "print('ok')\n"}],
                   "dependencies": ""},
        "code_execution": {
            "success": True,
            "execution_kind": "entry_point",
            "execution_policy": {"required_metric_keys": ["score"],
                                 "metric_constraints": {"score": {"min": 0.5}}},
            "analysis": {"metrics": {"score": 0.75}},
        },
        "self_review": {"recommendation": "weak_accept", "weaknesses": []},
        "paper_writing": {"full_paper_latex": "paper\n", "bibtex_entries": "refs\n",
                          "verified_metrics": {"score": 0.75},
                          "evidence_scope": "entry_point"},
        "documentation": {"readme": "# Result\n"},
    }
    return {
        "status": "completed",
        "completed_stages": list(STAGES),
        "metadata": {"research_direction": "Does it work?"},
        "stages": {stage: {"status": "done", "output": outputs[stage]}
                   for stage in STAGES},
    }


def export_fixture(tmp_path, state):
    (tmp_path / "code").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "code/main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "code/requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "output/paper.tex").write_text("paper\n", encoding="utf-8")
    (tmp_path / "output/references.bib").write_text("refs\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Result\n", encoding="utf-8")


def test_complete_traceable_session_is_accepted(tmp_path):
    state = completed_state()
    export_fixture(tmp_path, state)

    report = evaluate_session(tmp_path, state, STAGES)

    assert report["decision"] == "accepted"
    assert report["required_checks_passed"] is True
    assert len(report["artifact_manifest"]) == 5
    assert all(len(item["sha256"]) == 64 for item in report["artifact_manifest"])
    recovery = next(item for item in report["checks"] if item["id"] == "recovery:trace")
    assert recovery["required"] is False and recovery["passed"] is False


def test_acceptance_rejects_untraceable_claims_and_weak_review(tmp_path):
    state = completed_state()
    state["stages"]["paper_writing"]["output"]["verified_metrics"] = {"score": 0.9}
    state["stages"]["self_review"]["output"].update({
        "recommendation": "weak_reject",
        "weaknesses": [{"severity": "major", "issue": "invalid baseline"}],
    })
    export_fixture(tmp_path, state)
    (tmp_path / "code/main.py").write_text("print('tampered')\n", encoding="utf-8")

    report = evaluate_session(tmp_path, state, STAGES)

    assert report["decision"] == "rejected"
    assert "trace:paper-metrics" in report["failed_required_checks"]
    assert "review:recommendation" in report["failed_required_checks"]
    assert "review:no-major-weaknesses" in report["failed_required_checks"]
    assert "artifact:code/main.py" in report["failed_required_checks"]


def test_acceptance_report_is_machine_readable(tmp_path):
    state = completed_state()
    export_fixture(tmp_path, state)
    report = evaluate_session(tmp_path, state, STAGES)

    path = write_report(tmp_path, report)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["session"] == tmp_path.name
