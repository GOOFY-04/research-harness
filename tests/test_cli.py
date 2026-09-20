from argparse import Namespace
from pathlib import Path
import json
import pytest
import yaml
import main
from harness.core.checkpoint import CheckpointManager
from harness.core.memory import MemoryStore


def config(tmp_path):
    return {"paths":{"sessions_dir":str(tmp_path/"sessions"),"memory_dir":str(tmp_path/"memory")},
            "workflow":{"default":str(tmp_path/"workflow.yaml")},"anthropic":{},"agents":{}}


def test_resume_preserves_workflow_argument(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cp = CheckpointManager(cfg["paths"]["sessions_dir"], "test")
    state = cp.new_state()
    cp.save(state)
    args = Namespace(session="test", workflow="custom.yaml")
    observed = {}
    def run(args, config):
        observed.update(vars(args))
        return 0
    monkeypatch.setattr(main, "cmd_run", run)
    assert main.cmd_resume(args, cfg) == 0
    assert observed["workflow"] == "custom.yaml"


def test_missing_resume_does_not_create_session(tmp_path):
    cfg = config(tmp_path)
    with pytest.raises(ValueError):
        main.cmd_resume(Namespace(session="absent"), cfg)
    assert not (tmp_path/"sessions").exists()


def test_config_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = main.load_config()
    assert Path(cfg["workflow"]["default"]).is_file()
    assert Path(cfg["paths"]["sessions_dir"]).is_absolute()
    agents = main.build_agent_registry(cfg, None)
    assert agents["method"].model == "agnes-3.0-flash"
    assert agents["method"]._llm.protocol == "openai_compatible"
    assert agents["method"].use_extended_thinking is False


def test_full_experiment_requires_machine_readable_metrics():
    cfg = main.load_config(main.ROOT / "configs" / "full_experiment.yaml")
    agents = main.build_agent_registry(cfg, None)
    assert agents["executor"].run_entry_point is True
    assert agents["executor"].require_metrics is True
    assert agents["executor"].timeout == 180
    assert Path(agents["executor"].python_executable).name.lower() == "python.exe"


def test_sfm_profile_enforces_dependencies_and_research_metrics():
    cfg = main.load_config(main.ROOT / "configs" / "sfm_full_experiment.yaml")
    agents = main.build_agent_registry(cfg, None)
    assert agents["coder"].allowed_dependencies == ["numpy"]
    assert agents["executor"].allowed_dependencies == ["numpy"]
    assert "baseline_rotation_error_deg" in agents["executor"].required_metric_keys
    assert "proposed_focal_length_relative_error" in agents["coder"].required_metric_keys


def test_skills_config_controls_registration_and_trigger(tmp_path):
    path = tmp_path/"skills.yaml"
    path.write_text(yaml.safe_dump({"skills":{
        "code_review":{"enabled":False},
        "test_generation":{"enabled":False},
        "dependency_check":{"enabled":True,"auto_trigger":True}}}))
    registry = main.setup_skills({"skills_file":str(path)})
    assert [s["name"] for s in registry.list_skills()] == ["dependency_check"]
    assert registry.auto_triggers == ["dependency_check"]


def test_memory_zero_and_safe_topic(tmp_path):
    memory = MemoryStore(tmp_path)
    memory.append("../elsewhere", {"value":1})
    assert memory.get_latest("../elsewhere", 0) == []
    assert memory.get_latest("../elsewhere", 1)[0]["content"] == {"value":1}
    assert not (tmp_path.parent/"elsewhere.json").exists()


def test_export_requirements_and_references(tmp_path):
    cp = CheckpointManager(tmp_path, "export")
    state = cp.new_state()
    outputs = {
        "coding":{"files":[{"path":"main.py","content":"print(1)"}],"dependencies":"numpy>=1"},
        "paper_writing":{"full_paper_latex":"draft","bibtex_entries":"@misc{x,title={X}}"},
        "documentation":{"readme":"# Project"}}
    for sid, output in outputs.items():
        cp.mark_stage_started(state, sid)
        cp.mark_stage_done(state, sid, output)
    main.export_artifacts(cp, state)
    assert (cp.session_dir/"code/requirements.txt").read_text() == "numpy>=1"
    assert (cp.session_dir/"output/references.bib").read_text().startswith("@misc")
    assert (cp.session_dir/"README.md").read_text() == "# Project"
