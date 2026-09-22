"""Offline counterfactual checks for recovery and evidence gates.

Run from the repository root:
    python -m experiments.harness_ablation --output experiments/results/harness_ablation.json
or:
    python experiments/harness_ablation.py --output experiments/results/harness_ablation.json

The benchmark uses deterministic fake agents.  It tests harness mechanics; it
does not measure LLM research quality or establish novelty against every other
project.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import sys

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.acceptance import evaluate_session
from harness.core.checkpoint import CheckpointManager
from harness.core.workflow import WorkflowEngine


STAGES = ["planning", "literature", "method_design", "coding", "code_execution",
          "self_review", "paper_writing", "documentation"]


class CountingAgent:
    def __init__(self, counter: dict[str, int], name: str, *, fail=False):
        self.counter = counter
        self.name = name
        self.fail = fail

    def run(self, stage_id, inputs, state):
        self.counter[self.name] = self.counter.get(self.name, 0) + 1
        if self.fail:
            raise RuntimeError("injected interruption")
        return {"value": stage_id}


def recovery_ablation(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    workflow = root / "recovery.yaml"
    workflow.write_text(yaml.safe_dump({
        "name": "recovery_ablation",
        "stages": [
            {"id": "collect", "agent": "collect", "max_retries": 0},
            {"id": "analyze", "agent": "analyze", "depends_on": ["collect"], "max_retries": 0},
        ],
    }), encoding="utf-8")

    initial_calls: dict[str, int] = {}
    cp = CheckpointManager(root / "sessions", "resume")
    failed = WorkflowEngine(workflow, cp, {
        "collect": CountingAgent(initial_calls, "collect"),
        "analyze": CountingAgent(initial_calls, "analyze", fail=True),
    }).run()

    resume_calls: dict[str, int] = {}
    resumed = WorkflowEngine(workflow, cp, {
        "collect": CountingAgent(resume_calls, "collect"),
        "analyze": CountingAgent(resume_calls, "analyze"),
    }).run()

    restart_calls: dict[str, int] = {}
    restarted = WorkflowEngine(workflow, CheckpointManager(root / "sessions", "restart"), {
        "collect": CountingAgent(restart_calls, "collect"),
        "analyze": CountingAgent(restart_calls, "analyze"),
    }).run(resume=False)
    resume_work = sum(resume_calls.values())
    restart_work = sum(restart_calls.values())
    return {
        "same_failed_prefix": failed["status"] == "failed" and initial_calls == {"collect": 1, "analyze": 1},
        "resume_completed": resumed["status"] == "completed",
        "restart_completed": restarted["status"] == "completed",
        "additional_stage_calls": {"checkpoint_resume": resume_work, "stateless_restart": restart_work},
        "completed_stage_replayed": {"checkpoint_resume": resume_calls.get("collect", 0),
                                     "stateless_restart": restart_calls.get("collect", 0)},
        "work_reduction_fraction": (restart_work - resume_work) / restart_work,
    }


def _evidence_fixture(root: Path) -> dict:
    outputs = {
        "planning": {"novelty_hypothesis": "A falsifiable difference"},
        "literature": {"sources": [{"title": "Source", "arxiv_id": "2401.00001"}]},
        "method_design": {},
        "coding": {"files": [{"path": "main.py", "content": "print('ok')\n"}], "dependencies": ""},
        "code_execution": {"success": True, "execution_kind": "entry_point",
                           "execution_policy": {"required_metric_keys": ["score"],
                                                "metric_constraints": {"score": {"min": 0.5}}},
                           "analysis": {"metrics": {"score": 0.75}}},
        "self_review": {"recommendation": "weak_accept", "evidence_verdict": "supported",
                        "claim_scope": "one deterministic synthetic task", "weaknesses": []},
        "paper_writing": {"full_paper_latex": "paper\n", "bibtex_entries": "refs\n",
                          "verified_metrics": {"score": 0.75}, "evidence_scope": "entry_point"},
        "documentation": {"readme": "# Result\n"},
    }
    state = {"status": "completed", "completed_stages": list(STAGES),
             "metadata": {"research_direction": "Does it work?"},
             "stages": {stage: {"status": "done", "output": outputs[stage]} for stage in STAGES}}
    (root / "code").mkdir(parents=True)
    (root / "output").mkdir()
    (root / "code/main.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "code/requirements.txt").write_text("", encoding="utf-8")
    (root / "output/paper.tex").write_text("paper\n", encoding="utf-8")
    (root / "output/references.bib").write_text("refs\n", encoding="utf-8")
    (root / "README.md").write_text("# Result\n", encoding="utf-8")
    return state


def evidence_ablation(root: Path) -> dict:
    state = _evidence_fixture(root)
    clean = evaluate_session(root, state, STAGES)
    (root / "code/main.py").write_text("print('tampered')\n", encoding="utf-8")
    tampered = evaluate_session(root, state, STAGES)
    (root / "code/main.py").write_text("print('ok')\n", encoding="utf-8")
    state["stages"]["self_review"]["output"] = {
        "recommendation": "weak_reject",
        "evidence_verdict": "invalid", "claim_scope": "invalid baseline",
        "weaknesses": [{"severity": "major", "category": "validity", "issue": "invalid baseline"}],
    }
    weak_review = evaluate_session(root, state, STAGES)
    return {
        "clean_package_accepted": clean["required_checks_passed"],
        "naive_completed_gate_accepts_tampered_package": state["status"] == "completed",
        "strict_gate_accepts_tampered_package": tampered["required_checks_passed"],
        "tamper_checks": [item for item in tampered["failed_required_checks"] if item.startswith("artifact:")],
        "naive_completed_gate_accepts_weak_review": state["status"] == "completed",
        "strict_gate_accepts_weak_review": weak_review["required_checks_passed"],
        "review_checks": [item for item in weak_review["failed_required_checks"] if item.startswith("review:")],
    }


def run_benchmark() -> dict:
    with tempfile.TemporaryDirectory(prefix="research_harness_ablation_") as directory:
        root = Path(directory)
        recovery = recovery_ablation(root / "recovery")
        evidence = evidence_ablation(root / "evidence")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "deterministic harness mechanism ablation; not a research-quality benchmark",
        "recovery_ablation": recovery,
        "evidence_gate_ablation": evidence,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_benchmark()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
