"""Versioned local control plane for OpenCode. No workflow state is duplicated here.

Requests arrive as JSON on stdin. Detached workers own long CLI runs; checkpoint
snapshots remain the authority for stages, metrics and artifact availability.
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from uuid import uuid4

from harness.core.checkpoint import CheckpointManager
from harness.core.io import atomic_json, file_lock, safe_path
from harness.core.workflow import WorkflowEngine
from harness.acceptance import sha256_file, ACCEPTANCE_POLICY_VERSION

ROOT = Path(__file__).resolve().parents[1]
VERSION = 1
MUTATIONS = {"run", "resume", "repair", "revise", "reset-stage"}


def compact_message(value, limit=600):
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...<see logs>"


def parse_request(raw: bytes):
    """Decode JSON from shells that may prefix UTF-8 input with a BOM."""
    request = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(request, dict):
        raise ValueError("Request must be a JSON object")
    return request


class ResearchService:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.jobs = self.root / ".research" / "jobs"

    def job_path(self, session):
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", session):
            raise ValueError("session must be a filesystem-safe identifier")
        return safe_path(self.jobs, session + ".json")

    def read_job(self, session):
        path = self.job_path(session)
        if not path.exists():
            return None
        job = json.loads(path.read_text(encoding="utf-8"))
        if job["status"] not in ("queued", "running"):
            return job
        # OS leases outlive the requesting chat turn but not a crashed worker.
        try:
            with file_lock(path.with_suffix(".lock")):
                pass
        except RuntimeError:
            return {**job, "status": "running"}
        if job["status"] == "queued" and time.time() - job["created_at"] < 30:
            return job
        return {**job, "status": "interrupted"}

    def config(self, request, job=None):
        from main import load_config
        name = request.get("config") or (job or {}).get("config") or "configs/default.yaml"
        path = safe_path(self.root, name)
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            raise ValueError("config must be an existing YAML file inside the harness root")
        return name, load_config(path)

    def snapshot(self, session, request):
        job = self.read_job(session)
        _, config = self.config(request, job)
        cp = CheckpointManager(config["paths"]["sessions_dir"], session)
        if not cp.checkpoint_file.exists() and not job:
            raise ValueError(f"Session does not exist: {session}")
        state = cp.load()
        engine = WorkflowEngine(state.get("workflow_path") or config["workflow"]["default"], cp, {})
        stages = []
        for stage in engine.spec.stages:
            info = state["stages"].get(stage.id, {})
            errors = info.get("errors") if isinstance(info.get("errors"), list) else []
            stages.append({"id": stage.id, "name": stage.name, "status": info.get("status", "pending"),
                           "depends_on": stage.depends_on, "attempts": info.get("attempts", 0),
                           "error": compact_message(info.get("error")),
                           "last_error": compact_message(errors[-1]) if errors else None,
                           "started_at": info.get("started_at"), "repair_from": stage.repair_from})
        execution = state["stages"].get("code_execution", {})
        output = execution.get("output") or {}
        review_output = state["stages"].get("self_review", {}).get("output") or {}
        review = review_output.get("recommendation")
        metrics = {key: value for key, value in output.get("analysis", {}).get("metrics", {}).items()
                   if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
        required = config.get("agents", {}).get("executor", {}).get("required_metric_keys", [])
        artifacts = []
        paired = []
        paired_evidence = output.get("experiment_evidence") or {}
        definitions = {item["id"]: item for item in paired_evidence.get("contract", {}).get("comparisons", [])}
        if execution.get("status") == "done":
            for item in paired_evidence.get("comparisons", []):
                definition = definitions.get(item["id"], {})
                paired.append({"id": item["id"], "metric_name": definition.get("metric_name", item["id"]),
                               "unit": definition.get("unit", ""), "direction": definition.get("direction", ""),
                               "pair_count": item["pair_count"], "mean_difference": item["paired_mean_difference"],
                               "standard_error": item["paired_standard_error"],
                               "evaluation_scope": definition.get("evaluation_scope", "")})
                path = safe_path(cp.session_dir, item["artifact"]["path"])
                if path.is_file():
                    artifacts.append({"stage": "code_execution", "path": str(path)})
        for stage, relative in [("coding", "code"), ("paper_writing", "output/paper.tex"),
                                ("paper_writing", "output/references.bib"), ("documentation", "README.md")]:
            path = safe_path(cp.session_dir, relative)
            if cp.is_stage_done(state, stage) and path.exists():
                artifacts.append({"stage": stage, "path": str(path)})
        draft_progress = None
        paper_draft_progress = None
        method_draft_progress = None
        draft_dir = safe_path(cp.session_dir, ".drafts")
        if state.get("current_stage") == "method_design" and draft_dir.is_dir():
            try:
                drafts = sorted(draft_dir.glob("method_*.json"),
                                key=lambda item: item.stat().st_mtime, reverse=True)
                if drafts:
                    draft = json.loads(drafts[0].read_text(encoding="utf-8"))
                    method_draft_progress = {
                        "candidate_ready": isinstance(draft.get("candidate"), dict),
                        "audit_pending": True,
                    }
                    audit_state = draft.get("audit_state", {})
                    if isinstance(audit_state, dict) and audit_state.get("initial_review"):
                        method_draft_progress["verification_pending"] = "verification" not in audit_state
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if state.get("current_stage") == "coding" and draft_dir.is_dir():
            try:
                drafts = sorted(draft_dir.glob("coding_*.json"),
                                key=lambda item: item.stat().st_mtime, reverse=True)
                if drafts:
                    draft = json.loads(drafts[0].read_text(encoding="utf-8"))
                    generated = len(draft.get("files", []))
                    total = len(draft.get("manifest", {}).get("files", []))
                    if total > 0 and 0 <= generated <= total:
                        draft_progress = {"generated_files": generated, "total_files": total,
                                          "test_ready": isinstance(draft.get("test_snippet"), str)}
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if state.get("current_stage") == "paper_writing" and draft_dir.is_dir():
            try:
                drafts = sorted(draft_dir.glob("paper_*.json"),
                                key=lambda item: item.stat().st_mtime, reverse=True)
                if drafts:
                    draft = json.loads(drafts[0].read_text(encoding="utf-8"))
                    sections = draft.get("sections", {})
                    if isinstance(sections, dict) and 0 <= len(sections) <= 5:
                        paper_draft_progress = {
                            "generated_sections": len(sections), "total_sections": 5,
                            "metadata_ready": isinstance(draft.get("meta"), dict),
                        }
            except (OSError, ValueError, TypeError, KeyError):
                pass
        active = job and job["status"] in ("running", "queued")
        job_view = dict(job) if job else None
        if job_view and "error" in job_view:
            job_view["error"] = compact_message(job_view["error"])
        status = state.get("status", "pending")
        if status == "running" and not active:
            # A CLI outside this service may also own the session.
            try:
                with file_lock(cp.session_dir / ".session.lock"):
                    status = "interrupted"
            except RuntimeError:
                pass
        acceptance = {"decision": "not_evaluated", "failed_required_checks": [],
                      "report": None, "stale": False}
        acceptance_path = safe_path(cp.session_dir, "acceptance.json")
        if acceptance_path.is_file():
            try:
                saved = json.loads(acceptance_path.read_text(encoding="utf-8"))
                requirements_path = safe_path(cp.session_dir, "requirements_audit.json")
                requirements_hash = sha256_file(requirements_path) if requirements_path.is_file() else None
                stale = (saved.get("policy_version") != ACCEPTANCE_POLICY_VERSION
                         or saved.get("requirements_audit_sha256") != requirements_hash
                         or saved.get("checkpoint_updated_at") != state.get("_updated_at")
                         or saved.get("checkpoint_sha256") != sha256_file(cp.checkpoint_file))
                acceptance = {
                    "decision": "stale" if stale else saved.get("decision", "not_evaluated"),
                    "failed_required_checks": saved.get("failed_required_checks", []) if not stale else [],
                    "report": str(acceptance_path),
                    "stale": stale,
                }
            except (OSError, ValueError, TypeError):
                acceptance = {"decision": "invalid", "failed_required_checks": [],
                              "report": str(acceptance_path), "stale": False}
        return {"session": session, "direction": state.get("metadata", {}).get("research_direction", ""),
                "status": status, "current_stage": state.get("current_stage"), "stages": stages,
                "job": job_view, "metrics": metrics, "artifacts": artifacts, "review": review,
                "evidence_verdict": review_output.get("evidence_verdict"),
                "evidence": {"execution_kind": output.get("execution_kind", "not_run"),
                             "execution_passed": execution.get("status") == "done" and output.get("success") is True,
                             "missing_metrics": sorted(set(required) - set(metrics)),
                             "paired_comparisons": paired,
                             "scientific_validity": "not_established"},
                "acceptance": acceptance,
                "draft_progress": draft_progress,
                "paper_draft_progress": paper_draft_progress,
                "method_draft_progress": method_draft_progress,
                "revision_round": int(state.get("metadata", {}).get("revision_round", 0)),
                "repairs": len((state["stages"].get("coding", {}).get("output") or {}).get("repair_history", [])),
                "checkpoint": str(cp.checkpoint_file)}

    def handle(self, request):
        action = request.get("action")
        session = request.get("session")
        if action not in MUTATIONS | {"status", "list", "logs", "stage-output", "reset-preview"}:
            raise ValueError("Unknown research action")
        if action == "list":
            _, config = self.config(request)
            ids = {row["session_id"] for row in CheckpointManager(config["paths"]["sessions_dir"]).list_sessions()}
            # Include admitted jobs before their first checkpoint is written.
            ids.update(path.stem for path in self.jobs.glob("*.json"))
            sessions, errors = [], []
            for sid in sorted(ids):
                try:
                    sessions.append(self.snapshot(sid, request))
                except (OSError, ValueError, KeyError) as exc:
                    errors.append({"session": sid, "error": str(exc)})
            return {"sessions": sessions, "errors": errors}
        if action == "run" and not session:
            session = "research_" + uuid4().hex[:12]
        if not session:
            raise ValueError(f"session is required for {action}")
        self.job_path(session)
        if action == "status":
            return self.snapshot(session, request)
        if action == "logs":
            job = self.read_job(session)
            if not job:
                return {"session": session, "text": "No service job log for this session."}
            path = safe_path(self.jobs, job["id"] + ".log")
            if not path.exists():
                return {"session": session, "text": ""}
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 16000))
                text = stream.read(16000).decode("utf-8", errors="replace")
            return {"session": session, "text": re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)}
        if action == "stage-output":
            _, config = self.config(request, self.read_job(session))
            cp = CheckpointManager(config["paths"]["sessions_dir"], session)
            if not cp.checkpoint_file.exists():
                raise ValueError(f"Session does not exist: {session}")
            stage = request.get("stage")
            if not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", stage):
                raise ValueError("stage is required for stage-output")
            state = cp.load()
            if stage not in state.get("stages", {}):
                raise ValueError(f"Unknown stage: {stage}")
            info = state["stages"][stage]
            output = info.get("output")
            if output is None:
                text = "No persisted output yet. Running stages write output after the stage completes or fails."
            else:
                text = json.dumps(output, ensure_ascii=False, indent=2, default=str)
                text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
                if len(text) > 32000:
                    text = text[:32000] + "\n... [output truncated]"
            return {"session": session, "stage": stage, "status": info.get("status", "pending"),
                    "error": info.get("error"), "text": text, "has_output": output is not None}
        job = self.read_job(session)
        name, config = self.config(request, job)
        if job and action != "run" and request.get("config") and name != job["config"]:
            raise ValueError("Resume/reset must use the session's admitted config")
        cp = CheckpointManager(config["paths"]["sessions_dir"], session)
        args = ["--config", str(safe_path(self.root, name)), action, "--session", session]
        if action == "run":
            direction = request.get("direction")
            if not isinstance(direction, str) or not direction.strip():
                raise ValueError("direction is required for run")
            if cp.checkpoint_file.exists():
                raise ValueError("Session already exists; use resume or a new session identifier")
            args += ["--direction", direction.strip()]
        elif not cp.checkpoint_file.exists():
            raise ValueError(f"Session does not exist: {session}")
        if request.get("workflow"):
            path = safe_path(self.root, request["workflow"])
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                raise ValueError("workflow must be an existing YAML file inside the harness root")
            args += ["--workflow", str(path)]
        if action in ("reset-stage", "reset-preview"):
            stages = request.get("stages")
            if not isinstance(stages, list) or not stages or not all(isinstance(s, str) for s in stages):
                raise ValueError("stages are required for reset")
            state = cp.load()
            engine = WorkflowEngine(request.get("workflow") and str(safe_path(self.root, request["workflow"]))
                                    or state.get("workflow_path") or config["workflow"]["default"], cp, {})
            graph = {s.id: s.depends_on for s in engine.spec.stages}
            affected = set(stages)
            if affected - graph.keys():
                raise ValueError("Unknown reset stage")
            while True:
                expanded = affected | {s for s, deps in graph.items() if affected.intersection(deps)}
                if expanded == affected:
                    break
                affected = expanded
            if action == "reset-preview":
                return {"session": session, "invalidated": [s for s in graph if s in affected]}
            args += stages
        with file_lock(self.jobs / ".admission.lock"):
            previous = self.read_job(session)
            if previous and previous["status"] in ("queued", "running"):
                raise ValueError("A research operation is already active for this session")
            with file_lock(self.job_path(session).with_suffix(".lock")):
                pass
            with file_lock(cp.session_dir / ".session.lock"):
                pass
            job = {"id": uuid4().hex, "session": session, "config": name, "action": action,
                   "argv": args, "status": "queued", "created_at": time.time()}
            atomic_json(self.job_path(session), job)
            try:
                self.spawn_worker(session, job["id"])
            except OSError:
                atomic_json(self.job_path(session), {**job, "status": "failed", "error": "Worker launch failed"})
                raise
        return {"session": session, "job": job, "accepted": True,
                "message": "Background operation admitted. Inspect status and logs; admission is not completion."}

    def spawn_worker(self, session, job_id):
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, "-m", "harness.research_service", "worker", session, job_id],
                         cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **options)

    def worker(self, session, job_id):
        from main import main
        path = self.job_path(session)
        # Status readers probe this lease briefly. Do not lose an admitted job
        # merely because a reader happened to hold it during worker startup.
        with file_lock(path.with_suffix(".lock"), timeout=5):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["id"] != job_id or job["status"] != "queued":
                return
            atomic_json(path, {**job, "status": "running", "pid": os.getpid()})
            with safe_path(self.jobs, job["id"] + ".log").open("w", encoding="utf-8", buffering=1) as log:
                sys.stdout = sys.stderr = log
                try:
                    code = main(job["argv"])
                except BaseException:
                    traceback.print_exc()
                    code = 1
                atomic_json(path, {**job, "status": "completed" if code == 0 else "failed",
                                   "exit_code": code, "finished_at": time.time()})


def cli():
    if len(sys.argv) == 4 and sys.argv[1] == "worker":
        ResearchService().worker(sys.argv[2], sys.argv[3])
        return 0
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        request = parse_request(sys.stdin.buffer.read())
        response = {"version": VERSION, "ok": True, "data": ResearchService().handle(request)}
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        response = {"version": VERSION, "ok": False, "error": str(exc)}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(cli())
