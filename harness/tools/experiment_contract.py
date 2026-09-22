"""Declared paired comparisons and independently recomputed descriptive statistics.

This checks measurement consistency, not independence, scientific validity or
significance. Pairing and sampling assumptions remain explicit review evidence.
"""
import hashlib
import json
import math
import statistics
import tempfile
from pathlib import Path

from harness.core.io import safe_path

MAX_SAMPLE_BYTES = 5 * 1024 * 1024


def contract_digest(contract):
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def validate_contract(contract, required=False, source_paths=()):
    if contract is None and not required:
        return
    if not isinstance(contract, dict) or contract.get("version") != 1:
        raise ValueError("experiment_contract requires version=1 and declared paired comparisons")
    comparisons = contract.get("comparisons")
    if not isinstance(comparisons, list) or not 1 <= len(comparisons) <= 16:
        raise ValueError("experiment_contract requires 1..16 comparisons")
    identifiers, paths = set(), set()
    reserved = {str(p).replace("\\", "/").casefold() for p in source_paths}
    reserved.update({"requirements.txt", "_harness_quick_test.py"})
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            raise ValueError("comparison must be an object")
        for key in ("id", "metric_name", "definition", "unit", "sample_unit", "pairing",
                    "evaluation_scope", "sampling_assumptions", "proposed_metric", "baseline_metric",
                    "samples_path"):
            if not isinstance(comparison.get(key), str) or not comparison[key].strip():
                raise ValueError(f"comparison needs non-empty {key}")
        if comparison.get("direction") not in {"minimize", "maximize"}:
            raise ValueError("comparison direction must be minimize or maximize")
        if comparison["id"] in identifiers:
            raise ValueError("comparison ids must be unique")
        identifiers.add(comparison["id"])
        path = comparison["samples_path"].replace("\\", "/")
        safe_path(Path.cwd(), path)
        if not path.startswith("results/") or not path.endswith(".json"):
            raise ValueError("paired samples must be runtime JSON files under results/")
        if path.casefold() in reserved or path.casefold() in paths:
            raise ValueError("paired sample paths must be unique and cannot be source files")
        paths.add(path.casefold())
        keys = (comparison["proposed_metric"], comparison["baseline_metric"])
        if keys[0] == keys[1]:
            raise ValueError("proposed and baseline metric keys must be distinct")
    if contract.get("primary_comparison") not in identifiers:
        raise ValueError("primary_comparison must name a declared comparison")


def _summarize(raw, comparison, metrics):
    rows = json.loads(raw)
    if not isinstance(rows, list) or not 2 <= len(rows) <= 20000:
        raise ValueError("paired samples must contain 2..20000 rows")
    ids, proposed, baseline = set(), [], []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("pair_id"), str) or not row["pair_id"].strip():
            raise ValueError("each sample needs a non-empty string pair_id")
        if row["pair_id"] in ids:
            raise ValueError("duplicate pair_id; pair units must be unique")
        ids.add(row["pair_id"])
        for field, values in (("proposed", proposed), ("baseline", baseline)):
            value = row.get(field)
            if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > 1e150:
                raise ValueError(f"sample {field} must be finite numeric data within arithmetic limits")
            values.append(float(value))
    a, b = statistics.fmean(proposed), statistics.fmean(baseline)
    for key, value in ((comparison["proposed_metric"], a), (comparison["baseline_metric"], b)):
        reported = metrics.get(key)
        if type(reported) not in (float, int) or not math.isclose(value, reported, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(f"{key} differs from paired raw sample mean")
    differences = [x - y for x, y in zip(proposed, baseline)]
    delta = statistics.fmean(differences)
    return {"id": comparison["id"], "pair_count": len(rows), "proposed_mean": a,
            "baseline_mean": b, "paired_mean_difference": delta,
            "paired_standard_error": statistics.stdev(differences) / math.sqrt(len(rows)),
            "improvement_direction": "negative" if comparison["direction"] == "minimize" else "positive",
            "uncertainty_scope": "Descriptive paired SE; independence assumptions are declared, not verified. No significance decision."}


def _read_samples(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_SAMPLE_BYTES + 1)
    if len(raw) > MAX_SAMPLE_BYTES:
        raise ValueError(f"paired sample file exceeds {MAX_SAMPLE_BYTES} bytes")
    return raw


def _primary_check(contract, summaries, metrics):
    primary = next(x for x in summaries if x["id"] == contract["primary_comparison"])
    # Standard keys refer to one precisely declared primary comparison, not a
    # mixture of scenario means and an unrelated count of individual examples.
    expected = {"proposed_primary": primary["proposed_mean"],
                "baseline_primary": primary["baseline_mean"],
                "improvement_delta": primary["paired_mean_difference"],
                "sample_count": primary["pair_count"]}
    for key, value in expected.items():
        if key in metrics and (type(metrics[key]) not in (float, int)
                               or not math.isclose(metrics[key], value, rel_tol=1e-9, abs_tol=1e-12)):
            raise ValueError(f"{key} must describe primary comparison {contract['primary_comparison']}")


def collect_experiment_evidence(contract, code_dir, archive_dir, session_dir, metrics):
    validate_contract(contract, required=True)
    summaries = []
    for comparison in contract["comparisons"]:
        raw = _read_samples(safe_path(code_dir, comparison["samples_path"]))
        summary = _summarize(raw, comparison, metrics)
        with tempfile.NamedTemporaryFile(dir=archive_dir, prefix="paired_", suffix=".json", delete=False) as file:
            file.write(raw)
            destination = Path(file.name)
        summary["artifact"] = {"path": destination.resolve().relative_to(Path(session_dir).resolve()).as_posix(),
                               "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        summaries.append(summary)
    _primary_check(contract, summaries, metrics)
    return {"contract": contract, "contract_sha256": contract_digest(contract), "comparisons": summaries}


def verify_experiment_evidence(session_dir, execution, expected_contract=None):
    required = execution.get("execution_policy", {}).get("require_experiment_contract", False)
    evidence = execution.get("experiment_evidence")
    if evidence is None and not required and expected_contract is None:
        return [], []
    manifest = []
    try:
        if not isinstance(evidence, dict):
            raise ValueError("Missing paired experiment evidence; rerun coding and code_execution")
        contract = evidence.get("contract")
        validate_contract(contract, required=True)
        if expected_contract is not None and contract != expected_contract:
            raise ValueError("Executed contract differs from coding's declared contract")
        if contract_digest(contract) != evidence.get("contract_sha256"):
            raise ValueError("Experiment contract hash mismatch")
        summaries = evidence.get("comparisons")
        if not isinstance(summaries, list) or len(summaries) != len(contract["comparisons"]):
            raise ValueError("Missing comparison evidence")
        computed, used = [], set()
        for comparison, summary in zip(contract["comparisons"], summaries):
            if not isinstance(summary, dict) or not isinstance(summary.get("artifact"), dict):
                raise ValueError("Missing comparison artifact")
            record = summary["artifact"]
            path = safe_path(session_dir, record.get("path"))
            if path in used:
                raise ValueError("Comparisons reused the same sample artifact")
            used.add(path)
            raw = _read_samples(path)
            if type(record.get("bytes")) is not int or len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record.get("sha256"):
                raise ValueError("Paired sample artifact hash/size mismatch")
            recalculated = _summarize(raw, comparison, execution.get("analysis", {}).get("metrics", {}))
            if recalculated != {key: value for key, value in summary.items() if key != "artifact"}:
                raise ValueError("Cached paired summary differs from raw observations")
            computed.append(recalculated)
            manifest.append({**record, "source": f"paired comparison:{comparison['id']}"})
        _primary_check(contract, computed, execution.get("analysis", {}).get("metrics", {}))
        return [], manifest
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        return [str(exc)], manifest
