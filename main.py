#!/usr/bin/env python3
"""CLI for resumable research generation and validation."""
import argparse
import logging
import os
from pathlib import Path
from datetime import datetime
import shutil
import sys
import yaml

ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from harness.core import CheckpointManager, MemoryStore, WorkflowEngine
from harness.core.agent import BaseAgent
from harness.core.io import safe_path, validate_result, file_lock
from harness.core.skill import SkillRegistry
from harness.agents import PlannerAgent, LiteratureAgent, MethodAgent, CoderAgent, ReviewerAgent, WriterAgent
from harness.agents.executor import ExecutorAgent
from harness.agents.documenter import DocumenterAgent
from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill
from harness.tools import write_code_files
from harness.tools.validation import validate_files
from harness.acceptance import evaluate_session, write_report


def setup_logging(level="INFO", log_file="harness.log"):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=handlers)


def load_config(config_path=None):
    path = Path(config_path).resolve() if config_path else ROOT / "configs/default.yaml"
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping")
    if (not isinstance(config.get("paths"), dict) or not isinstance(config.get("workflow"), dict)
            or not all(isinstance(config["paths"].get(k), str)
                       for k in ("sessions_dir", "memory_dir", "workflows_dir"))
            or not isinstance(config["workflow"].get("default"), str)):
        raise ValueError("Configuration needs paths (sessions_dir, memory_dir, workflows_dir) and workflow.default")
    # Project paths are relative to project_root, itself relative to the config file.
    project = (path.parent / config.get("project_root", "..")).resolve()
    for key in ("sessions_dir", "memory_dir", "workflows_dir"):
        config["paths"][key] = str((project / config["paths"][key]).resolve())
    config["workflow"]["default"] = str((project / config["workflow"]["default"]).resolve())
    config["skills_file"] = str((project / config.get("skills_file", "configs/skills.yaml")).resolve())
    if config.get("logging", {}).get("file"):
        config["logging"]["file"] = str((project / config["logging"]["file"]).resolve())
    return config


def build_agent_registry(config, memory):
    provider = config.get("llm", config.get("anthropic", {}))
    settings = config.get("agents", {})
    classes = {"planner": PlannerAgent, "literature": LiteratureAgent, "method": MethodAgent,
               "coder": CoderAgent, "reviewer": ReviewerAgent, "writer": WriterAgent,
               "executor": ExecutorAgent, "documenter": DocumenterAgent}
    result = {}
    for name, cls in classes.items():
        options = dict(memory=memory, api_key=provider.get("api_key") or None,
                       base_url=provider.get("base_url"), request_timeout=provider.get("timeout", 120),
                       protocol=provider.get("protocol", "anthropic"),
                       api_key_env=provider.get("api_key_env"))
        common_model = provider.get("default_model")
        if common_model:
            options["model"] = common_model
        cfg = settings.get(name, {})
        for key in ("model", "max_tokens", "use_extended_thinking", "thinking_budget", "request_timeout"):
            if key in cfg:
                options[key] = cfg[key]
        if name in ("coder", "executor"):
            for key in ("allowed_dependencies", "required_metric_keys", "metric_constraints"):
                if key in cfg:
                    options[key] = cfg[key]
        if name == "executor":
            for key in ("timeout", "install_dependencies", "run_entry_point", "entry_args",
                        "enable_code_review", "require_metrics", "python_executable"):
                if key in cfg:
                    options[key] = cfg[key]
        result[name] = cls(**options)
    return result


def setup_skills(config=None):
    config = config or {}
    path = Path(config.get("skills_file", ROOT / "configs/skills.yaml"))
    settings = yaml.safe_load(path.read_text(encoding="utf-8")).get("skills", {}) if path.exists() else {}
    registry = SkillRegistry()
    provider = config.get("llm", config.get("anthropic", {}))
    for cls in (CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill):
        cfg = settings.get(cls.name, {})
        if not cfg.get("enabled", True):
            continue
        options = {} if cls is DependencyCheckSkill else {
            "api_key": provider.get("api_key"), "base_url": provider.get("base_url"),
            "timeout": provider.get("timeout", 120), "model": provider.get("default_model"),
            "protocol": provider.get("protocol", "anthropic"),
            "api_key_env": provider.get("api_key_env")}
        registry.register(cls(**options))
        if cfg.get("auto_trigger", False):
            registry.auto_triggers.append(cls.name)
    return registry


def workflow_for(args, config, state):
    return getattr(args, "workflow", None) or state.get("workflow_path") or config["workflow"]["default"]


def existing_session(args, config):
    cp = CheckpointManager(config["paths"]["sessions_dir"], args.session)
    if not cp.checkpoint_file.exists():
        raise ValueError(f"Session does not exist: {args.session}")
    return cp, cp.load()


def archive_artifacts(cp, stage_ids, include_checkpoint=False):
    targets = set()
    if "coding" in stage_ids:
        targets.add("code")
    if "paper_writing" in stage_ids:
        targets.add("output")
    if "documentation" in stage_ids:
        targets.add("README.md")
    if include_checkpoint:
        targets.add("checkpoint.json")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for relative in targets:
        source = safe_path(cp.session_dir, relative)
        destination = safe_path(cp.session_dir, f"history/{stamp}/{relative}")
        # Both resolved paths are verified inside the named session before moving.
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == "checkpoint.json":
                shutil.copy2(source, destination)
            else:
                source.rename(destination)


def export_artifacts(cp, state):
    for stage_id in ("coding", "paper_writing", "documentation"):
        if not cp.is_stage_done(state, stage_id):
            continue
        output = cp.get_stage_output(state, stage_id)
        validate_result(output)
        if stage_id == "coding":
            files = list(output.get("files", []))
            code_dir = safe_path(cp.session_dir, "code")
            validate_files(files, code_dir)
            # Store requirements even when it was separate from the generated manifest.
            files = [f for f in files if f["path"].replace("\\", "/").casefold() != "requirements.txt"]
            files.append({"path": "requirements.txt", "content": output.get("dependencies", "")})
            write_code_files(files, code_dir)
        elif stage_id == "paper_writing":
            directory = safe_path(cp.session_dir, "output")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "paper.tex").write_text(output["full_paper_latex"], encoding="utf-8")
            (directory / "references.bib").write_text(output.get("bibtex_entries", ""), encoding="utf-8")
        else:
            safe_path(cp.session_dir, "README.md").write_text(output["readme"], encoding="utf-8")


def cmd_run(args, config):
    cp = CheckpointManager(config["paths"]["sessions_dir"], args.session)
    previous = cp.load()
    resume = getattr(args, "resume", config["workflow"].get("resume", True)) and not args.no_resume
    direction = getattr(args, "direction", None)
    old_direction = previous.get("metadata", {}).get("research_direction")
    if resume and previous.get("stages") and direction and old_direction != direction:
        raise ValueError("Research direction changed; use a new session or --no-resume")
    agents = build_agent_registry(config, MemoryStore(config["paths"]["memory_dir"]))
    skills = setup_skills(config)
    # Share the configured registry with the optional executor review hook.
    agents["executor"].skill_registry = skills
    engine = WorkflowEngine(workflow_for(args, config, previous if resume else {}), cp, agents, skills)
    if not resume and cp.checkpoint_file.exists():
        archive_artifacts(cp, {"coding", "paper_writing", "documentation"}, include_checkpoint=True)
    elif cp.checkpoint_file.exists():
        archive_artifacts(cp, set(), include_checkpoint=True)
    override = {"planning": {"research_direction": direction}} if direction else None
    metadata = {"research_direction": direction} if direction else None
    final = engine.run(resume=resume, inputs_override=override, metadata=metadata)
    engine.status(final)
    changed = {sid for sid in ("coding", "paper_writing", "documentation")
               if previous.get("stages", {}).get(sid) != final.get("stages", {}).get(sid)}
    archive_artifacts(cp, changed)
    export_artifacts(cp, final)
    acceptance = evaluate_session(cp.session_dir, final, [stage.id for stage in engine.spec.stages])
    report_path = write_report(cp.session_dir, acceptance)
    print(f"Session: {cp.session_id}; status: {final['status']}; directory: {cp.session_dir}")
    print(f"Acceptance: {acceptance['decision']}; report: {report_path}")
    return 0 if final["status"] == "completed" else 1


def cmd_resume(args, config):
    existing_session(args, config)
    args.no_resume = False
    args.resume = True
    args.direction = None
    return cmd_run(args, config)


def cmd_reset_stage(args, config):
    cp, state = existing_session(args, config)
    engine = WorkflowEngine(workflow_for(args, config, state), cp, {})
    state["stage_dependencies"] = {s.id: s.depends_on for s in engine.spec.stages}
    unknown = set(args.stages) - set(state["stage_dependencies"])
    if unknown:
        raise ValueError(f"Unknown stages: {sorted(unknown)}")
    archive_artifacts(cp, set(), include_checkpoint=True)
    before = set(state["stages"])
    for stage in args.stages:
        cp.reset_stage(state, stage)
    invalidated = (before - set(state["stages"])) | set(args.stages)
    archive_artifacts(cp, invalidated)
    print(f"Reset stages and consumers: {', '.join(sorted(invalidated))}")
    return 0


def cmd_repair(args, config):
    cp, state = existing_session(args, config)
    archive_artifacts(cp, set(), include_checkpoint=True)
    # Lazy clients make validation/repair entirely offline and credential-free.
    agents = build_agent_registry(config, None)
    engine = WorkflowEngine(workflow_for(args, config, state), cp, agents)
    state["stage_dependencies"] = {s.id: s.depends_on for s in engine.spec.stages}
    repaired, remaining = [], []
    for stage in engine.spec.stages:
        data = state["stages"].get(stage.id, {})
        output = data.get("output")
        if not isinstance(output, dict) or not output.get("parse_error"):
            continue
        fixed = BaseAgent._parse_json(output.get("raw", ""))
        try:
            engine._validate_output(stage, fixed)
        except (ValueError, TypeError, KeyError, SyntaxError):
            remaining.append(stage.id)
            continue
        before = set(state["stages"])
        cp.reset_stage(state, stage.id)
        archive_artifacts(cp, before - set(state["stages"]))
        cp.mark_stage_started(state, stage.id)
        cp.mark_stage_done(state, stage.id, fixed)
        repaired.append(stage.id)
    export_artifacts(cp, state)
    print(f"Repaired: {repaired}; still invalid: {remaining}")
    return 1 if remaining else 0


def cmd_status(args, config):
    cp, state = existing_session(args, config)
    engine = WorkflowEngine(workflow_for(args, config, state), cp, {})
    engine.status(state)
    print(f"Overall status: {state.get('status', 'legacy')}")
    return 0


def cmd_list(args, config):
    cp = CheckpointManager(config["paths"]["sessions_dir"])
    for session in cp.list_sessions():
        print(f"{session['session_id']}  {session['workflow']}  {session['current_stage']}  {session['updated_at']}")
    return 0


def cmd_accept(args, config):
    cp, state = existing_session(args, config)
    engine = WorkflowEngine(workflow_for(args, config, state), cp, {})
    report = evaluate_session(cp.session_dir, state, [stage.id for stage in engine.spec.stages])
    if args.write_report:
        path = write_report(cp.session_dir, report)
        print(f"Acceptance report: {path}")
    print(f"Acceptance decision: {report['decision']}")
    for check in report["checks"]:
        icon = "PASS" if check["passed"] else ("INFO" if not check["required"] else "FAIL")
        print(f"[{icon}] {check['id']}: {check['detail']}")
    return 0 if report["required_checks_passed"] else 1


def main(argv=None):
    # Windows may inherit a legacy GBK console even when checkpoint text is
    # UTF-8. Reconfigure the CLI streams so status symbols and Chinese stage
    # names cannot turn a successful command into an encoding exception.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Resumable research generation and validation")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--direction", required=True)
    run.add_argument("--session")
    run.add_argument("--workflow")
    run.add_argument("--no-resume", action="store_true")
    for command in ("resume", "status", "repair", "reset-stage", "accept"):
        child = sub.add_parser(command)
        child.add_argument("--session", required=True)
        child.add_argument("--workflow")
        if command == "reset-stage":
            child.add_argument("stages", nargs="+")
        if command == "accept":
            child.add_argument("--write-report", action="store_true")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        setup_logging(**{"level": config.get("logging", {}).get("level", "INFO"),
                         "log_file": config.get("logging", {}).get("file", "")})
        action = {"run": cmd_run, "resume": cmd_resume, "status": cmd_status,
                  "repair": cmd_repair, "reset-stage": cmd_reset_stage,
                  "accept": cmd_accept, "list": cmd_list}[args.command]
        if args.command in ("run", "resume", "repair", "reset-stage"):
            if args.command != "run":
                existing_session(args, config)
            cp = CheckpointManager(config["paths"]["sessions_dir"], args.session)
            args.session = cp.session_id
            with file_lock(cp.session_dir / ".session.lock"):
                return action(args, config)
        return action(args, config)
    except (ValueError, OSError, RuntimeError, yaml.YAMLError) as exc:
        logging.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logging.warning("Interrupted; resume will retry the unfinished stage.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
