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
    assert agents["method"]._llm.stream_responses is True
    assert agents["method"].use_extended_thinking is False
    assert agents["planner"].max_tokens == 3072
    assert agents["literature"].max_tokens == 8192
    assert agents["method"].max_tokens == 6144
    assert agents["reviewer"].max_tokens == 4096


def test_full_experiment_requires_machine_readable_metrics():
    cfg = main.load_config(main.ROOT / "configs" / "full_experiment.yaml")
    agents = main.build_agent_registry(cfg, None)
    assert agents["coder"].require_experiment_contract
    assert agents["executor"].require_experiment_contract
    assert agents["executor"].run_entry_point is True
    assert agents["executor"].require_metrics is True
    assert agents["executor"].timeout == 180
    assert agents["executor"].required_metric_keys == [
        "proposed_primary", "baseline_primary", "improvement_delta", "sample_count",
    ]
    assert agents["executor"].metric_constraints == {"sample_count": {"min": 30}}
    assert agents["coder"].required_metric_keys == agents["executor"].required_metric_keys
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


def test_archiving_invalidated_coding_removes_resumable_drafts(tmp_path):
    cp = CheckpointManager(tmp_path, "draft-archive")
    draft = cp.session_dir / ".drafts" / "coding_context.json"
    draft.parent.mkdir(parents=True)
    draft.write_text("{}", encoding="utf-8")

    main.archive_artifacts(cp, {"coding"}, include_drafts=True)

    assert not draft.exists()
    assert list((cp.session_dir / "history").glob("*/.drafts/coding_context.json"))


def test_archiving_invalidated_paper_removes_resumable_drafts(tmp_path):
    cp = CheckpointManager(tmp_path, "paper-draft-archive")
    draft = cp.session_dir / ".drafts" / "paper_context.json"
    draft.parent.mkdir(parents=True)
    draft.write_text("{}", encoding="utf-8")

    main.archive_artifacts(cp, {"paper_writing"}, include_drafts=True)

    assert not draft.exists()
    assert list((cp.session_dir / "history").glob("*/.drafts/paper_context.json"))


def test_revise_archives_review_and_injects_feedback_before_resume(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg["workflow"]["default"] = str(main.ROOT / "workflows" / "research.yaml")
    cp = CheckpointManager(cfg["paths"]["sessions_dir"], "revision")
    state = cp.new_state()
    state.update(status="completed", workflow_path=cfg["workflow"]["default"], metadata={})
    state["stages"] = {
        "method_design": {"status": "done", "output": {"method_name": "Old"}},
        "coding": {"status": "done", "output": {"files": []}},
        "code_execution": {"status": "done", "output": {
            "execution_kind": "entry_point", "analysis": {"metrics": {"score": 0.1}},
            "execution_policy": {"required_metric_keys": ["score"]},
        }},
        "self_review": {"status": "done", "output": {
            "recommendation": "weak_reject",
            "weaknesses": [{"severity": "major", "issue": "invalid baseline"}],
            "revision_plan": [{"priority": "high", "action": "fix baseline"}],
            "missing_experiments": ["ablation"], "missing_baselines": ["strong baseline"],
        }},
        "paper_writing": {"status": "done", "output": {}},
        "documentation": {"status": "done", "output": {}},
    }
    state["completed_stages"] = list(state["stages"])
    cp.save(state)
    observed = {}

    def resume(args, config):
        current = cp.load()
        observed.update(current)
        assert args.resume is True and args.no_resume is False and args.direction is None
        return 7

    monkeypatch.setattr(main, "cmd_run", resume)
    result = main.cmd_revise(Namespace(session="revision", workflow=None), cfg)

    assert result == 7
    assert "method_design" not in observed["stages"]
    feedback = observed["stage_inputs_override"]["method_design"]
    assert feedback["review_feedback"]["recommendation"] == "weak_reject"
    assert feedback["previous_method"] == {"method_name": "Old"}
    assert feedback["previous_execution"]["analysis"]["metrics"] == {"score": 0.1}
    assert observed["metadata"]["revision_round"] == 1
    assert observed["metadata"]["revision_history"][0]["major_issues"] == ["invalid baseline"]
