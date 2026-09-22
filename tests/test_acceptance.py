import json

from harness.acceptance import evaluate_session, write_report
from harness.agents.executor import ExecutorAgent


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
        "self_review": {"recommendation": "weak_accept", "evidence_verdict": "supported",
                        "claim_scope": "one deterministic synthetic task", "weaknesses": []},
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
    outputs = {key: value["output"] for key, value in state["stages"].items()}
    metrics = outputs["code_execution"]["analysis"]["metrics"]
    source = "print(" + repr("HARNESS_METRICS=" + json.dumps(metrics)) + ")\n"
    outputs["coding"]["files"][0]["content"] = source
    policy = outputs["code_execution"]["execution_policy"]
    outputs["code_execution"].update(ExecutorAgent(install_dependencies=False).run(
        "code_execution", {**outputs["coding"], "entry_point": "main.py"},
        {"session_dir": str(tmp_path)}))
    outputs["code_execution"]["execution_policy"] = policy
    (tmp_path / "code").mkdir(exist_ok=True)
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "code/main.py").write_text(source, encoding="utf-8")
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
    assert len(report["artifact_manifest"]) == 7
    assert all(len(item["sha256"]) == 64 for item in report["artifact_manifest"])
    recovery = next(item for item in report["checks"] if item["id"] == "recovery:trace")
    assert recovery["required"] is False and recovery["passed"] is False


def test_acceptance_rejects_untraceable_claims_and_weak_review(tmp_path):
    state = completed_state()
    state["stages"]["paper_writing"]["output"]["verified_metrics"] = {"score": 0.9}
    state["stages"]["self_review"]["output"].update({
        "recommendation": "weak_reject",
        "evidence_verdict": "invalid",
        "weaknesses": [{"severity": "major", "category": "validity", "issue": "invalid baseline"}],
    })
    export_fixture(tmp_path, state)
    (tmp_path / "code/main.py").write_text("print('tampered')\n", encoding="utf-8")

    report = evaluate_session(tmp_path, state, STAGES)

    assert report["decision"] == "rejected"
    assert "trace:paper-metrics" in report["failed_required_checks"]
    assert "review:evidence-verdict" in report["failed_required_checks"]
    assert "review:no-validity-blockers" in report["failed_required_checks"]
    assert "artifact:code/main.py" in report["failed_required_checks"]


def test_acceptance_report_is_machine_readable(tmp_path):
    state = completed_state()
    export_fixture(tmp_path, state)
    report = evaluate_session(tmp_path, state, STAGES)

    path = write_report(tmp_path, report)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["session"] == tmp_path.name


def test_acceptance_requires_original_process_evidence(tmp_path):
    state = completed_state()
    export_fixture(tmp_path, state)
    execution = state["stages"]["code_execution"]["output"]
    log = execution["runs"][-1]["logs"]["stdout"]
    (tmp_path / log["path"]).write_text('HARNESS_METRICS={"score": 0.95}\n')
    report = evaluate_session(tmp_path, state, STAGES)
    assert "execution:archived-evidence" in report["failed_required_checks"]
    execution.pop("evidence_version")
    report = evaluate_session(tmp_path, state, STAGES)
    check = next(x for x in report["checks"] if x["id"] == "execution:archived-evidence")
    assert not check["passed"] and "rerun code_execution" in check["detail"]


def test_acceptance_enforces_standard_comparison_arithmetic(tmp_path):
    state = completed_state()
    metrics = {"proposed_primary": 0.8, "baseline_primary": 0.7,
               "improvement_delta": 0.5, "sample_count": 100}
    execution = state["stages"]["code_execution"]["output"]
    execution["analysis"]["metrics"] = metrics
    execution["execution_policy"] = {
        "required_metric_keys": list(metrics), "metric_constraints": {"sample_count": {"min": 30}},
    }
    state["stages"]["paper_writing"]["output"]["verified_metrics"] = metrics
    export_fixture(tmp_path, state)

    report = evaluate_session(tmp_path, state, STAGES)
    assert "execution:comparison-contract" in report["failed_required_checks"]

    metrics["improvement_delta"] = 0.1
    execution["analysis"]["metrics"] = metrics
    export_fixture(tmp_path, state)
    report = evaluate_session(tmp_path, state, STAGES)
    assert "execution:comparison-contract" not in report["failed_required_checks"]


def test_revision_requires_traceable_history_and_method_response(tmp_path):
    state = completed_state()
    state["metadata"]["revision_round"] = 1
    state["metadata"]["revision_history"] = [{"round": 1}]
    export_fixture(tmp_path, state)

    report = evaluate_session(tmp_path, state, STAGES)
    assert "trace:revision-lineage" in report["failed_required_checks"]

    state["stages"]["method_design"]["output"]["revision_response"] = ["fixed baseline"]
    state["stages"]["method_design"]["output"]["consistency_audit"] = {
        "valid": True, "issues": [],
    }
    report = evaluate_session(tmp_path, state, STAGES)
    assert "trace:revision-lineage" not in report["failed_required_checks"]


def test_acceptance_detects_method_changes_after_audit(tmp_path):
    from harness.agents.method_audit import candidate_digest

    state = completed_state()
    method = {"algorithm": "predict then update", "revision_response": ["fixed leakage"]}
    method["consistency_audit"] = {
        "protocol_version": 2, "candidate_sha256": candidate_digest(method),
        "initial_review": {"valid": True, "issues": []}, "verification": [],
        "valid": True, "issues": [],
    }
    state["stages"]["method_design"]["output"] = method
    state["metadata"].update(revision_round=1, revision_history=[{"round": 1}])
    export_fixture(tmp_path, state)
    assert evaluate_session(tmp_path, state, STAGES)["decision"] == "accepted"
    method["algorithm"] = "update then predict"
    report = evaluate_session(tmp_path, state, STAGES)
    assert "trace:revision-lineage" in report["failed_required_checks"]
