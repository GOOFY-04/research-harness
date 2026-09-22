"""Small, live model sanity check; invokes the configured provider and consumes API usage."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from main import load_config
from harness.core.agent import BaseAgent
from harness.core.llm import LLMClient


CASES = [
    {"id": "causal_next_step", "valid": True, "algorithm":
     "At t, output interval C_t from H_(t-1). Then observe y_t, update H_t, and choose alpha_next for the interval at t+1."},
    {"id": "current_label_leak", "valid": False, "algorithm":
     "At t, observe y_t, append residual |y_t - prediction_t| to H, calibrate q from H, and report [prediction_t-q, prediction_t+q] for the same t as an out-of-sample interval."},
    {"id": "conservative_fallback", "valid": True, "algorithm":
     "alpha is miscoverage; interval radius is Q(1-alpha) for the same fixed residual list. When no candidate is feasible, use the smallest alpha in the grid to obtain the widest candidate interval. No coverage guarantee is claimed."},
    {"id": "inverted_fallback", "valid": False, "algorithm":
     "alpha is miscoverage; interval radius is Q(1-alpha) for the same fixed residual list. Use the largest alpha in the grid and claim this gives the widest candidate interval."},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/full_experiment.yaml")
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)["llm"]
    prompt = ('Check each algorithm for internal logical consistency. Output JSON only: '
              '{"results":[{"id":"case id","valid":true or false,"reason":"one short numerical or time-index check"}]}. '
              'Correctly distinguish next-step updates from current-label leakage. Cases: '
              + json.dumps([{key: value for key, value in case.items() if key != "valid"} for case in CASES]))
    results = []
    for model in args.models or [config["default_model"]]:
        started = time.monotonic()
        client = LLMClient(protocol=config["protocol"], base_url=config["base_url"],
                           api_key=config.get("api_key") or None,
                           api_key_env=config.get("api_key_env"), timeout=45)
        row = {"model": model}
        try:
            raw = client.create(model=model, max_tokens=1024, temperature=0,
                                messages=[{"role": "user", "content": prompt}])
            data = BaseAgent._parse_json(raw)
            answers = {item.get("id"): item for item in data.get("results", []) if isinstance(item, dict)}
            row["answers"] = list(answers.values())
            row["correct"] = sum(answers.get(case["id"], {}).get("valid") is case["valid"] for case in CASES)
        except (ValueError, RuntimeError, OSError) as exc:
            row["error"] = str(exc)[:200]
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        results.append(row)
        print(json.dumps(row, ensure_ascii=True), flush=True)
    report = {"scope": "four hand-authored sanity cases; not a benchmark of research quality",
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "expected": CASES, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row.get("correct") == len(CASES) for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
