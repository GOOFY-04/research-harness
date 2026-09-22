"""Dependency order and cross-file state contracts, including interrupted generation."""
import json

import pytest

from harness.agents.coder import CoderAgent, ordered_manifest
from harness.agents.executor import ExecutorAgent


def manifest(files):
    return {"files": files, "entry_point": "main.py", "dependencies": "",
            "run_instructions": "python main.py"}


def test_file_order_places_dependencies_before_consumers_and_entry_last(tmp_path):
    spec = manifest([
        {"path": "main.py", "description": "driver"},
        {"path": "learner.py", "description": "learner", "depends_on": ["stats.py"]},
        {"path": "stats.py", "description": "stats"},
    ])
    assert [f["path"] for f in ordered_manifest(spec, tmp_path)["files"]] == [
        "stats.py", "learner.py", "main.py"]
    assert spec["files"][0]["path"] == "main.py"


@pytest.mark.parametrize("files, message", [
    ([{"path": "main.py"}, {"path": "MAIN.py"}], "unique"),
    ([{"path": "main.py"}, {"path": "lib/a.py"}, {"path": "lib\\a.py"}], "unique"),
    ([{"path": "main.py", "depends_on": ["missing.py"]}], "declared"),
    ([{"path": "main.py", "depends_on": "a.py"}], "declared"),
    ([{"path": "main.py"}, {"path": "a.py", "depends_on": ["main.py"]}], "cycle"),
    ([{"path": "main.py"}, {"path": "a.py", "depends_on": ["a.py"]}], "cycle"),
])
def test_invalid_file_graph_fails_before_generation(tmp_path, files, message):
    with pytest.raises(ValueError, match=message):
        ordered_manifest(manifest([{**f, "description": "module"} for f in files]), tmp_path)


def test_resumed_driver_receives_exact_stateful_dependency_and_executes(tmp_path, monkeypatch):
    # Calling interval() then observe() looks plausible from signatures, but
    # observe() needs a prediction cached by predict(). Preserve that source
    # across a provider interruption, not just the names of these methods.
    learner = '''class Learner:
    def __init__(self):
        self.pending = None
        self.residuals = []
    def predict(self, x):
        self.pending = x
        return x
    def interval(self, x):
        return (x - 1, x + 1)
    def observe(self, y):
        if self.pending is None:
            return
        self.residuals.append(abs(y - self.pending))
        self.pending = None
'''
    spec = manifest([
        {"path": "main.py", "description": "driver", "depends_on": ["learner.py"]},
        {"path": "learner.py", "description": "stateful learner"},
    ])
    replies = iter([json.dumps(spec), learner, TimeoutError("driver request interrupted")])
    agent = CoderAgent(allowed_dependencies=[])

    def initial_request(_prompt):
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agent, "_call_llm", initial_request)
    state = {"session_dir": str(tmp_path), "stages": {"method_design": {"output": {
        "invariants": ["Every observation updates the calibration state exactly once"]}}},
        "stage_inputs_override": {"method_design": {"review_feedback": {
            "weaknesses": ["Baseline update was silently skipped"]}}}}
    with pytest.raises(TimeoutError):
        agent.run("coding", {}, state)

    driver = '''from learner import Learner
def run():
    learner = Learner()
    for x, y in [(0, 2), (1, 4)]:
        learner.predict(x)
        learner.interval(x)
        learner.observe(y)
    return learner.residuals
if __name__ == '__main__':
    assert run() == [2, 3]
'''
    prompts = []
    resumed = CoderAgent(allowed_dependencies=[])
    remaining = iter([driver, "from main import run\nassert run() == [2, 3]\n"])
    monkeypatch.setattr(resumed, "_call_llm", lambda p: prompts.append(p) or next(remaining))
    output = resumed.run("coding", {}, state)

    assert len(prompts) == 2  # no repeated manifest or learner generation
    assert json.dumps(learner.strip(), ensure_ascii=False) in prompts[0]
    assert "Baseline update was silently skipped" in prompts[0]
    assert "Every observation updates" in prompts[0]
    result = ExecutorAgent(install_dependencies=False, run_entry_point=True).run(
        "code_execution", output, state)
    assert result["success"] is True


def test_empty_legacy_draft_reorders_without_discarding_manifest(tmp_path):
    agent = CoderAgent()
    state = {"session_dir": str(tmp_path)}
    path, digest = agent._draft_path(state, "unchanged")
    spec = manifest([{"path": "main.py", "description": "driver"},
                     {"path": "learner.py", "description": "learner"}])
    agent._save_draft(path, digest, spec, [])
    restored = agent._load_draft(path, digest, state)
    assert [f["path"] for f in restored["manifest"]["files"]] == ["learner.py", "main.py"]

    # Once generated files exist, keep their persisted prefix to avoid replay.
    agent._save_draft(path, digest, spec, [{"path": "main.py", "content": "pass\n"}])
    restored = agent._load_draft(path, digest, state)
    assert restored["manifest"]["files"][0]["path"] == "main.py"
