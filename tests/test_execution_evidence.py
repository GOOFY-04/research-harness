import copy
import hashlib
import shutil
import sys
from pathlib import Path

import pytest

from harness.agents.executor import ExecutorAgent
from harness.tools.execution_evidence import verify_execution_evidence
from harness.tools.process import run_command


def execute(root):
    agent = ExecutorAgent(install_dependencies=False, run_entry_point=True, require_metrics=True)
    result = agent.run("code_execution", {
        "files": [{"path": "main.py", "content":
                   'print(\'HARNESS_METRICS={"score": 0.75, "raw": [1, 2, 3]}\')\n'
                   'print("x" * 40000)\n'}],
        "entry_point": "main.py", "dependencies": "",
        "test_snippet": 'print(\'HARNESS_METRICS={"score": 99}\')',
    }, {"session_dir": str(root)})
    return agent, result


def test_complete_logs_survive_tail_truncation_and_session_copy(tmp_path):
    root = tmp_path / "original"
    agent, result = execute(root)
    assert "HARNESS_METRICS" not in result["runs"][-1]["stdout"]
    logs = result["runs"][-1]["logs"]
    raw = (root / logs["stdout"]["path"]).read_bytes()
    assert b'"raw": [1, 2, 3]' in raw and len(raw) > 40000
    assert hashlib.sha256(raw).hexdigest() == logs["stdout"]["sha256"]
    agent.validate_output(result)
    agent.validate_artifacts(result, root)
    errors, manifest = verify_execution_evidence(root, result)
    assert errors == [] and len(manifest) == 4
    copied = tmp_path / "copied"
    shutil.copytree(root, copied)
    assert verify_execution_evidence(copied, result)[0] == []
    (root / logs["stdout"]["path"]).unlink()
    agent.validate_artifacts(result, copied)


@pytest.mark.parametrize("mutation", ["missing", "tampered", "escape", "cached_metrics",
                                      "smoke_substitution", "role", "legacy", "stderr"])
def test_evidence_rejects_corruption(tmp_path, mutation):
    agent, result = execute(tmp_path)
    final = result["runs"][-1]
    path = tmp_path / final["logs"]["stdout"]["path"]
    if mutation == "missing":
        path.unlink()
    elif mutation == "tampered":
        path.write_bytes(path.read_bytes().replace(b"0.75", b"0.95"))
    elif mutation == "escape":
        final["logs"]["stdout"]["path"] = "../outside.log"
    elif mutation == "cached_metrics":
        final["emitted_metrics"]["score"] = 100
        result["analysis"]["metrics"]["score"] = 100
    elif mutation == "smoke_substitution":
        final["logs"] = copy.deepcopy(result["runs"][0]["logs"])
        final["emitted_metrics"] = {"score": 99}
        result["analysis"]["metrics"] = {"score": 99}
    elif mutation == "role":
        final["role"] = "smoke_test"
    elif mutation == "legacy":
        result.pop("evidence_version")
    else:
        (tmp_path / final["logs"]["stderr"]["path"]).write_text("changed")
    assert verify_execution_evidence(tmp_path, result)[0]
    with pytest.raises(ValueError, match="Execution evidence"):
        agent.validate_artifacts(result, tmp_path)


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_process_preserves_raw_output(tmp_path, timeout):
    source = "import sys, time; print('raw evidence', flush=True); "
    source += "time.sleep(20)" if timeout else "sys.exit(3)"
    result = run_command([sys.executable, "-c", source], tmp_path,
                         1 if timeout else 5, log_dir=tmp_path / "logs")
    assert not result["success"] and result["timed_out"] is timeout
    record = result["logs"]["stdout"]
    assert Path(record["path"]).read_text().strip() == "raw evidence"
    assert record["bytes"] > 0
