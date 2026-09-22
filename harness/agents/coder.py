"""Generate typed files with shared interfaces and validate before publishing."""
import ast
import hashlib
import json
import logging
import re
from pathlib import Path
from harness.core.agent import BaseAgent
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


class CoderAgent(BaseAgent):
    required_fields = {"files": list, "entry_point": str, "dependencies": str,
                       "run_instructions": str, "test_snippet": str}

    def __init__(self, *args, allowed_dependencies=None, required_metric_keys=None,
                 metric_constraints=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_dependencies = allowed_dependencies
        self.required_metric_keys = required_metric_keys or []
        self.metric_constraints = validate_metric_constraints(metric_constraints)
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

    def _generate_smoke_test(self, files, context, dependency_instruction, repair=False):
        """Generate a smoke test from real source contracts, not guessed signatures."""
        prompt = f"""Write a short Python smoke test for this exact implementation.
Use CPU and tiny synthetic inputs. Assert only behavior supported by the supplied source,
including its real return types. Do not invent a different interface contract, fixed metric
values, downloads, training workloads, or publication claims. Prefer construction, shape,
finite-value, and one-step integration checks. Return only Python code.
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
            manifest_paths = [item["path"] for item in manifest["files"]]
            if manifest.get("entry_point") not in manifest_paths:
                return None
            if [item.get("path") for item in files] != manifest_paths[:len(files)]:
                return None
            validate_dependencies(manifest.get("dependencies", ""), self.allowed_dependencies)
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
Design 6-10 files with consistent public interfaces. Return a JSON object:
{{"files":[{{"path":"relative/path.py","description":"purpose","interface":"exact public class/function signatures"}}],
"entry_point":"train.py","dependencies":"requirements.txt text","run_instructions":"Markdown"}}
Use Python and honor the research design's dependency and resource constraints. Do not introduce
PyTorch, OpenCV, GPU code, or other heavy packages unless the supplied design explicitly requires them.
Allowed third-party dependencies: {json.dumps(self.allowed_dependencies, ensure_ascii=False)}.
When this value is not null, every declared dependency must be in that list.
{dependency_instruction}
List only PEP 508 package requirements, never pip flags or direct URLs.
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
- when proposed_primary, baseline_primary, improvement_delta and sample_count are configured, use the
  same directly observed primary metric for proposed_primary and baseline_primary, define
  improvement_delta = proposed_primary - baseline_primary (a signed raw difference, not a percentage),
  and set sample_count to the integer number of held-out evaluation cases;
- satisfy these executed-metric acceptance constraints: {json.dumps(self.metric_constraints, ensure_ascii=False)};
- clearly label all results as synthetic-benchmark evidence, not publication claims.
Research design:
{context}"""
            for manifest_attempt in range(3):
                manifest = self._parse_json(self._call_llm(manifest_prompt))
                try:
                    items = manifest.get("files")
                    if (manifest.get("parse_error") or not isinstance(items, list) or not items
                            or not all(isinstance(item, dict)
                                       and isinstance(item.get("path"), str)
                                       and isinstance(item.get("description"), str)
                                       for item in items)):
                        raise ValueError("manifest needs non-empty file objects with path and description")
                    if not isinstance(manifest.get("entry_point"), str):
                        raise ValueError("manifest needs an entry_point string")
                    if manifest["entry_point"] not in [item["path"] for item in items]:
                        raise ValueError("manifest entry_point must name one of its files")
                    validate_dependencies(manifest.get("dependencies", ""), self.allowed_dependencies)
                    for info in items:
                        safe_path(state.get("session_dir", "."), info["path"])
                    break
                except (ValueError, TypeError, KeyError) as exc:
                    if manifest_attempt == 2:
                        raise ValueError(f"Invalid code manifest: {exc}") from exc
                    logger.warning("[CoderAgent] invalid manifest; regenerating: %s", exc)
                    manifest_prompt += (f"\nPrevious manifest was invalid: {exc}. Return only the compact JSON "
                                        "manifest, without file contents or explanatory prose.")
            files = []
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
{dependency_instruction}
If this is the entry point, implement the complete deterministic experiment described in the manifest,
including baseline comparison and the HARNESS_METRICS JSON line. Use fixed iteration counts for reproducibility;
never use a wall-clock while-loop as the normal training schedule. Keep defaults bounded and download-free.
Design: {context}
Manifest: {json.dumps(manifest, ensure_ascii=False)}
Existing implemented interfaces: {json.dumps(interfaces(files), ensure_ascii=False)}
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
                  "run_instructions": manifest.get("run_instructions", ""), "test_snippet": test}
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
        plan = traceback_repair_plan(failure, by_path)
        if plan:
            logger.info("[CoderAgent] traceback localized repair to %s", plan["files"][0])
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
{dependency_instruction}
Diagnosis: {plan.get('diagnosis', '')}
Execution failure: {json.dumps(failure, ensure_ascii=False)}
Current execution contract: {json.dumps(contract, ensure_ascii=False)}
Project interfaces: {json.dumps(interfaces(files), ensure_ascii=False)}
Project source: {json.dumps(source_context(files), ensure_ascii=False)}
Current complete file: {info['content']}"""
            for attempt in range(3):
                content = strip_outer_fence(self._call_repair(prompt), (language, "yml", "md", "text"))
                try:
                    validate_file(path, content)
                    break
                except (ValueError, SyntaxError) as exc:
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
        entry = output["entry_point"].replace("\\", "/")
        if entry not in {f["path"].replace("\\", "/") for f in output["files"]}:
            raise ValueError("Entry point missing from generated files")
        compile(output["test_snippet"], "<generated_test>", "exec")

    def parse_output(self, raw_text, stage_id, inputs):
        return self._parse_json(raw_text)
