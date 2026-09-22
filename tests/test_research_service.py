import json
import shutil
import time
from pathlib import Path

import pytest
import yaml

from harness.core.checkpoint import CheckpointManager
from harness.core.io import atomic_json, file_lock
from harness.research_service import ResearchService, ROOT, parse_request
from harness.acceptance import sha256_file


@pytest.fixture
def service(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "workflows").mkdir()
    config = {
        "paths": {"sessions_dir": "sessions", "memory_dir": "memory", "workflows_dir": "workflows"},
        "workflow": {"default": "workflows/test.yaml"}, "logging": {"file": ""},
        "agents": {"executor": {"install_dependencies": False, "run_entry_point": True,
                                 "require_metrics": True, "required_metric_keys": ["loss"]}},
    }
    (tmp_path / "configs/default.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    workflow = {"name": "test", "stages": [
        {"id": "code_execution", "name": "Execute", "agent": "executor", "max_retries": 0,
         "inputs": {"files": [{"path": "train.py", "content": 'print(\'HARNESS_METRICS={"loss": 0.25}\')'}],
                    "dependencies": "", "test_snippet": "", "entry_point": "train.py"}},
    ]}
    (tmp_path / "workflows/test.yaml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    return ResearchService(tmp_path)


def checkpoint(service, status="failed"):
    cp = CheckpointManager(service.root / "sessions", "study")
    state = cp.new_state()
    state.update(status=status, workflow_path=str(service.root / "workflows/test.yaml"))
    state["stages"] = {"code_execution": {"status": "failed", "error": "missing loss", "output": {}}}
    cp.save(state)
    return cp


def test_service_request_accepts_utf8_bom_from_powershell():
    assert parse_request(b'\xef\xbb\xbf{"action":"list"}') == {"action": "list"}


def test_snapshot_preserves_failed_evidence_and_pending_metrics(service):
    checkpoint(service)
    view = service.handle({"action": "status", "session": "study"})
    assert view["status"] == "failed"
    assert view["evidence"]["missing_metrics"] == ["loss"]
    assert not view["evidence"]["execution_passed"]
    assert view["evidence"]["scientific_validity"] == "not_established"
    assert view["acceptance"]["decision"] == "not_evaluated"
    assert view["revision_round"] == 0
    assert view["draft_progress"] is None
    assert view["paper_draft_progress"] is None
    assert view["method_draft_progress"] is None


def test_snapshot_exposes_coding_draft_progress(service):
    cp = checkpoint(service, "running")
    state = cp.load()
    state["current_stage"] = "coding"
    cp.save(state)
    draft = cp.session_dir / ".drafts" / "coding_context.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"manifest": {"files": [{"path": "a.py"}, {"path": "b.py"}]},
                                 "files": [{"path": "a.py", "content": "x=1"}]}), encoding="utf-8")

    view = service.handle({"action": "status", "session": "study"})
    assert view["draft_progress"] == {"generated_files": 1, "total_files": 2, "test_ready": False}


def test_snapshot_exposes_paper_draft_progress(service):
    cp = checkpoint(service, "running")
    state = cp.load()
    state["current_stage"] = "paper_writing"
    cp.save(state)
    draft = cp.session_dir / ".drafts" / "paper_context.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"meta": {"title": "T", "abstract": "A"},
                                 "sections": {"introduction": "I", "related_work": "R"}}),
                     encoding="utf-8")

    view = service.handle({"action": "status", "session": "study"})
    assert view["paper_draft_progress"] == {
        "generated_sections": 2, "total_sections": 5, "metadata_ready": True,
    }


def test_snapshot_exposes_pending_method_audit(service):
    cp = checkpoint(service, "running")
    state = cp.load()
    state["current_stage"] = "method_design"
    cp.save(state)
    draft = cp.session_dir / ".drafts" / "method_context.json"
    draft.parent.mkdir(parents=True)
    draft.write_text(json.dumps({"candidate": {"method_name": "M"}}), encoding="utf-8")

    view = service.handle({"action": "status", "session": "study"})
    assert view["method_draft_progress"] == {"candidate_ready": True, "audit_pending": True}


def test_snapshot_exposes_current_acceptance_report_and_rejects_stale_one(service):
    cp = checkpoint(service)
    state = cp.load()
    report = {"decision": "rejected", "failed_required_checks": ["execution:succeeded"],
              "checkpoint_updated_at": state["_updated_at"],
              "checkpoint_sha256": sha256_file(cp.checkpoint_file)}
    (cp.session_dir / "acceptance.json").write_text(json.dumps(report), encoding="utf-8")
    view = service.handle({"action": "status", "session": "study"})
    assert view["acceptance"]["decision"] == "rejected"
    assert view["acceptance"]["failed_required_checks"] == ["execution:succeeded"]

    state["status"] = "running"
    cp.save(state)
    view = service.handle({"action": "status", "session": "study"})
    assert view["acceptance"]["decision"] == "stale"
    assert view["acceptance"]["stale"] is True


def test_stage_output_exposes_persisted_result_without_mutating_checkpoint(service):
    cp = checkpoint(service)
    state = cp.load()
    state["stages"]["code_execution"]["output"] = {
        "success": False,
        "analysis": {"metrics": {"loss": 0.25}},
        "error": "measured failure",
    }
    cp.save(state)
    before = cp.checkpoint_file.read_bytes()

    result = service.handle({"action": "stage-output", "session": "study", "stage": "code_execution"})

    assert result["has_output"] is True
    assert result["status"] == "failed"
    assert '"loss": 0.25' in result["text"]
    assert "measured failure" in result["text"]
    assert cp.checkpoint_file.read_bytes() == before
    with pytest.raises(ValueError, match="Unknown stage"):
        service.handle({"action": "stage-output", "session": "study", "stage": "unknown"})


def test_stale_running_checkpoint_is_interrupted_without_mutating_checkpoint(service):
    cp = checkpoint(service, "running")
    assert service.handle({"action": "status", "session": "study"})["status"] == "interrupted"
    assert cp.load()["status"] == "running"
    with file_lock(cp.session_dir / ".session.lock"):
        assert service.handle({"action": "status", "session": "study"})["status"] == "running"


def test_admission_is_not_completion_and_duplicate_launch_is_rejected(service, monkeypatch):
    monkeypatch.setattr(service, "spawn_worker", lambda *args: None)
    result = service.handle({"action": "run", "session": "study", "direction": "test"})
    assert result["accepted"] and result["job"]["status"] == "queued"
    assert service.snapshot("study", {})["job"]["status"] == "queued"
    with pytest.raises(ValueError, match="already active"):
        service.handle({"action": "run", "session": "study", "direction": "test"})


def test_resume_pins_admitted_configuration(service, monkeypatch):
    checkpoint(service)
    monkeypatch.setattr(service, "spawn_worker", lambda *args: None)
    path = service.root / "configs/strict.yaml"
    shutil.copyfile(service.root / "configs/default.yaml", path)
    atomic_json(service.job_path("study"), {"id": "old", "session": "study", "status": "failed", "config": "configs/strict.yaml"})
    result = service.handle({"action": "resume", "session": "study"})
    assert result["job"]["config"] == "configs/strict.yaml"


def test_revision_is_admitted_as_a_background_mutation(service, monkeypatch):
    checkpoint(service)
    monkeypatch.setattr(service, "spawn_worker", lambda *args: None)
    result = service.handle({"action": "revise", "session": "study"})
    assert result["accepted"] is True
    assert result["job"]["action"] == "revise"
    assert "revise" in result["job"]["argv"]


def test_crashed_worker_and_reset_preview(service):
    cp = checkpoint(service)
    atomic_json(service.job_path("study"), {"id": "old", "session": "study", "status": "running", "config": "configs/default.yaml", "created_at": 0})
    assert service.read_job("study")["status"] == "interrupted"
    before = cp.checkpoint_file.read_bytes()
    result = service.handle({"action": "reset-preview", "session": "study", "stages": ["code_execution"]})
    assert result["invalidated"] == ["code_execution"]
    assert cp.checkpoint_file.read_bytes() == before


def test_reset_preview_follows_transitive_workflow_dependencies(service):
    cp = checkpoint(service)
    path = service.root / "workflows/test.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec["stages"].extend([
        {"id": "review", "name": "Review", "agent": "reviewer", "depends_on": ["code_execution"]},
        {"id": "paper", "name": "Paper", "agent": "writer", "input_from": {"review": "review"}},
    ])
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    before = cp.checkpoint_file.read_bytes()
    view = service.handle({"action": "reset-preview", "session": "study", "stages": ["code_execution"]})
    assert view["invalidated"] == ["code_execution", "review", "paper"]
    assert cp.checkpoint_file.read_bytes() == before


def test_stale_artifacts_are_not_exposed_as_current(service):
    cp = checkpoint(service)
    (cp.session_dir / "output").mkdir()
    (cp.session_dir / "output/paper.tex").write_text("old evidence", encoding="utf-8")
    assert service.snapshot("study", {})["artifacts"] == []


@pytest.mark.parametrize("payload", [
    {"action": "run", "session": "../outside", "direction": "test"},
    {"action": "run", "direction": "test", "config": "../outside.yaml"},
    {"action": "run", "direction": "test", "workflow": "../outside.yaml"},
    {"action": "resume"},
])
def test_service_rejects_invalid_requests(service, payload):
    with pytest.raises(ValueError):
        service.handle(payload)


def test_real_detached_worker_survives_admission_and_exports_metrics(service):
    # A standalone copy makes ROOT and subprocess imports point at the fixture;
    # the worker exercises the real CLI/engine/executor with no model call.
    shutil.copytree(ROOT / "harness", service.root / "harness", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(ROOT / "main.py", service.root / "main.py")
    result = service.handle({"action": "run", "session": "study", "direction": "offline control plane test"})
    assert result["accepted"] is True
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = service.read_job("study")
        if job["status"] in ("completed", "failed", "interrupted"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed", service.handle({"action": "logs", "session": "study"})
    view = service.snapshot("study", {})
    assert view["status"] == "completed"
    assert view["metrics"] == {"loss": 0.25}
    assert view["evidence"]["execution_passed"]
    assert view["evidence"]["scientific_validity"] == "not_established"
