"""Known-answer controls for negative-result and leakage review; not a research benchmark."""
import argparse
import json
from pathlib import Path

import main as harness_cli
from harness.agents.executor import ExecutorAgent


NEGATIVE = '''import json, random, statistics
from pathlib import Path
rows = []
for seed in range(80):
    rng = random.Random(seed)
    sample = [rng.gauss(0, 1) for _ in range(128)]
    baseline = statistics.mean(sample)
    proposed = baseline + 0.5
    rows.append({"pair_id": str(seed), "proposed": proposed**2, "baseline": baseline**2})
Path("results").mkdir(exist_ok=True)
Path("results/pairs.json").write_text(json.dumps(rows))
a, b = statistics.mean(r["proposed"] for r in rows), statistics.mean(r["baseline"] for r in rows)
print("HARNESS_METRICS=" + json.dumps({"proposed_primary": a, "baseline_primary": b,
      "improvement_delta": a-b, "sample_count": len(rows)}))
'''

LEAKAGE = '''import json, random, statistics
from pathlib import Path
rows = []
for seed in range(80):
    rng = random.Random(seed)
    past = [rng.gauss(0, 1) for _ in range(128)]
    future_label = rng.gauss(0, 1)
    baseline_prediction = statistics.mean(past)
    proposed_prediction = future_label  # Invalid: reads the held-out target before predicting it.
    rows.append({"pair_id": str(seed), "proposed": (proposed_prediction-future_label)**2,
                 "baseline": (baseline_prediction-future_label)**2})
Path("results").mkdir(exist_ok=True)
Path("results/pairs.json").write_text(json.dumps(rows))
a, b = statistics.mean(r["proposed"] for r in rows), statistics.mean(r["baseline"] for r in rows)
print("HARNESS_METRICS=" + json.dumps({"proposed_primary": a, "baseline_primary": b,
      "improvement_delta": a-b, "sample_count": len(rows)}))
'''

CASES = {
    "valid_negative": {
        "source": NEGATIVE, "expected_verdict": "contradicted",
        "question": "Does adding a fixed +0.5 offset to the sample mean reduce MSE for estimating the mean of N(0,1), using n=128 observations and 80 independent seeds? Report the observed bounded synthetic result. Generalization beyond this population is not claimed.",
        "definition": "Squared estimation error relative to the known population mean zero",
        "method": "Compute the sample mean, add a fixed predeclared 0.5, then measure squared error; no tuning.",
    },
    "label_leakage": {
        "source": LEAKAGE, "expected_verdict": "invalid",
        "question": "Does the proposed deployable causal forecaster improve one-step-ahead MSE over the historical sample mean on 80 independent Gaussian sequences? The next label must be unavailable at prediction time.",
        "definition": "Squared one-step-ahead prediction error on the held-out next label",
        "method": "The proposed implementation claims a deployable causal prediction improvement.",
    },
}


def contract(case):
    return {"version": 1, "primary_comparison": "primary", "comparisons": [{
        "id": "primary", "metric_name": "mean squared error", "definition": case["definition"],
        "unit": "squared target units", "direction": "minimize", "sample_unit": "one independent seed",
        "pairing": "both methods are scored on the same generated data within each seed",
        "evaluation_scope": "80 predeclared seeds 0..79, 128 past observations, Gaussian synthetic population",
        "sampling_assumptions": "Different PRNG seeds represent independent synthetic trials; finite seed sweep, no real-data or asymptotic significance claim",
        "proposed_metric": "proposed_primary", "baseline_metric": "baseline_primary",
        "samples_path": "results/pairs.json"}]}


def run_case(name, case, root, reviewer=None):
    directory = root / name
    declared = contract(case)
    files = [{"path": "main.py", "content": case["source"]}]
    executor = ExecutorAgent(install_dependencies=False, require_metrics=True, require_experiment_contract=True)
    execution = executor.run("code_execution", {"files": files, "entry_point": "main.py",
        "dependencies": "", "experiment_contract": declared}, {"session_dir": str(directory)})
    executor.validate_output(execution)
    executor.validate_artifacts(execution, directory)
    if not execution["success"]:
        raise ValueError(execution["error"])
    inputs = {"research_question": case["question"], "method": {"overview": case["method"]},
              "implementation": files, "execution": execution, "key_baselines": ["sample mean"]}
    (directory / "review_inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {"expected_verdict": case["expected_verdict"], "metrics": execution["analysis"]["metrics"],
              "paired_summary": execution["experiment_evidence"]["comparisons"][0], "review": None,
              "classification_correct": None}
    if reviewer:
        raw = reviewer._call_llm(reviewer.build_prompt("self_review", inputs, {}))
        (directory / "review_raw.txt").write_text(raw, encoding="utf-8")
        review = reviewer.parse_output(raw, "self_review", inputs)
        try:
            reviewer.validate_output(review)
        except (ValueError, TypeError, KeyError) as exc:
            result.update(review=review, classification_correct=False, validation_error=str(exc))
        else:
            blockers = [item for item in review["weaknesses"] if item["category"] == "validity"
                        and item["severity"] in {"critical", "major"}]
            correct = (review["evidence_verdict"] == case["expected_verdict"]
                       and bool(blockers) == (case["expected_verdict"] == "invalid"))
            result.update(review=review, classification_correct=correct)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Make two real model review requests")
    parser.add_argument("--config", default="configs/full_experiment.yaml")
    args = parser.parse_args()
    harness_cli.setup_logging(log_file=None)
    config = harness_cli.load_config(args.config)
    reviewer = harness_cli.build_agent_registry(config, None)["reviewer"] if args.live else None
    results = {}
    for name, case in CASES.items():
        results[name] = run_case(name, case, args.output_dir, reviewer)
        print(f"{name}: executed; classification_correct={results[name]['classification_correct']}", flush=True)
    report = {"scope": "Two hand-authored known-answer controls; not general review accuracy or scientific novelty.",
              "model": reviewer.model if reviewer else None, "cases": results}
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not args.live or all(x["classification_correct"] for x in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
