import copy
import json

import pytest

from harness.agents.coder import CoderAgent
from harness.agents.executor import ExecutorAgent
from harness.tools.experiment_contract import validate_contract, verify_experiment_evidence


def contract():
    return {"version": 1, "primary_comparison": "loss", "comparisons": [{
        "id": "loss", "metric_name": "squared error", "definition": "squared error to target zero",
        "unit": "squared units", "direction": "minimize", "sample_unit": "independent seed",
        "pairing": "same generated input for both methods at each seed",
        "evaluation_scope": "synthetic held-out seed trials",
        "sampling_assumptions": "independent seeds; no claim about real populations",
        "proposed_metric": "proposed_primary", "baseline_metric": "baseline_primary",
        "samples_path": "results/pairs.json"}]}


def source(write_pairs=True, rows=None, score=3.0, count=3):
    rows = rows if rows is not None else [{"pair_id": str(i), "proposed": i + 2, "baseline": i + 1}
                                         for i in range(3)]
    return ("import json\nfrom pathlib import Path\n"
            + ("Path('results').mkdir(exist_ok=True)\n"
               + f"Path('results/pairs.json').write_text({json.dumps(json.dumps(rows))})\n" if write_pairs else "")
            + "print('HARNESS_METRICS=' + json.dumps("
            + repr({"proposed_primary": score, "baseline_primary": 2.0,
                    "improvement_delta": score - 2.0, "sample_count": count}) + "))")


def execute(root, program=None, test="", declared=None):
    agent = ExecutorAgent(install_dependencies=False, run_entry_point=True, require_metrics=True,
                          require_experiment_contract=True)
    result = agent.run("code_execution", {
        "files": [{"path": "main.py", "content": program or source()}],
        "dependencies": "", "entry_point": "main.py", "test_snippet": test,
        "experiment_contract": declared or contract(),
    }, {"session_dir": str(root)})
    return agent, result


def test_paired_statistics_preserve_negative_results_and_covariance(tmp_path):
    agent, result = execute(tmp_path)
    assert result["success"]
    summary = result["experiment_evidence"]["comparisons"][0]
    assert summary["paired_mean_difference"] == 1.0  # proposed worse for a loss
    assert summary["paired_standard_error"] == 0.0  # individual-method variance is nonzero
    assert summary["improvement_direction"] == "negative"
    agent.validate_artifacts(result, tmp_path)
    assert verify_experiment_evidence(tmp_path, result, contract())[0] == []


@pytest.mark.parametrize("mutation", ["raw", "cached_summary", "definition", "missing", "expected_contract"])
def test_paired_provenance_rejects_changes(tmp_path, mutation):
    _, result = execute(tmp_path)
    evidence = result["experiment_evidence"]
    record = evidence["comparisons"][0]["artifact"]
    expected = contract()
    if mutation == "raw":
        (tmp_path / record["path"]).write_text("[]")
    elif mutation == "cached_summary":
        evidence["comparisons"][0]["paired_standard_error"] = 0.3
    elif mutation == "definition":
        evidence["contract"]["comparisons"][0]["metric_name"] = "coverage"
    elif mutation == "missing":
        (tmp_path / record["path"]).unlink()
    else:
        expected["comparisons"][0]["direction"] = "maximize"
    assert verify_experiment_evidence(tmp_path, result, expected)[0]


@pytest.mark.parametrize("program, diagnostic", [
    (source(score=5.0), "differs from paired raw sample mean"),
    (source(count=50), "sample_count must describe primary comparison"),
    (source(rows=[{"pair_id": "same", "proposed": 3, "baseline": 2}] * 3), "duplicate pair_id"),
    (source(rows=[{"pair_id": str(i), "proposed": True, "baseline": 2} for i in range(3)]), "finite numeric"),
])
def test_entry_point_cannot_pass_with_inconsistent_samples(tmp_path, program, diagnostic):
    _, result = execute(tmp_path, program)
    assert not result["success"] and diagnostic in result["error"]


def test_smoke_samples_cannot_satisfy_silent_entry_artifact(tmp_path):
    _, result = execute(tmp_path, source(write_pairs=False), test=source())
    assert not result["success"] and "Experiment evidence contract failed" in result["error"]


def test_contract_is_validated_before_codegen_and_execution(tmp_path):
    declared = contract()
    declared["comparisons"][0]["samples_path"] = "../outside.json"
    with pytest.raises(ValueError, match="Unsafe path"):
        execute(tmp_path, declared=declared)
    assert not list(tmp_path.iterdir())
    declared = contract()
    with pytest.raises(ValueError, match="cannot be source"):
        validate_contract(declared, True, ["results/pairs.json"])
    with pytest.raises(ValueError, match="experiment_contract"):
        CoderAgent(require_experiment_contract=True).validate_output({
            "files": [{"path": "main.py", "content": "pass"}], "entry_point": "main.py",
            "dependencies": "", "run_instructions": "python main.py", "test_snippet": "pass"})


def test_generation_preserves_contract_in_manifest_and_output(tmp_path, monkeypatch):
    agent = CoderAgent(require_experiment_contract=True)
    spec = {"files": [{"path": "main.py", "description": "paired experiment"}], "entry_point": "main.py",
            "dependencies": "", "run_instructions": "python main.py", "experiment_contract": contract()}
    replies = iter([json.dumps(spec), source(), "assert True"])
    prompts = []
    def respond(prompt):
        prompts.append(prompt)
        return next(replies)
    monkeypatch.setattr(agent, "_call_llm", respond)
    result = agent.run("coding", {}, {"session_dir": str(tmp_path)})
    assert result["experiment_contract"] == contract()
    assert "experiment_contract" in prompts[1]


def test_contract_substep_resumes_without_regenerating_file_manifest(tmp_path, monkeypatch):
    agent = CoderAgent(require_experiment_contract=True)
    spec = {"files": [{"path": "main.py", "description": "paired experiment"}], "entry_point": "main.py",
            "dependencies": "", "run_instructions": "python main.py"}
    replies = iter([json.dumps(spec), '{"version": 9}', TimeoutError("contract request timed out"),
                    json.dumps(contract()), source(), "assert True"])
    prompts = []
    def respond(prompt):
        prompts.append(prompt)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(agent, "_call_llm", respond)
    with pytest.raises(TimeoutError):
        agent.run("coding", {}, {"session_dir": str(tmp_path)})
    draft = json.loads(next((tmp_path / ".drafts").glob("coding_*.json")).read_text(encoding="utf-8"))
    assert draft["manifest"]["files"] == spec["files"] and draft["files"] == []
    failure = json.loads(next((tmp_path / ".drafts/failures").glob("*.json")).read_text(encoding="utf-8"))
    assert failure["raw"] == '{"version": 9}' and failure["kind"] == "contract"
    result = agent.run("coding", {}, {"session_dir": str(tmp_path)})
    assert result["experiment_contract"] == contract()
    assert prompts[1].startswith("Return ONLY the experiment_contract")
    assert prompts[3].startswith("Return ONLY the experiment_contract")
    assert sum("You are implementing a research prototype" in prompt for prompt in prompts) == 1
