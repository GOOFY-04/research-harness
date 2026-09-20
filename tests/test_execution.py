import sys
from pathlib import Path
import pytest
from harness.agents.executor import ExecutorAgent
from harness.tools.process import run_command


def inputs(test="from model import value\nassert value == 3", entry="main.py"):
    return {"files":[{"path":"model.py","content":"value = 3"},
                     {"path":"main.py","content":"print('ENTRY_RAN')"}],
            "dependencies":"","entry_point":entry,"test_snippet":test}


def test_real_smoke_subprocess_and_metrics(tmp_path):
    agent = ExecutorAgent(install_dependencies=False, python_executable=sys.executable)
    data = inputs("from model import value\nassert value == 3\nprint('HARNESS_METRICS={\"loss\": 0.2}')")
    result = agent.run("exec", data, {"session_dir":str(tmp_path)})
    assert result["success"] is True
    assert result["execution_kind"] == "smoke_test"
    assert result["analysis"]["metrics"] == {"loss":0.2}
    assert "no full experiment" in result["analysis"]["summary"]
    assert result["execution_policy"]["python_executable"] == str(Path(sys.executable).resolve())
    assert result["execution_policy"]["timeout_seconds"] == 600


def test_configured_python_must_exist():
    with pytest.raises(ValueError, match="was not found"):
        ExecutorAgent(python_executable="definitely-not-a-real-python-executable")


def test_entry_point_used_without_test(tmp_path):
    result = ExecutorAgent(install_dependencies=False).run("exec", inputs(test=""), {"session_dir":str(tmp_path)})
    assert result["success"] is True
    assert "ENTRY_RAN" in result["test_log"]
    assert result["execution_kind"] == "entry_point"


def test_required_metrics_make_silent_entry_point_fail(tmp_path):
    result = ExecutorAgent(install_dependencies=False, require_metrics=True).run(
        "exec", inputs(test=""), {"session_dir": str(tmp_path)})
    assert result["success"] is False
    assert "HARNESS_METRICS" in result["error"]
    assert result["execution_policy"]["install_dependencies"] is False
    assert result["execution_policy"]["require_metrics"] is True


def test_metrics_json_envelope_is_accepted(tmp_path):
    data = inputs(test="")
    data["files"][1]["content"] = (
        "import json\nprint(json.dumps({'HARNESS_METRICS': {'accuracy': 0.75}}))")
    result = ExecutorAgent(install_dependencies=False, require_metrics=True).run(
        "exec", data, {"session_dir": str(tmp_path)})
    assert result["success"] is True
    assert result["analysis"]["metrics"] == {"accuracy": 0.75}


def test_missing_required_metric_keys_fail_entry_point(tmp_path):
    data = inputs(test="")
    data["files"][1]["content"] = "print('HARNESS_METRICS={\"reprojection\": 1.0}')"
    result = ExecutorAgent(
        install_dependencies=False,
        require_metrics=True,
        required_metric_keys=["rotation", "reprojection"],
    ).run("exec", data, {"session_dir": str(tmp_path)})
    assert result["success"] is False
    assert "rotation" in result["error"]
    assert result["execution_policy"]["required_metric_keys"] == ["rotation", "reprojection"]


def test_explicit_entry_run_after_test(tmp_path):
    result = ExecutorAgent(install_dependencies=False, run_entry_point=True).run(
        "exec", inputs(), {"session_dir":str(tmp_path)})
    assert result["success"] and len(result["runs"]) == 2


def test_failed_test_does_not_run_entry(tmp_path):
    result = ExecutorAgent(install_dependencies=False, run_entry_point=True).run(
        "exec", inputs("raise RuntimeError('broken')"), {"session_dir":str(tmp_path)})
    assert result["success"] is False and len(result["runs"]) == 1
    assert "broken" in result["test_log"]


def test_install_failure_stops_execution(tmp_path, monkeypatch):
    import harness.agents.executor as module
    calls = []
    def fake(command, cwd, timeout):
        calls.append(command)
        return {"success":False,"stdout":"","stderr":"install failed","returncode":1}
    monkeypatch.setattr(module, "run_command", fake)
    data = inputs()
    data["dependencies"] = "numpy>=1"
    result = ExecutorAgent().run("exec", data, {"session_dir":str(tmp_path)})
    assert result["success"] is False
    assert len(calls) == 1
    assert "-m" in calls[0] and "venv" in calls[0]


def test_timeout_and_unicode_output(tmp_path):
    result = run_command([sys.executable,"-c","import time; time.sleep(10)"], tmp_path, 0.15)
    assert result["timed_out"] and not result["success"]
    result = run_command([sys.executable,"-c","print(chr(0x4e2d))"], tmp_path, 5)
    assert result["success"] and result["stdout"].strip() == chr(0x4e2d)


def test_generated_program_does_not_inherit_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    result = run_command([sys.executable,"-c",
        "import os; assert 'ANTHROPIC_API_KEY' not in os.environ"], tmp_path, 5)
    assert result["success"]


def test_generated_program_uses_bounded_numeric_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "64")
    result = run_command([sys.executable, "-c",
        "import os; assert os.environ['OPENBLAS_NUM_THREADS'] == '1'; "
        "assert os.environ['OMP_NUM_THREADS'] == '1'; "
        "assert os.environ['MKL_NUM_THREADS'] == '1'"], tmp_path, 5)
    assert result["success"]
