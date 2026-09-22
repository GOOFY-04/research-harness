"""Generate typed files with shared interfaces and validate before publishing."""
import ast
import hashlib
import json
import logging
import re
from pathlib import Path
from uuid import uuid4
from harness.core.agent import BaseAgent
from harness.tools.experiment_contract import validate_contract
from harness.core.io import atomic_json, safe_path, strip_outer_fence
from harness.tools.validation import (validate_file, validate_files, validate_dependencies,
                                      validate_imports, validate_metric_constraints)

logger = logging.getLogger(__name__)


def interfaces(files):
    result = {}
    for info in files:
        if not info["path"].endswith(".py"):
            continue
        tree = ast.parse(info["content"])
        lines = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.append(f"def {node.name}({ast.unparse(node.args)})")
            elif isinstance(node, ast.ClassDef):
                lines.append(f"class {node.name}:")
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        lines.append(f"  def {member.name}({ast.unparse(member.args)})")
        result[info["path"]] = "\n".join(lines)
    return result


def _tail(value, limit=5000):
    value = str(value or "")
    return value if len(value) <= limit else "...<truncated>\n" + value[-limit:]


def execution_failure_context(output):
    """Keep the actionable tail of subprocess output for an LLM repair request."""
    runs = []
    for run in output.get("runs", []) if isinstance(output, dict) else []:
        if not isinstance(run, dict):
            continue
        runs.append({
            "command": run.get("command"),
            "returncode": run.get("returncode"),
            "timed_out": run.get("timed_out", False),
            "stdout": _tail(run.get("stdout")),
            "stderr": _tail(run.get("stderr")),
            "emitted_metrics": run.get("emitted_metrics", {}),
        })
    return {
        "error": output.get("error", "") if isinstance(output, dict) else str(output),
        "execution_kind": output.get("execution_kind") if isinstance(output, dict) else None,
        "execution_policy": output.get("execution_policy", {}) if isinstance(output, dict) else {},
        "install_log": _tail(output.get("install_log")) if isinstance(output, dict) else "",
        "runs": runs,
    }


def traceback_repair_plan(failure, generated_paths):
    """Localize a Python traceback to its deepest generated project file."""
    chunks = []
    for run in failure.get("runs", []) if isinstance(failure, dict) else []:
        if isinstance(run, dict):
            chunks.extend((str(run.get("stdout", "")), str(run.get("stderr", ""))))
    text = "\n".join(chunks)
    frames = re.findall(r'File\s+["\']([^"\']+)["\'](?:,\s+line\s+\d+)?', text)
    normalized = {path: path.replace("\\", "/") for path in generated_paths}
    localized = []
    for frame in frames:
        frame = frame.replace("\\", "/")
        for path, portable in normalized.items():
            if frame == portable or frame.endswith("/" + portable):
                localized.append(path)
                break
    if not localized:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    diagnosis = lines[-1] if lines else "Python traceback in generated source"
    return {"diagnosis": diagnosis, "files": [localized[-1]],
            "dependencies": None, "regenerate_test": False,
            "localization": "deepest_generated_traceback_frame"}


def metric_contract_repair_plan(failure, entry_point, generated_paths):
    """Metric protocol failures are emitted by the entry point, so localize them directly."""
    error = str(failure.get("error", "")) if isinstance(failure, dict) else ""
    if (entry_point not in generated_paths
            or not (error.startswith("Entry point completed without a HARNESS_METRICS")
                    or error.startswith("HARNESS_METRICS is missing required keys")
                    or error.startswith("HARNESS_METRICS violates"))):
        return None
    detail = error
    if error.startswith("Entry point completed without"):
        detail += (". Print exactly one final line with the literal prefix HARNESS_METRICS= "
                   "followed immediately by a JSON object; a space instead of '=' is invalid. "
                   "Preserve and emit all already computed numeric results for every method, "
                   "including secondary metrics; the required keys are a minimum, not a replacement "
                   "for the existing results. Nested numeric objects are supported")
    return {"diagnosis": detail, "files": [entry_point], "dependencies": None,
            "regenerate_test": False, "localization": "metric_contract_entry_point"}


def source_context(files, total_limit=60000, file_limit=16000):
    """Bound repair prompts while retaining both ends of larger generated files."""
    result, remaining = [], total_limit
    for item in files:
        if remaining <= 0:
            break
        content = str(item.get("content", ""))
        limit = min(file_limit, remaining)
        if len(content) > limit:
            half = max(1, (limit - 32) // 2)
            content = content[:half] + "\n...<truncated>...\n" + content[-half:]
        result.append({"path": item.get("path"), "content": content})
        remaining -= len(content)
    return result


def ordered_manifest(manifest, session_dir):
    """Validate the file graph before generation and put consumers after dependencies."""
    items = manifest.get("files") if isinstance(manifest, dict) else None
    if (not isinstance(items, list) or not items or manifest.get("parse_error")
            or not all(isinstance(item, dict)
                       and isinstance(item.get("path"), str)
                       and isinstance(item.get("description"), str) for item in items)):
        raise ValueError("manifest needs non-empty file objects with path and description")
    paths = [item["path"] for item in items]
    portable = [path.replace("\\", "/").casefold() for path in paths]
    if len(set(portable)) != len(paths):
        raise ValueError("manifest paths must be unique (including case and separator aliases)")
    for path in paths:
        safe_path(session_dir, path)
    entry = manifest.get("entry_point")
    if not isinstance(entry, str) or entry not in paths:
        raise ValueError("manifest entry_point must name one of its files")
    dependencies = {}
    for item in items:
        required = item.get("depends_on", [])
        if (not isinstance(required, list)
                or not all(isinstance(path, str) and path in paths for path in required)
                or len(set(required)) != len(required)):
            raise ValueError("manifest depends_on must list unique declared file paths")
        dependencies[item["path"]] = set(required)
    # Even old manifests without dependency metadata benefit from generating the
    # experiment driver with all implemented APIs available.
    dependencies[entry].update(path for path in paths if path != entry)
    ordered, pending = [], list(items)
    while pending:
        ready = next((item for item in pending if not dependencies[item["path"]]), None)
        if ready is None:
            raise ValueError("manifest file dependencies contain a cycle or import the entry point")
        pending.remove(ready)
        ordered.append(ready)
        for required in dependencies.values():
            required.discard(ready["path"])
    return {**manifest, "files": ordered}


CONTRACT_INSTRUCTION = """
Declare this experiment contract BEFORE generating source or executing code:
{"version":1,"primary_comparison":"primary","comparisons":[{
"id":"primary","metric_name":"mean squared estimation error","definition":"squared error against the known synthetic target, averaged over independent trial seeds",
"unit":"target units squared","direction":"minimize","sample_unit":"one independent trial seed",
"pairing":"both estimators receive the exact same sample for each seed",
"evaluation_scope":"identify scenario, held-out population, time range and any aggregation",
"sampling_assumptions":"state which seeds are independent and any temporal or grouped dependence",
"proposed_metric":"proposed_primary","baseline_metric":"baseline_primary","samples_path":"results/primary_pairs.json"}]}
Adapt every field to the actual research question; the example metric is NOT a requirement.
Declare ALL planned method/baseline comparisons, scenarios and secondary measurement comparisons
needed to evaluate the hypothesis. Use unique ids and separate files. A shared baseline metric
key is allowed only when it describes the same evaluation scope and sample mean.
The declared samples_path files are runtime OUTPUTS, not source manifest files.
Each is a JSON array of {"pair_id":"unique seed or matched evaluation unit","proposed":number,"baseline":number}.
Write observed per-unit scores, not repetitions of aggregate results or fabricated uncertainty.
Both metric keys must equal the arithmetic means of their corresponding raw sample columns.
The four standard primary keys refer to primary_comparison; sample_count is its number of pairs.
If averaging multiple scenarios, pair independent seed-level aggregates with explicitly declared
weights; additionally export per-scenario comparisons. Do not treat dependent time steps as independent seeds.
HARNESS computes paired differences and SE from these files; preserve negative results.
The contract is fixed through code repair. Changing hypotheses, metric semantics or evaluation
scope requires a new research revision, not changing the contract to fit observed results.
"""


class CoderAgent(BaseAgent):
    required_fields = {"files": list, "entry_point": str, "dependencies": str,
                       "run_instructions": str, "test_snippet": str}

    def __init__(self, *args, allowed_dependencies=None, required_metric_keys=None,
                 metric_constraints=None, require_experiment_contract=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_dependencies = allowed_dependencies
        self.required_metric_keys = required_metric_keys or []
        self.metric_constraints = validate_metric_constraints(metric_constraints)
        self.require_experiment_contract = bool(require_experiment_contract)
        if self.allowed_dependencies is not None:
            if (not isinstance(self.allowed_dependencies, list)
                    or not all(isinstance(item, str) and item.strip()
                               for item in self.allowed_dependencies)):
                raise ValueError("allowed_dependencies must be a list of package names")
        if (not isinstance(self.required_metric_keys, list)
                or not all(isinstance(item, str) and item.strip() for item in self.required_metric_keys)):
            raise ValueError("required_metric_keys must be a list of non-empty strings")

    def build_prompt(self, stage_id, inputs, state):
        return ""

    def _dependency_instruction(self):
        """Render the dependency policy for every independent model request."""
        if self.allowed_dependencies == []:
            return ("Dependency constraint: use only the Python standard library; "
                    "do not import or declare any third-party package.")
        if self.allowed_dependencies is not None:
            allowed = json.dumps(self.allowed_dependencies, ensure_ascii=False)
            return ("Dependency constraint: third-party imports and requirements are "
                    f"limited to this allowlist: {allowed}.")
        return "Dependency constraint: honor the dependency and resource limits in the research design."

    def _call_repair(self, prompt):
        """Retry a failed repair sub-request without rerunning unchanged code."""
        previous_tokens = self.max_tokens
        try:
            # A repair returns one bounded source file or a tiny JSON plan.
            # Keeping the ceiling below full-project generation reduces stalls.
            self.max_tokens = min(self.max_tokens, 6144)
            for attempt in range(2):
                try:
                    return self._call_llm(prompt)
                except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
                    message = str(exc).lower()
                    transient = any(token in message for token in
                                    ("timeout", "timed out", "connection", "temporarily", "unavailable"))
                    if attempt or not transient:
                        raise
                    logger.warning("Coder repair sub-request failed; retrying once: %s", exc)
        finally:
            self.max_tokens = previous_tokens

    def _measurement_instruction(self, contract):
        instruction = (
            f"Runtime measurement requirements: emit all numeric keys {json.dumps(self.required_metric_keys)} "
            f"in one final HARNESS_METRICS JSON line; constraints: {json.dumps(self.metric_constraints)}. "
            "Preserve all secondary measurements. improvement_delta is proposed_primary minus baseline_primary.\n"
        )
        if contract:
            instruction += (
                "For EVERY experiment_contract comparison, write its samples_path at runtime as a JSON array "
                "of objects with EXACT keys pair_id (unique non-empty STRING), proposed (number), baseline (number). "
                "Do not substitute seed or metric-key names for these three column names. "
                "The comparison's proposed_metric and baseline_metric must equal the arithmetic means of these columns. "
                "The four standard primary metrics describe primary_comparison, with sample_count equal to its row count. "
                "Preserve the declared metric definition, pairing and scope; do not switch from grouped scenarios "
                "to a favorable single scenario. Do not redefine or choose the contract inside the running program.\n"
            )
        return instruction

    def _generate_smoke_test(self, files, context, dependency_instruction, repair=False):
        """Generate a smoke test from real source contracts, not guessed signatures."""
        prompt = f"""Write a short Python smoke test for this exact implementation.
Use CPU and tiny synthetic inputs. Assert only behavior supported by the supplied source,
including its real return types. Do not invent a different interface contract, fixed metric
values, downloads, training workloads, or publication claims. Prefer construction, shape,
finite-value, and multi-step integration checks using the entry point's exact calling sequence.
For each stateful method AND baseline, use nondegenerate inputs after any required warmup
and check that observations reach the intended model/statistics (no silent no-op update).
Use the method invariants where executable. Do not assert that the proposed method wins.
Use ordinary top-level assertions; do not build a custom test runner or catch assertion failures,
because the harness needs the complete traceback. Keep the test compact and omit tutorial comments.
For floating-point boundary claims, use justified tolerances: an asymptotic limit does not imply
exact equality at a finite input (for example, sigmoid(-100) is positive, not exactly zero).
Return only Python code.
{dependency_instruction}
Public interfaces: {json.dumps(interfaces(files), ensure_ascii=False)}
Implementation source: {json.dumps(source_context(files, total_limit=45000), ensure_ascii=False)}
Research context: {context}"""
        call = self._call_repair if repair else self._call_llm
        for attempt in range(3):
            test = strip_outer_fence(call(prompt), ("python",))
            try:
                compile(test, "<generated_test>", "exec")
                validate_imports(
                    files + [{"path": "__harness_smoke_test__.py", "content": test}],
                    self.allowed_dependencies,
                )
                return test
            except (ValueError, SyntaxError) as exc:
                if attempt == 2:
                    raise
                prompt += f"\nPrevious smoke test was invalid: {exc}. Regenerate it completely."
        raise ValueError("Smoke test generation failed")

    def _draft_path(self, state, context):
        policy = json.dumps({
            "context": context,
            "allowed_dependencies": self.allowed_dependencies,
            "required_metric_keys": self.required_metric_keys,
            "metric_constraints": self.metric_constraints,
            "require_experiment_contract": self.require_experiment_contract,
        }, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(policy.encode("utf-8")).hexdigest()
        path = safe_path(state.get("session_dir", "."), f".drafts/coding_{digest[:20]}.json")
        return path, digest

    def _load_draft(self, path, digest, state):
        if not path.is_file():
            return None
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
            manifest = draft["manifest"]
            files = draft.get("files", [])
            if draft.get("context_sha256") != digest or not isinstance(manifest.get("files"), list):
                return None
            ordered = ordered_manifest(manifest, state.get("session_dir", "."))
            if not files:
                draft["manifest"] = manifest = ordered
            manifest_paths = [item["path"] for item in manifest["files"]]
            if manifest.get("entry_point") not in manifest_paths:
                return None
            if [item.get("path") for item in files] != manifest_paths[:len(files)]:
                return None
            validate_dependencies(manifest.get("dependencies", ""), self.allowed_dependencies)
            validate_contract(manifest.get("experiment_contract"), bool(files) and self.require_experiment_contract,
                              manifest_paths)
            for info in manifest["files"]:
                safe_path(state.get("session_dir", "."), info["path"])
            for item in files:
                validate_file(item["path"], item["content"])
            test = draft.get("test_snippet")
            if test is not None:
                if not isinstance(test, str):
                    return None
                compile(test, "<persisted_generated_test>", "exec")
                validate_imports(files + [{"path": "__harness_smoke_test__.py", "content": test}],
                                 self.allowed_dependencies)
            return draft
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, SyntaxError):
            return None

    @staticmethod
    def _save_draft(path, digest, manifest, files, test_snippet=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "context_sha256": digest,
                   "manifest": manifest, "files": files}
        if test_snippet is not None:
            payload["test_snippet"] = test_snippet
        atomic_json(path, payload)

    def run(self, stage_id, inputs, state):
        research_context = dict(inputs)
        direction = state.get("metadata", {}).get("research_direction")
        if direction:
            research_context["original_research_direction"] = direction
        method = state.get("stages", {}).get("method_design", {}).get("output") or {}
        if method.get("invariants"):
            research_context["method_invariants"] = method["invariants"]
        feedback = state.get("stage_inputs_override", {}).get("method_design", {}).get("review_feedback")
        if feedback:
            research_context["previous_review_findings_to_address"] = feedback
        context = json.dumps(research_context, ensure_ascii=False, indent=2)
        dependency_instruction = self._dependency_instruction()
        draft_path, context_digest = self._draft_path(state, context)
        draft = self._load_draft(draft_path, context_digest, state)
        if draft:
            manifest = draft["manifest"]
            files = list(draft.get("files", []))
            logger.info("[CoderAgent] resuming coding draft with %s/%s files",
                        len(files), len(manifest["files"]))
        else:
            logger.info("[CoderAgent] generating implementation manifest")
            manifest_prompt = f"""You are implementing a research prototype.
Use the fewest cohesive files needed (typically 2-6), with consistent public interfaces.
List dependencies before consumers and the entry point last. Return a JSON object:
{{"files":[{{"path":"relative/path.py","description":"purpose","interface":"exact public class/function signatures",
"depends_on":[],"contract":"return types, state ownership, required call order, warmup and update pre/postconditions"}}],
"entry_point":"train.py","dependencies":"requirements.txt text","run_instructions":"Markdown"}}
depends_on contains generated file paths, not package names. No file may import the entry point.
For stateful comparisons, explicitly assign ownership of fitting, prediction, calibration and updates
so neither the baseline nor the proposed model silently skips updates or observes held-out labels early.
Use Python and honor the research design's dependency and resource constraints. Do not introduce
PyTorch, OpenCV, GPU code, or other heavy packages unless the supplied design explicitly requires them.
Allowed third-party dependencies: {json.dumps(self.allowed_dependencies, ensure_ascii=False)}.
When this value is not null, every declared dependency must be in that list.
{dependency_instruction}
List only PEP 508 package requirements, never pip flags or direct URLs.
When no third-party packages are needed, dependencies MUST be the empty string, not "None", "null" or prose.
The entry point will be executed by the harness as a bounded end-to-end experiment. It must:
- run with no mandatory command-line arguments and require no external files, downloads or credentials;
- use deterministic seeds and a download-free synthetic benchmark tied to the research question;
- train/evaluate the proposed method and at least one meaningful baseline on held-out data;
- use fixed, equal update counts for compared methods; a wall-clock check may only be a safety stop and
  must not determine the normal number of optimization steps;
- finish on CPU in under three minutes with small default dimensions and epochs;
- print one final line beginning HARNESS_METRICS= followed by a JSON object of numeric metrics,
  including proposed and baseline scores plus an improvement/delta where meaningful;
- include every configured metric key: {json.dumps(self.required_metric_keys, ensure_ascii=False)};
- also emit the computed secondary metrics and all compared methods in the same JSON object
  (nested numeric objects are supported); required keys are a minimum, not the complete results;
- when proposed_primary, baseline_primary, improvement_delta and sample_count are configured, use the
  same directly observed primary metric for proposed_primary and baseline_primary, define
  improvement_delta = proposed_primary - baseline_primary (a signed raw difference, not a percentage),
  and set sample_count to the integer number of held-out evaluation cases;
- satisfy these executed-metric acceptance constraints: {json.dumps(self.metric_constraints, ensure_ascii=False)};
- clearly label all results as synthetic-benchmark evidence, not publication claims.
Research design:
{context}"""
            for manifest_attempt in range(3):
                raw_manifest = self._call_llm(manifest_prompt)
                manifest = self._parse_json(raw_manifest)
                try:
                    manifest = ordered_manifest(manifest, state.get("session_dir", "."))
                    validate_dependencies(manifest.get("dependencies", ""), self.allowed_dependencies)
                    validate_contract(manifest.get("experiment_contract"), False,
                                      [item["path"] for item in manifest["files"]])
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    self._record_invalid_draft(draft_path, "manifest", raw_manifest, str(exc))
                    if manifest_attempt == 2:
                        raise ValueError(f"Invalid code manifest: {exc}") from exc
                    logger.warning("[CoderAgent] invalid manifest; regenerating: %s", exc)
                    manifest_prompt += (f"\nPrevious manifest was invalid: {exc}. Return only the compact JSON "
                                        "manifest, without file contents or explanatory prose.")
            files = []
            self._save_draft(draft_path, context_digest, manifest, files)
        if self.require_experiment_contract and manifest.get("experiment_contract") is None:
            logger.info("[CoderAgent] file manifest ready; generating experiment measurement contract")
            contract_prompt = ("Return ONLY the experiment_contract JSON object itself, starting with "
                               "version and primary_comparison. Do not return a file manifest.\n"
                               + CONTRACT_INSTRUCTION + "\nResearch design:\n" + context
                               + "\nAccepted file manifest:\n" + json.dumps(manifest, ensure_ascii=False))
            for contract_attempt in range(3):
                raw_contract = self._call_llm(contract_prompt)
                measurement_contract = self._parse_json(raw_contract)
                if set(measurement_contract) == {"experiment_contract"}:
                    measurement_contract = measurement_contract["experiment_contract"]
                try:
                    validate_contract(measurement_contract, True, [item["path"] for item in manifest["files"]])
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    self._record_invalid_draft(draft_path, "contract", raw_contract, str(exc))
                    if contract_attempt == 2:
                        raise ValueError(f"Invalid experiment measurement contract: {exc}") from exc
                    contract_prompt += f"\nPrevious contract was invalid: {exc}. Return the corrected contract object only."
            manifest = {**manifest, "experiment_contract": measurement_contract}
            self._save_draft(draft_path, context_digest, manifest, files)
        manifest_paths = [item["path"] for item in manifest["files"]]
        logger.info("[CoderAgent] manifest accepted: %s files", len(manifest_paths))
        for file_index, info in enumerate(manifest["files"], start=1):
            if file_index <= len(files):
                continue
            path = info["path"]
            logger.info("[CoderAgent] generating file %s/%s: %s", file_index, len(manifest_paths), path)
            extension = Path(path).suffix.lower()
            language = {".py":"python", ".yaml":"yaml", ".yml":"yaml", ".json":"json",
                        ".md":"markdown", ".txt":"text", ".toml":"toml"}.get(extension, "text")
            prompt = f"""Generate the COMPLETE {language} file {path}.
Return only the file content, optionally enclosed in a {language} fence.
Honor the file extension: YAML/JSON files must contain data, never Python.
Implement exactly the shared public interfaces, with all imports and complete bodies.
Do not invent alternate names or parameters. Avoid work at import time.
Use existing source to honor behavioral contracts, not only signatures. Explicitly satisfy update
preconditions and state ownership for every compared method. Keep comments concise; do not include
deliberation transcripts. A source block containing ...<truncated>... is only a context excerpt.
{dependency_instruction}
If this is the entry point, implement the complete deterministic experiment described in the manifest,
including baseline comparison and the HARNESS_METRICS JSON line. Use fixed iteration counts for reproducibility;
never use a wall-clock while-loop as the normal training schedule. Keep defaults bounded and download-free.
{self._measurement_instruction(manifest.get('experiment_contract'))}
Design: {context}
Manifest: {json.dumps(manifest, ensure_ascii=False)}
Existing implemented interfaces: {json.dumps(interfaces(files), ensure_ascii=False)}
Existing implementation source: {json.dumps(source_context(files, total_limit=45000), ensure_ascii=False)}
Current file: {json.dumps(info, ensure_ascii=False)}"""
            for attempt in range(3):
                content = strip_outer_fence(self._call_llm(prompt), (language, "yml", "md", "text"))
                try:
                    validate_file(path, content)
                    # Validate each response while it can still be regenerated. Empty
                    # placeholders preserve knowledge of local module roots without
                    # requiring later files to have been generated already.
                    import_context = [
                        {"path": candidate, "content": content if candidate == path else ""}
                        for candidate in manifest_paths
                    ]
                    validate_imports(import_context, self.allowed_dependencies)
                    break
                except (ValueError, SyntaxError) as exc:
                    logger.warning("[CoderAgent] invalid file %s (attempt %s/3): %s", path, attempt + 1, exc)
                    if attempt == 2:
                        raise
                    prompt += f"\nPrevious output was invalid: {exc}. Regenerate the complete file."
            files.append({**info, "content": content})
            self._save_draft(draft_path, context_digest, manifest, files)
        try:
            validate_files(files, state.get("session_dir", "."))
            validate_imports(files, self.allowed_dependencies)
        except (ValueError, SyntaxError):
            draft_path.unlink(missing_ok=True)
            raise
        if isinstance((draft or {}).get("test_snippet"), str):
            test = draft["test_snippet"]
            logger.info("[CoderAgent] reusing validated smoke test from coding draft")
        else:
            logger.info("[CoderAgent] generating smoke test from implementation source")
            test = self._generate_smoke_test(files, context, dependency_instruction)
            self._save_draft(draft_path, context_digest, manifest, files, test)
        output = {"files": files, "entry_point": manifest.get("entry_point", ""),
                  "dependencies": manifest.get("dependencies", ""),
                  "run_instructions": manifest.get("run_instructions", ""), "test_snippet": test,
                  "experiment_contract": manifest.get("experiment_contract")}
        self.validate_output(output)
        logger.info("[CoderAgent] implementation bundle validated")
        if self.memory:
            self.memory.append(stage_id, {"files": [f["path"] for f in files]}, ["CoderAgent"])
        return output

    def repair(self, stage_id, previous_output, failure_output, state):
        """Repair generated files from concrete executor feedback.

        The first request chooses existing files and explains the failure. Each
        chosen file is then regenerated in full and the merged project receives
        the same static validation as an initial coding result.
        """
        self.validate_output(previous_output)
        files = [dict(item) for item in previous_output["files"]]
        by_path = {item["path"]: item for item in files}
        failure = execution_failure_context(failure_output)
        sources = source_context(files)
        dependency_instruction = self._dependency_instruction()
        contract = {
            "entry_point": previous_output["entry_point"],
            "dependencies": previous_output["dependencies"],
            "test_snippet": previous_output["test_snippet"],
            "experiment_contract": previous_output.get("experiment_contract"),
        }
        plan_prompt = f"""A generated research program failed during execution.
Diagnose the runtime or installation failure and propose the smallest repair.
Return JSON only:
{{"diagnosis":"concrete root cause","files":["existing/path.py"],
  "dependencies":null,"regenerate_test":false}}
"files" must contain only existing generated paths and at most two entries. Prefer one file;
use two only when a public interface and its caller must change together.
Use dependencies only when the requirements text itself must change; otherwise return null.
When the smoke test assumed behavior that the supplied source never promised, set
regenerate_test=true and do not distort correct source code to satisfy that bad assumption.
Set regenerate_test=false when the test exposed a real implementation defect.
If execution_policy.install_dependencies is false, dependencies must be null and the source must avoid incompatible optional packages.
execution_policy.timeout_seconds is the total budget for the smoke test and entry point together, including all baselines and evaluation.
When execution timed out, reduce bounded workload or split the total budget; do not merely add tolerance to an internal timer.
Do not weaken coverage, remove the baseline, suppress errors, hard-code metrics, or skip the experiment.
{dependency_instruction}
The supplied test snippet may contain an unsupported interface assumption. Compare it with
the actual source before deciding whether source or test must change.
Execution failure:
{json.dumps(failure, ensure_ascii=False, indent=2)}
Execution contract:
{json.dumps(contract, ensure_ascii=False, indent=2)}
Current interfaces:
{json.dumps(interfaces(files), ensure_ascii=False, indent=2)}
Current source:
{json.dumps(sources, ensure_ascii=False, indent=2)}"""
        plan = (metric_contract_repair_plan(failure, previous_output["entry_point"], by_path)
                or traceback_repair_plan(failure, by_path))
        if plan:
            logger.info("[CoderAgent] %s localized repair to %s",
                        plan["localization"], plan["files"][0])
            selected = plan["files"]
            dependency_change = None
            regenerate_test = False
        else:
            last_plan_error = None
            for plan_attempt in range(3):
                plan = self._parse_json(self._call_repair(plan_prompt))
                try:
                    selected = plan.get("files") if isinstance(plan, dict) else None
                    dependency_change = plan.get("dependencies") if isinstance(plan, dict) else None
                    regenerate_test = plan.get("regenerate_test", False) if isinstance(plan, dict) else False
                    if (not isinstance(selected, list) or len(selected) > 2
                            or not all(isinstance(path, str) and path in by_path for path in selected)
                            or len(set(selected)) != len(selected)):
                        raise ValueError("files must be a unique list of at most two existing paths")
                    # Models commonly emit [] to mean no dependency change. A list
                    # of requirement strings is also safe to normalize deterministically.
                    if isinstance(dependency_change, list):
                        if not all(isinstance(item, str) for item in dependency_change):
                            raise ValueError("dependencies list must contain only strings")
                        dependency_change = "\n".join(item.strip() for item in dependency_change if item.strip()) or None
                    if dependency_change is not None and not isinstance(dependency_change, str):
                        raise ValueError("dependencies must be text, a string list, or null")
                    if not isinstance(regenerate_test, bool):
                        raise ValueError("regenerate_test must be true or false")
                    install_enabled = failure.get("execution_policy", {}).get("install_dependencies")
                    if install_enabled is False and dependency_change is not None:
                        raise ValueError("dependency installation is disabled; repair source files instead")
                    if dependency_change is not None:
                        validate_dependencies(dependency_change, self.allowed_dependencies)
                    if not selected and dependency_change is None and not regenerate_test:
                        raise ValueError("repair plan made no changes")
                    break
                except (KeyError, TypeError, ValueError) as exc:
                    last_plan_error = exc
                    if plan_attempt == 2:
                        raise ValueError(f"Invalid automatic repair plan: {exc}") from exc
                    plan_prompt += (f"\nYour previous plan was invalid: {exc}. "
                                    "Return a corrected JSON object matching the schema exactly.")
            else:
                raise ValueError(f"Invalid automatic repair plan: {last_plan_error}")

        changed = []
        before_hashes = {
            path: hashlib.sha256(by_path[path]["content"].encode("utf-8")).hexdigest()
            for path in selected
        }
        for path in selected:
            info = by_path[path]
            extension = Path(path).suffix.lower()
            language = {".py":"python", ".yaml":"yaml", ".yml":"yaml", ".json":"json",
                        ".md":"markdown", ".txt":"text", ".toml":"toml"}.get(extension, "text")
            prompt = f"""Repair the COMPLETE {language} file {path} using the execution failure and project source below.
Return only the complete replacement content, optionally enclosed in one {language} fence.
Preserve public interfaces unless the failure proves an interface is inconsistent; keep all callers consistent.
Do not hard-code expected outputs or metrics, disable assertions, catch-and-ignore failures, or remove experiment steps.
Required emitted numeric keys: {json.dumps(self.required_metric_keys, ensure_ascii=False)}.
These must be numeric measurements, not method-name strings or renamed aliases. In particular,
proposed_primary and baseline_primary must hold their measured scores, and improvement_delta must
equal proposed_primary - baseline_primary. Retain the existing secondary results as well.
{dependency_instruction}
Diagnosis: {plan.get('diagnosis', '')}
Execution failure: {json.dumps(failure, ensure_ascii=False)}
Current execution contract: {json.dumps(contract, ensure_ascii=False)}
{self._measurement_instruction(previous_output.get('experiment_contract'))}
Project interfaces: {json.dumps(interfaces(files), ensure_ascii=False)}
Project source: {json.dumps(source_context(files), ensure_ascii=False)}
Current complete file: {info['content']}"""
            for attempt in range(3):
                content = strip_outer_fence(self._call_repair(prompt), (language, "yml", "md", "text"))
                try:
                    validate_file(path, content)
                    break
                except (ValueError, SyntaxError) as exc:
                    logger.warning("[CoderAgent] invalid file %s (attempt %s/3): %s", path, attempt + 1, exc)
                    if attempt == 2:
                        raise
                    prompt += f"\nThe replacement was invalid: {exc}. Return the corrected complete file."
            if content != info["content"]:
                info["content"] = content
                changed.append(path)

        repaired = dict(previous_output)
        repaired["files"] = files
        if dependency_change is not None:
            repaired["dependencies"] = dependency_change
        if regenerate_test:
            logger.info("[CoderAgent] regenerating smoke test from repaired source")
            repair_context = json.dumps({"execution_failure": failure, "diagnosis": plan.get("diagnosis", "")}, ensure_ascii=False)
            repaired["test_snippet"] = self._generate_smoke_test(
                files, repair_context, self._dependency_instruction(), repair=True,
            )
        if (not changed and repaired["dependencies"] == previous_output["dependencies"]
                and not regenerate_test):
            raise ValueError("Automatic repair produced no effective change")
        history = list(previous_output.get("repair_history", []))
        history.append({
            "diagnosis": str(plan.get("diagnosis", "")),
            "localization": plan.get("localization", "model_plan"),
            "changed_files": changed,
            "file_hashes": {
                path: {
                    "before": before_hashes[path],
                    "after": hashlib.sha256(by_path[path]["content"].encode("utf-8")).hexdigest(),
                }
                for path in selected
            },
            "dependencies_changed": repaired["dependencies"] != previous_output["dependencies"],
            "test_regenerated": regenerate_test,
            "failure": failure,
        })
        repaired["repair_history"] = history
        self.validate_output(repaired)
        if self.memory:
            self.memory.append(stage_id, history[-1], ["CoderAgent", "automatic_repair"])
        return repaired

    def validate_output(self, output):
        super().validate_output(output)
        validate_files(output["files"], ".")
        validate_imports(output["files"], self.allowed_dependencies)
        validate_dependencies(output["dependencies"], self.allowed_dependencies)
        validate_contract(output.get("experiment_contract"), self.require_experiment_contract,
                          [item["path"] for item in output["files"]])
        entry = output["entry_point"].replace("\\", "/")
        if entry not in {f["path"].replace("\\", "/") for f in output["files"]}:
            raise ValueError("Entry point missing from generated files")
        compile(output["test_snippet"], "<generated_test>", "exec")

    @staticmethod
    def _record_invalid_draft(draft_path, kind, raw, error):
        path = draft_path.parent / "failures" / f"{draft_path.stem}_{kind}_{uuid4().hex}.json"
        atomic_json(path, {"draft": draft_path.name, "kind": kind, "raw": raw, "error": error})

    def parse_output(self, raw_text, stage_id, inputs):
        return self._parse_json(raw_text)
