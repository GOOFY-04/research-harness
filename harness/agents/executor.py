"""Execute generated code in a fresh source directory and a session virtualenv."""
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from time import monotonic

from harness.core.agent import BaseAgent
from harness.core.io import safe_path
from harness.core.skill import get_global_registry
from harness.tools.code_runner import write_code_files
from harness.tools.process import run_command
from harness.tools.validation import (metric_constraint_errors, validate_dependencies,
                                      validate_files, validate_imports,
                                      validate_metric_constraints)

logger = logging.getLogger(__name__)


class ExecutorAgent(BaseAgent):
    required_fields = {"success": bool, "analysis": dict, "execution_kind": str}

    def __init__(self, *args, timeout=600, install_dependencies=True,
                 run_entry_point=False, entry_args=None, enable_code_review=False,
                 require_metrics=False, required_metric_keys=None, python_executable=None,
                 allowed_dependencies=None, metric_constraints=None, **kwargs):
        super().__init__(*args, **kwargs)
        if timeout <= 0:
            raise ValueError("Executor timeout must be positive")
        self.timeout = timeout
        self.install_dependencies = install_dependencies
        self.run_entry_point = run_entry_point
        self.entry_args = entry_args or []
        if not isinstance(self.entry_args, list) or not all(isinstance(x, str) for x in self.entry_args):
            raise ValueError("entry_args must be a list of strings")
        self.enable_code_review = enable_code_review
        self.require_metrics = bool(require_metrics)
        self.required_metric_keys = required_metric_keys or []
        self.metric_constraints = validate_metric_constraints(metric_constraints)
        if (not isinstance(self.required_metric_keys, list)
                or not all(isinstance(item, str) and item.strip() for item in self.required_metric_keys)
                or len(set(self.required_metric_keys)) != len(self.required_metric_keys)):
            raise ValueError("required_metric_keys must be a unique list of non-empty strings")
        self.allowed_dependencies = allowed_dependencies
        requested_python = python_executable or sys.executable
        if not isinstance(requested_python, str) or not requested_python.strip():
            raise ValueError("python_executable must be a non-empty string")
        resolved_python = shutil.which(requested_python)
        if not resolved_python:
            raise ValueError(f"Python executable was not found: {requested_python}")
        self.python_executable = str(Path(resolved_python).resolve())

    def build_prompt(self, stage_id, inputs, state):
        return ""

    def parse_output(self, raw_text, stage_id, inputs):
        return self._parse_json(raw_text)

    def run(self, stage_id, inputs, state):
        session_dir = state.get("session_dir")
        if not session_dir:
            raise ValueError("Missing session_dir")
        session = Path(session_dir).resolve()
        session.mkdir(parents=True, exist_ok=True)
        files = inputs.get("files", [])
        validate_files(files, session / "code")
        validate_imports(files, self.allowed_dependencies)
        dependencies = inputs.get("dependencies", "")
        if not isinstance(dependencies, str):
            raise ValueError("dependencies must be requirements text")
        validate_dependencies(dependencies, self.allowed_dependencies)
        test = inputs.get("test_snippet", "")
        entry = inputs.get("entry_point", "")
        if not test and not entry:
            raise ValueError("Neither a test snippet nor an entry point was supplied")
        if test:
            compile(test, "<test_snippet>", "exec")
        deadline = monotonic() + min(self.timeout, inputs.get("_timeout", self.timeout))
        def execute(command, cwd):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return {"success": False, "stdout": "", "stderr": "Execution deadline exceeded",
                        "returncode": -1, "timed_out": True, "command": command}
            return run_command(command, cwd, remaining)
        # Fresh directory prevents stale files from earlier generations affecting imports.
        code_dir = Path(tempfile.mkdtemp(prefix="execution_", dir=session))
        write_code_files(files, code_dir)
        (code_dir / "requirements.txt").write_text(dependencies, encoding="utf-8")
        python = self.python_executable
        install_log, runs = "", []
        if dependencies and self.install_dependencies:
            environment = safe_path(session, ".venv")
            python = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            if not Path(python).exists():
                setup = execute([self.python_executable, "-m", "venv", str(environment)], session)
                install_log += setup["stdout"] + setup["stderr"]
                if not setup["success"]:
                    return self._report(False, code_dir, install_log, [], "environment", "Virtualenv creation failed")
            install = execute([python, "-m", "pip", "install", "--disable-pip-version-check",
                               "-r", str(code_dir / "requirements.txt")], code_dir)
            install_log += install["stdout"] + install["stderr"]
            if not install["success"]:
                return self._report(False, code_dir, install_log, [], "installation", "Dependency installation failed")
        if test:
            # Reserve a name instead of overwriting a generated file.
            test_path = code_dir / "_harness_quick_test.py"
            if test_path.exists():
                raise ValueError("Generated files use reserved _harness_quick_test.py")
            test_path.write_text(test, encoding="utf-8")
            runs.append(execute([python, str(test_path)], code_dir))
        kind = "smoke_test" if test else "entry_point"
        if (self.run_entry_point or not test) and (not runs or runs[-1]["success"]):
            if not entry:
                raise ValueError("run_entry_point requires an entry_point")
            entry_path = safe_path(code_dir, entry)
            if not entry_path.is_file() or entry_path.suffix != ".py":
                raise ValueError("entry_point must name an existing Python file")
            runs.append(execute([python, str(entry_path), *self.entry_args], code_dir))
            kind = "entry_point"
        success = bool(runs) and all(run["success"] for run in runs)
        output = self._report(success, code_dir, install_log, runs, kind,
                              "" if success else "Generated program failed or timed out")
        if (output["success"] and kind == "entry_point" and self.require_metrics
                and not output["analysis"]["metrics"]):
            error = "Entry point completed without a HARNESS_METRICS JSON line"
            output.update(success=False, test_success=False, error=error)
            output["analysis"].update(success=False, errors=[error])
        if output["success"] and kind == "entry_point" and self.required_metric_keys:
            missing = sorted(set(self.required_metric_keys) - set(output["analysis"]["metrics"]))
            if missing:
                error = f"HARNESS_METRICS is missing required keys: {', '.join(missing)}"
                output.update(success=False, test_success=False, error=error)
                output["analysis"].update(success=False, errors=[error])
        if output["success"] and kind == "entry_point" and self.metric_constraints:
            violations = metric_constraint_errors(output["analysis"]["metrics"], self.metric_constraints)
            if violations:
                error = "HARNESS_METRICS violates constraints: " + "; ".join(violations)
                output.update(success=False, test_success=False, error=error)
                output["analysis"].update(success=False, errors=[error])
        if self.enable_code_review and success:
            registry = getattr(self, "skill_registry", None) or get_global_registry()
            output["code_review"] = [
                {"file": f["path"], "review": registry.execute("code_review", {"code": f["content"]})}
                for f in [f for f in files if f["path"].endswith(".py")][:3]
            ]
            if any(not r["review"].get("success") for r in output["code_review"]):
                output.update(success=False, error="Code review skill failed")
        if self.memory:
            self.memory.append(stage_id, {"success": output["success"], "execution_kind": kind},
                               ["ExecutorAgent", stage_id])
        return output

    def validate_output(self, output):
        super().validate_output(output)
        if not output["success"]:
            return
        metrics = output.get("analysis", {}).get("metrics", {})
        if self.require_metrics and output.get("execution_kind") == "entry_point" and not metrics:
            raise ValueError("Cached entry point has no HARNESS_METRICS")
        missing = sorted(set(self.required_metric_keys) - set(metrics))
        if missing:
            raise ValueError(f"Cached HARNESS_METRICS is missing required keys: {', '.join(missing)}")
        violations = metric_constraint_errors(metrics, self.metric_constraints)
        if violations:
            raise ValueError("Cached HARNESS_METRICS violates constraints: " + "; ".join(violations))

    def _report(self, success, code_dir, install_log, runs, kind, error=""):
        log = "\n".join(run["stdout"] + run["stderr"] for run in runs)
        metrics = {}
        # Only accept explicitly emitted machine-readable metrics, never invent them.
        for line in log.splitlines():
            values = None
            try:
                if line.startswith("HARNESS_METRICS="):
                    values = json.loads(line.partition("=")[2])
                elif line.startswith("{"):
                    envelope = json.loads(line)
                    if isinstance(envelope, dict):
                        values = envelope.get("HARNESS_METRICS")
            except ValueError:
                continue
            if isinstance(values, dict):
                metrics.update({k: v for k, v in values.items()
                                if isinstance(v, (int, float)) and not isinstance(v, bool)})
        summary = (f"{kind}: {'passed' if success else 'failed'}. "
                   + ("Quick validation only; no full experiment has been established."
                      if kind == "smoke_test" else "See command logs for execution scope."))
        return {"success": success, "error": error, "code_dir": str(code_dir),
                "install_log": install_log, "test_log": log, "test_success": success,
                "execution_kind": kind, "runs": runs,
                "execution_policy": {
                    "install_dependencies": self.install_dependencies,
                    "run_entry_point": self.run_entry_point,
                    "require_metrics": self.require_metrics,
                    "required_metric_keys": self.required_metric_keys,
                    "metric_constraints": self.metric_constraints,
                    "python_executable": self.python_executable,
                    "timeout_seconds": self.timeout,
                },
                "analysis": {"success": success, "summary": summary, "metrics": metrics,
                             "errors": [error] if error else []}}
