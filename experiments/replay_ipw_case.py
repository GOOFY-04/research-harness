"""Reproduce the preserved IPW failure, offline; a reproduced failure is not acceptance."""
import argparse
import hashlib
import json
import math
from pathlib import Path

from experiments.audit_ipw_requirements import audit
from harness.agents.executor import ExecutorAgent
from harness.core.io import atomic_json, safe_path


def replay(output_dir):
    source_dir = Path(__file__).resolve().parent / "live_cases/ipw_20260922"
    case = json.loads((source_dir / "case.json").read_text(encoding="utf-8"))
    files = []
    for item in case["files"]:
        source = safe_path(source_dir, item["path"]).read_text(encoding="utf-8")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != item["content_sha256"]:
            raise ValueError(f"Preserved source changed: {item['path']}")
        files.append({"path": item["path"], "content": source})
    coding = {key: case[key] for key in ("entry_point", "dependencies", "experiment_contract")}
    coding["files"] = files
    root = Path(output_dir).resolve()
    execution = ExecutorAgent(install_dependencies=False, require_metrics=True,
                              require_experiment_contract=True, timeout=180).run(
        "code_execution", coding, {"session_dir": str(root)})
    if not execution["success"]:
        raise ValueError(f"Replay execution failed: {execution.get('error')}")
    metrics = execution["analysis"]["metrics"]
    same_metrics = (metrics.keys() == case["metrics"].keys() and all(
        math.isclose(value, case["metrics"][key], rel_tol=1e-12, abs_tol=1e-12) for key, value in metrics.items()))
    state = {"metadata": {"research_direction": case["original_direction"]},
             "stages": {"coding": {"output": coding}, "code_execution": {"output": execution}}}
    atomic_json(root / "checkpoint.json", state)
    diagnostic = audit(root)
    failure_ids = [item.split(":", 1)[0] for item in diagnostic["failures"]]
    reproduced = same_metrics and failure_ids == ["probability-sign", "proposed-primary-scope", "baseline-primary-scope"]
    result = {"scope": "Offline diagnostic replay, not a full workflow or independent statistical replication",
              "same_metrics": same_metrics, "metric_count": len(metrics),
              "recorded_workflow_completed": case["workflow_completed"],
              "original_acceptance": case["acceptance"], "diagnostic": diagnostic,
              "expected_failures_reproduced": reproduced}
    atomic_json(root / "replay.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["expected_failures_reproduced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
