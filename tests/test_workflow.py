import json
from pathlib import Path
import pytest
import yaml
from harness.core.checkpoint import CheckpointManager
from harness.core.workflow import WorkflowEngine
from harness.agents.executor import ExecutorAgent


class Fake:
    def __init__(self, output=None):
        self.output = {} if output is None else output
        self.calls = 0
        self.inputs = None
        self.state = None
    def run(self, stage_id, inputs, state):
        self.calls += 1
        self.inputs, self.state = inputs, state
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class Repairable(Fake):
    def __init__(self, output=None):
        super().__init__(output)
        self.repairs = 0

    def repair(self, stage_id, previous_output, failure_output, state):
        self.repairs += 1
        return {**previous_output, "files": "fixed", "repair_history": [{"changed_files": ["model.py"]}]}


class FailingThenPassing(Fake):
    def run(self, stage_id, inputs, state):
        self.calls += 1
        self.inputs, self.state = inputs, state
        if inputs["files"] == "broken":
            return {"success": False, "error": "shape mismatch", "runs": []}
        return {"success": True, "error": ""}


def make_engine(tmp_path, stages, agents):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(yaml.safe_dump({"name": "test", "stages": stages}), encoding="utf-8")
    cp = CheckpointManager(tmp_path / "sessions", "test")
    return WorkflowEngine(workflow, cp, agents), cp


def test_nested_input_and_topological_order(tmp_path):
    a, b = Fake({"analysis": {"summary": "ok"}}), Fake()
    engine, cp = make_engine(tmp_path, [
        {"id": "b", "agent": "b", "input_from": {"summary": "a.analysis.summary"}},
        {"id": "a", "agent": "a"}], {"a": a, "b": b})
    state = engine.run()
    assert state["status"] == "completed"
    assert b.inputs["summary"] == "ok"
    assert a.state["session_dir"] == str(cp.session_dir)


@pytest.mark.parametrize("output", [{"parse_error": True, "raw": "bad"},
    {"success": False, "error": "failed"}, [], None])
def test_invalid_results_block_downstream(tmp_path, output):
    a, b = Fake(), Fake()
    a.output = output
    engine, cp = make_engine(tmp_path, [
        {"id":"a", "agent":"a", "max_retries":1},
        {"id":"b", "agent":"b", "depends_on":["a"]}], {"a":a, "b":b})
    state = engine.run()
    assert state["status"] == "failed"
    assert state["completed_stages"] == []
    assert a.calls == 2 and b.calls == 0
    assert cp.load()["stages"]["a"]["output"] == output
    assert cp.load()["stages"]["a"]["attempt_history"][0]["output"] == output


def test_no_resume_creates_fresh_state_with_new_inputs(tmp_path):
    a = Fake()
    engine, cp = make_engine(tmp_path, [{"id":"a", "agent":"a"}], {"a":a})
    engine.run(inputs_override={"a":{"direction":"old"}})
    state = engine.run(resume=False, inputs_override={"a":{"direction":"new"}})
    assert a.calls == 2 and a.inputs == {"direction":"new"}
    assert state["stages"]["a"]["attempts"] == 1


def test_reset_invalidates_all_transitive_consumers(tmp_path):
    a, b, c = Fake({"version":1}), Fake(), Fake()
    engine, cp = make_engine(tmp_path, [
        {"id":"a", "agent":"a"},
        {"id":"b", "agent":"b", "input_from":{"data":"a"}},
        {"id":"c", "agent":"c", "depends_on":["b"]}], {"a":a, "b":b, "c":c})
    state = engine.run()
    cp.reset_stage(state, "a")
    assert state["completed_stages"] == []
    a.output = {"version":2}
    engine.run()
    assert a.calls == b.calls == c.calls == 2
    assert b.inputs["data"] == {"version":2}


def test_resume_exhausted_and_interrupted_stages(tmp_path):
    agent = Fake(RuntimeError("transient"))
    engine, cp = make_engine(tmp_path, [{"id":"a", "agent":"a", "max_retries":0}], {"a":agent})
    assert engine.run()["status"] == "failed"
    agent.output = {"ok":True}
    assert engine.run()["status"] == "completed"
    state = cp.load()
    cp.reset_stage(state, "a")
    cp.mark_stage_started(state, "a")
    assert engine.run()["status"] == "completed"
    assert agent.calls == 3


def test_missing_nested_field_is_explicit_failure(tmp_path):
    a, b = Fake({"analysis":{}}), Fake()
    engine, cp = make_engine(tmp_path, [
        {"id":"a","agent":"a"},
        {"id":"b","agent":"b","max_retries":0,"input_from":{"summary":"a.analysis.summary"}}
    ], {"a":a,"b":b})
    state = engine.run()
    assert state["status"] == "failed" and b.calls == 0
    assert "a.analysis.summary" in state["stages"]["b"]["error"]


@pytest.mark.parametrize("stages", [
    [{"id":"a","agent":"a"},{"id":"a","agent":"a"}],
    [{"id":"a","agent":"a","depends_on":["missing"]}],
    [{"id":"a","agent":"a","depends_on":["b"]},{"id":"b","agent":"b","depends_on":["a"]}],
])
def test_reject_invalid_graphs(tmp_path, stages):
    with pytest.raises(ValueError):
        make_engine(tmp_path, stages, {})


def test_skipped_dependencies_propagate(tmp_path):
    a, b = Fake(), Fake()
    engine, cp = make_engine(tmp_path, [
        {"id":"a","agent":"a","condition":"state['metadata']['skip'] == True"},
        {"id":"b","agent":"b","depends_on":["a"]}], {"a":a,"b":b})
    state = engine.run(metadata={"skip":True})
    assert state["status"] == "completed"
    assert state["stages"]["b"]["status"] == "skipped"
    assert a.calls == b.calls == 0


def test_condition_cannot_call_objects():
    with pytest.raises(ValueError):
        WorkflowEngine._eval_condition("().__class__.__base__.__subclasses__()", {})


def test_cached_failed_output_is_retried(tmp_path):
    agent = Fake({"valid": True})
    engine, cp = make_engine(tmp_path, [{"id":"a","agent":"a"}], {"a":agent})
    state = engine.run()
    state["stages"]["a"]["output"] = {"success":False}
    cp.save(state)
    assert engine.run()["status"] == "completed"
    assert agent.calls == 2


def test_failed_execution_repairs_upstream_code_before_retry(tmp_path):
    coder = Repairable({"files": "broken"})
    executor = FailingThenPassing()
    engine, cp = make_engine(tmp_path, [
        {"id": "coding", "agent": "coder"},
        {"id": "execution", "agent": "executor", "max_retries": 1,
         "repair_from": "coding", "input_from": {"files": "coding.files"}},
    ], {"coder": coder, "executor": executor})
    state = engine.run()
    assert state["status"] == "completed"
    assert coder.repairs == 1 and executor.calls == 2
    assert state["stages"]["coding"]["output"]["files"] == "fixed"
    assert state["stages"]["coding"]["repair_attempts"] == 1
    assert "shape mismatch" in state["stages"]["execution"]["errors"]


def test_resume_repairs_final_failure_before_rerunning_code(tmp_path):
    coder = Repairable({"files": "broken"})
    executor = FailingThenPassing()
    engine, cp = make_engine(tmp_path, [
        {"id": "coding", "agent": "coder"},
        {"id": "execution", "agent": "executor", "max_retries": 0,
         "repair_from": "coding", "input_from": {"files": "coding.files"}},
    ], {"coder": coder, "executor": executor})
    assert engine.run()["status"] == "failed"
    assert executor.calls == 1 and coder.repairs == 0
    state = engine.run()
    assert state["status"] == "completed"
    assert executor.calls == 2 and coder.repairs == 1
    assert state["stages"]["coding"]["output"]["files"] == "fixed"


def test_resume_repair_failure_does_not_rerun_unchanged_code(tmp_path):
    class RepairFails(Repairable):
        def repair(self, stage_id, previous_output, failure_output, state):
            self.repairs += 1
            raise TimeoutError("provider timeout")

    coder = RepairFails({"files": "broken"})
    executor = FailingThenPassing()
    engine, cp = make_engine(tmp_path, [
        {"id": "coding", "agent": "coder"},
        {"id": "execution", "agent": "executor", "max_retries": 0,
         "repair_from": "coding", "input_from": {"files": "coding.files"}},
    ], {"coder": coder, "executor": executor})
    assert engine.run()["status"] == "failed"
    assert executor.calls == 1

    state = engine.run()
    assert state["status"] == "failed"
    assert executor.calls == 1 and coder.repairs == 1
    assert "Automatic resume repair failed" in state["stages"]["execution"]["error"]


def test_failed_automatic_repair_does_not_consume_next_execution_attempt(tmp_path):
    class RepairFails(Repairable):
        def repair(self, stage_id, previous_output, failure_output, state):
            self.repairs += 1
            raise TimeoutError("provider timeout")

    coder = RepairFails({"files": "broken"})
    executor = FailingThenPassing()
    engine, cp = make_engine(tmp_path, [
        {"id": "coding", "agent": "coder"},
        {"id": "execution", "agent": "executor", "max_retries": 1,
         "repair_from": "coding", "input_from": {"files": "coding.files"}},
    ], {"coder": coder, "executor": executor})
    state = engine.run()
    assert state["status"] == "failed"
    assert executor.calls == 1 and coder.repairs == 1
    assert "Automatic repair failed" in state["stages"]["execution"]["error"]


def test_resume_repairs_interrupted_stage_from_attempt_history(tmp_path):
    coder = Repairable({"files": "broken"})
    executor = FailingThenPassing()
    engine, cp = make_engine(tmp_path, [
        {"id": "coding", "agent": "coder"},
        {"id": "execution", "agent": "executor", "max_retries": 0,
         "repair_from": "coding", "input_from": {"files": "coding.files"}},
    ], {"coder": coder, "executor": executor})
    state = engine.run()
    assert state["status"] == "failed"
    cp.mark_stage_started(state, "execution")
    state = engine.run()
    assert state["status"] == "completed"
    assert executor.calls == 2 and coder.repairs == 1


def test_real_subprocess_uses_repaired_checkpoint_files(tmp_path):
    generated = {
        "files": [
            {"path": "model.py", "content": "value = 2"},
            {"path": "main.py", "content": "from model import value\nprint(value)"},
        ],
        "entry_point": "main.py",
        "dependencies": "",
        "run_instructions": "python main.py",
        "test_snippet": "from model import value\nassert value == 3",
    }

    class CodeRepair(Repairable):
        def repair(self, stage_id, previous_output, failure_output, state):
            self.repairs += 1
            repaired = {**previous_output, "files": [dict(item) for item in previous_output["files"]]}
            repaired["files"][0]["content"] = "value = 3"
            repaired["repair_history"] = [{"changed_files": ["model.py"]}]
            return repaired

    coder = CodeRepair(generated)
    executor = ExecutorAgent(install_dependencies=False)
    engine, cp = make_engine(tmp_path, [
        {"id": "coding", "agent": "coder"},
        {"id": "execution", "agent": "executor", "max_retries": 1,
         "repair_from": "coding", "input_from": {
             "files": "coding.files", "entry_point": "coding.entry_point",
             "dependencies": "coding.dependencies", "test_snippet": "coding.test_snippet"}},
    ], {"coder": coder, "executor": executor})
    state = engine.run()
    assert state["status"] == "completed" and coder.repairs == 1
    assert state["stages"]["coding"]["output"]["files"][0]["content"] == "value = 3"
    assert len(list(cp.session_dir.glob("execution_*"))) == 2


def test_repair_source_must_be_an_upstream_dependency(tmp_path):
    with pytest.raises(ValueError, match="repair_from"):
        make_engine(tmp_path, [
            {"id": "coding", "agent": "coder"},
            {"id": "execution", "agent": "executor", "repair_from": "coding"},
        ], {})


def test_session_read_does_not_create_directories(tmp_path):
    cp = CheckpointManager(tmp_path / "sessions", "read_only")
    assert cp.list_sessions() == []
    cp.load()
    assert not cp.sessions_dir.exists()


@pytest.mark.parametrize("name", ["../escape", "C:\\escape", "/escape", "NUL"])
def test_invalid_session_id(tmp_path, name):
    with pytest.raises(ValueError):
        CheckpointManager(tmp_path, name)


def test_changed_definition_invalidates_cache(tmp_path):
    agent = Fake()
    engine, cp = make_engine(tmp_path, [{"id":"a","agent":"a","inputs":{"v":1}}], {"a":agent})
    engine.run()
    engine, _ = make_engine(tmp_path, [{"id":"a","agent":"a","inputs":{"v":2}}], {"a":agent})
    engine.run()
    assert agent.calls == 2 and agent.inputs["v"] == 2
