"""Replay preserved, unaccepted generated cases without model calls or downloads."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from harness.core.io import safe_path
from harness.tools.process import run_command
from harness.agents.executor import ExecutorAgent
from harness.tools.execution_evidence import verify_execution_evidence


ROOT = Path(__file__).resolve().parent / "live_cases"
PROBES = {
    "forecast_r6": """import json
from train import run_experiment
print(json.dumps(run_experiment()))
""",
    "robust_location_v2": """import contextlib,io,json,statistics,math
from train import run_experiment
with contextlib.redirect_stdout(io.StringIO()):
    result = run_experiment()
rows = []
for scenario, methods in result['per_seed_raw_data'].items():
    a, b = methods['SATS (Proposed)'], methods['Fixed Trimmed']
    assert len(a) == len(b) == 50
    differences = [x-y for x,y in zip(a,b)]
    rows.append({'scenario': scenario, 'paired_repeats': len(differences),
                 'paired_mean_mse_difference': statistics.mean(differences),
                 'paired_se': statistics.stdev(differences)/math.sqrt(len(differences))})
print(json.dumps(rows))
""",
}


def replay(name, case, evidence_root):
    directory = safe_path(ROOT, name)
    files = []
    for item in case["files"]:
        # Git may change LF to CRLF on Windows; compare normalized source text.
        content = safe_path(directory, item["path"]).read_text(encoding="utf-8")
        if hashlib.sha256(content.encode()).hexdigest() != item["content_sha256"]:
            raise ValueError(f"Source changed: {name}/{item['path']}")
        files.append({"path": item["path"], "content": content})
    case_root = evidence_root / name
    execution = ExecutorAgent(install_dependencies=False, require_metrics=True, timeout=180).run(
        "code_execution", {"files": files, "entry_point": case["entry_point"],
                           "dependencies": case["declared_dependencies"]},
        {"session_dir": str(case_root)})
    errors, log_manifest = verify_execution_evidence(case_root, execution)
    (case_root / "execution.json").write_text(
        json.dumps(execution, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not execution["success"] or errors:
        raise RuntimeError(f"{name} failed: {execution['error']} {errors}")
    metrics = execution["analysis"]["metrics"]
    expected = case["metrics"]
    match = (metrics.keys() == expected.keys()
             and all(math.isclose(value, expected[key], rel_tol=1e-12, abs_tol=1e-12)
                     for key, value in metrics.items()))
    audit = run_command([sys.executable, "-c", PROBES[name]], cwd=directory, timeout=180)
    if not audit["success"]:
        raise RuntimeError(f"{name} audit failed: {audit['stderr']}")
    return {"source_hashes_verified": True, "same_metrics": match,
            "archived_evidence_verified": not errors, "log_manifest": log_manifest,
            "metric_count": len(metrics), "metrics": metrics,
            "independent_audit": json.loads(audit["stdout"]),
            "original_acceptance": case["acceptance"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence_root = args.output.parent / (args.output.stem + "_evidence")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    report = {
        "scope": "Deterministic replay verifies recorded numbers, not scientific validity or independent replication.",
        "metric_tolerance": {"relative": 1e-12, "absolute": 1e-12},
        "evidence_directory": evidence_root.name,
        "cases": {name: replay(name, case, evidence_root) for name, case in manifest["cases"].items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, case in report["cases"].items():
        print(f"{name}: source verified; {case['metric_count']} metrics; reproduced={case['same_metrics']}; "
              f"original acceptance={case['original_acceptance']['decision']}")
    return 0 if all(case["same_metrics"] for case in report["cases"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
