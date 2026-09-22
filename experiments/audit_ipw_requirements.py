"""Case-specific, human-authored diagnostic of the IPW study's original constraints.

This does not repair the experiment or constitute generic autonomous validation.
Run only on trusted generated code; the subprocess is not a security sandbox.
"""
import argparse
import json
import math
import sys
from pathlib import Path

from harness.core.io import atomic_json, file_lock, safe_path
from harness.tools.execution_evidence import verify_execution_evidence
from harness.tools.process import run_command
from harness.tools.requirements_audit import audit_context_digest, verify_requirements_audit


PROBABILITY_QUOTE = "p(x)=0.05+0.9/(1+exp(-a*x))"
AGGREGATION_QUOTE = "每个种子组内对三个场景的平方误差做等权平均，主指标为这50个种子组的平均MSE"


def audit(session_dir):
    root = Path(session_dir).resolve()
    if not (root / "checkpoint.json").is_file():
        raise ValueError("Session checkpoint is missing")
    with file_lock(root / ".session.lock"):
        return _audit(root)


def _audit(root):
    state = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
    direction = state["metadata"]["research_direction"]
    if any(quote not in direction for quote in (PROBABILITY_QUOTE, AGGREGATION_QUOTE)):
        raise ValueError("This diagnostic only supports the specified IPW question")
    execution = state["stages"]["code_execution"]["output"]
    errors, _ = verify_execution_evidence(root, execution)
    if errors or not execution.get("success"):
        raise ValueError(f"Original execution evidence is invalid: {errors}")
    code_dir = Path(execution["code_dir"]).resolve()
    if not code_dir.is_relative_to(root):
        raise ValueError("Executed source directory must be inside this session")
    for item in state["stages"]["coding"]["output"]["files"]:
        if safe_path(code_dir, item["path"]).read_text(encoding="utf-8") != item["content"]:
            raise ValueError(f"Executed source differs from checkpoint: {item['path']}")
    metrics = execution["analysis"]["metrics"]
    # Register before running so interruption or accidental removal cannot turn
    # this already-requested audit into an optional skipped acceptance check.
    state["metadata"]["requirements_audit_required"] = True
    atomic_json(root / "checkpoint.json", state)
    # The aggregation check recomputes the equal-scenario mean of recorded MSEs.
    # It does not claim to independently regenerate all underlying seed samples.
    probe = ("import json\nfrom synthetic_data_harness import generate_p\n"
             + "m = " + repr(metrics) + "\n"
             + "result = {'p_x1_a2': generate_p(1.0, 2.0), 'p_x0_a0': generate_p(0.0, 0.0)}\n"
             + "for method, prefix in [('proposed', 'cht'), ('baseline', 'ht')]:\n"
             + "    expected = sum(m[f'scenario_mse_{prefix}_{a}'] for a in (0, 2, 5)) / 3\n"
             + "    result[method + '_aggregation_error'] = m[method + '_primary'] - expected\n"
             + "print('HARNESS_METRICS=' + json.dumps(result))\n")
    run = run_command([sys.executable, "-c", probe], code_dir, 30,
                      log_dir=root / "requirements_probe_logs")
    for log in run["logs"].values():
        log["path"] = Path(log["path"]).relative_to(root).as_posix()
    run["role"] = "entry_point"
    record = {
        "version": 1, "context_sha256": audit_context_digest(state),
        "scope": "Post-run human-authored diagnostic against pre-existing explicit constraints; not an independent full statistical replication",
        "execution": {"success": run["success"], "execution_kind": "entry_point", "evidence_version": 1,
                      "runs": [run], "analysis": {"metrics": run["emitted_metrics"]}},
        "checks": [
            {"id": "probability-sign", "metric": "p_x1_a2", "expected": 0.05 + 0.9 / (1 + math.exp(-2)), "requirement_quote": PROBABILITY_QUOTE},
            {"id": "probability-zero", "metric": "p_x0_a0", "expected": 0.5, "requirement_quote": PROBABILITY_QUOTE},
            *[{"id": method + "-primary-scope", "metric": method + "_aggregation_error", "expected": 0.0,
               "requirement_quote": AGGREGATION_QUOTE} for method in ("proposed", "baseline")],
        ],
    }
    atomic_json(root / "requirements_audit.json", record)
    failures, _, _ = verify_requirements_audit(root, state)
    return {"session": root.name, "passed": not failures, "failures": failures,
            "measured": run["emitted_metrics"], "scope": record["scope"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.session_dir)
    if args.output:
        atomic_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
