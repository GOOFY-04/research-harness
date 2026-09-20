"""Generate typed files with shared interfaces and validate before publishing."""
import ast
import hashlib
import json
import logging
from pathlib import Path
from harness.core.agent import BaseAgent
from harness.core.io import safe_path, strip_outer_fence
from harness.tools.validation import validate_file, validate_files, validate_dependencies, validate_imports

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

    def __init__(self, *args, allowed_dependencies=None, required_metric_keys=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed_dependencies = allowed_dependencies
        self.required_metric_keys = required_metric_keys or []
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

    def run(self, stage_id, inputs, state):
        context = json.dumps(inputs, ensure_ascii=False, indent=2)
        manifest = self._parse_json(self._call_llm(f"""You are implementing a research prototype.
Design 6-10 files with consistent public interfaces. Return a JSON object:
{{"files":[{{"path":"relative/path.py","description":"purpose","interface":"exact public class/function signatures"}}],
"entry_point":"train.py","dependencies":"requirements.txt text","run_instructions":"Markdown"}}
Use Python and honor the research design's dependency and resource constraints. Do not introduce
PyTorch, OpenCV, GPU code, or other heavy packages unless the supplied design explicitly requires them.
Allowed third-party dependencies: {json.dumps(self.allowed_dependencies, ensure_ascii=False)}.
When this value is not null, every declared dependency must be in that list.
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
- clearly label all results as synthetic-benchmark evidence, not publication claims.
Research design:
{context}"""))
        if manifest.get("parse_error") or not isinstance(manifest.get("files"), list) or not manifest["files"]:
            raise ValueError("Invalid code manifest")
        validate_dependencies(manifest.get("dependencies", ""), self.allowed_dependencies)
        for info in manifest["files"]:
            safe_path(state.get("session_dir", "."), info["path"])
        files = []
        for info in manifest["files"]:
            path = info["path"]
            extension = Path(path).suffix.lower()
            language = {".py":"python", ".yaml":"yaml", ".yml":"yaml", ".json":"json",
                        ".md":"markdown", ".txt":"text", ".toml":"toml"}.get(extension, "text")
            prompt = f"""Generate the COMPLETE {language} file {path}.
Return only the file content, optionally enclosed in a {language} fence.
Honor the file extension: YAML/JSON files must contain data, never Python.
Implement exactly the shared public interfaces, with all imports and complete bodies.
Do not invent alternate names or parameters. Avoid work at import time.
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
                    break
                except (ValueError, SyntaxError) as exc:
                    if attempt == 2:
                        raise
                    prompt += f"\nPrevious output was invalid: {exc}. Regenerate the complete file."
            files.append({**info, "content": content})
        validate_files(files, state.get("session_dir", "."))
        validate_imports(files, self.allowed_dependencies)
        test = strip_outer_fence(self._call_llm(f"""Write a short Python smoke test for these exact interfaces.
Use CPU and tiny synthetic inputs. Assert output shapes and finite values.
Do not download datasets or weights, train a full model, or report synthetic scores as research results.
Return only Python code.\n{json.dumps(interfaces(files), ensure_ascii=False)}
Research context: {context}"""), ("python",))
        compile(test, "<generated_test>", "exec")
        output = {"files": files, "entry_point": manifest.get("entry_point", ""),
                  "dependencies": manifest.get("dependencies", ""),
                  "run_instructions": manifest.get("run_instructions", ""), "test_snippet": test}
        self.validate_output(output)
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
        contract = {
            "entry_point": previous_output["entry_point"],
            "dependencies": previous_output["dependencies"],
            "test_snippet": previous_output["test_snippet"],
        }
        plan_prompt = f"""A generated research program failed during execution.
Diagnose the runtime or installation failure and propose the smallest repair.
Return JSON only:
{{"diagnosis":"concrete root cause","files":["existing/path.py"],
  "dependencies":null}}
"files" must contain only existing generated paths and at most six entries.
Use dependencies only when the requirements text itself must change; otherwise return null.
If execution_policy.install_dependencies is false, dependencies must be null and the source must avoid incompatible optional packages.
execution_policy.timeout_seconds is the total budget for the smoke test and entry point together, including all baselines and evaluation.
When execution timed out, reduce bounded workload or split the total budget; do not merely add tolerance to an internal timer.
Do not weaken or delete tests, remove the baseline, suppress errors, hard-code metrics, or skip the experiment.
The supplied test snippet is an immutable interface contract. Repair source files to satisfy it.
Execution failure:
{json.dumps(failure, ensure_ascii=False, indent=2)}
Execution contract:
{json.dumps(contract, ensure_ascii=False, indent=2)}
Current interfaces:
{json.dumps(interfaces(files), ensure_ascii=False, indent=2)}
Current source:
{json.dumps(sources, ensure_ascii=False, indent=2)}"""
        last_plan_error = None
        for plan_attempt in range(3):
            plan = self._parse_json(self._call_repair(plan_prompt))
            try:
                selected = plan.get("files") if isinstance(plan, dict) else None
                dependency_change = plan.get("dependencies") if isinstance(plan, dict) else None
                if (not isinstance(selected, list) or len(selected) > 6
                        or not all(isinstance(path, str) and path in by_path for path in selected)
                        or len(set(selected)) != len(selected)):
                    raise ValueError("files must be a unique list of at most six existing paths")
                # Models commonly emit [] to mean no dependency change. A list
                # of requirement strings is also safe to normalize deterministically.
                if isinstance(dependency_change, list):
                    if not all(isinstance(item, str) for item in dependency_change):
                        raise ValueError("dependencies list must contain only strings")
                    dependency_change = "\n".join(item.strip() for item in dependency_change if item.strip()) or None
                if dependency_change is not None and not isinstance(dependency_change, str):
                    raise ValueError("dependencies must be text, a string list, or null")
                install_enabled = failure.get("execution_policy", {}).get("install_dependencies")
                if install_enabled is False and dependency_change is not None:
                    raise ValueError("dependency installation is disabled; repair source files instead")
                if dependency_change is not None:
                    validate_dependencies(dependency_change, self.allowed_dependencies)
                if not selected and dependency_change is None:
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
Diagnosis: {plan.get('diagnosis', '')}
Execution failure: {json.dumps(failure, ensure_ascii=False)}
Immutable execution contract: {json.dumps(contract, ensure_ascii=False)}
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
        if not changed and repaired["dependencies"] == previous_output["dependencies"]:
            raise ValueError("Automatic repair produced no effective change")
        history = list(previous_output.get("repair_history", []))
        history.append({
            "diagnosis": str(plan.get("diagnosis", "")),
            "changed_files": changed,
            "file_hashes": {
                path: {
                    "before": before_hashes[path],
                    "after": hashlib.sha256(by_path[path]["content"].encode("utf-8")).hexdigest(),
                }
                for path in selected
            },
            "dependencies_changed": repaired["dependencies"] != previous_output["dependencies"],
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
