"""Static checks for generated files; never executes generated code."""
import ast
import json
import math
import sys
from pathlib import Path
import yaml
from harness.core.io import safe_path


def validate_metric_constraints(constraints) -> dict:
    """Validate numeric min/max rules used to gate executed experiment metrics."""
    if constraints is None:
        return {}
    if not isinstance(constraints, dict):
        raise ValueError("metric_constraints must be a mapping")
    normalized = {}
    for key, rule in constraints.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(rule, dict) or not rule:
            raise ValueError("metric_constraints needs non-empty metric names and rule mappings")
        unknown = set(rule) - {"min", "max"}
        if unknown:
            raise ValueError(f"Unknown metric constraint for {key}: {', '.join(sorted(unknown))}")
        clean = {}
        for bound in ("min", "max"):
            if bound not in rule:
                continue
            value = rule[bound]
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                raise ValueError(f"Metric constraint {key}.{bound} must be finite numeric")
            clean[bound] = value
        if not clean or ("min" in clean and "max" in clean and clean["min"] > clean["max"]):
            raise ValueError(f"Invalid metric constraint bounds for {key}")
        normalized[key] = clean
    return normalized


def metric_constraint_errors(metrics: dict, constraints: dict) -> list[str]:
    errors = []
    for key, rule in constraints.items():
        if key not in metrics:
            continue
        value = metrics[key]
        if "min" in rule and value < rule["min"]:
            errors.append(f"{key}={value} is below minimum {rule['min']}")
        if "max" in rule and value > rule["max"]:
            errors.append(f"{key}={value} is above maximum {rule['max']}")
    return errors


def comparison_metric_errors(metrics: dict) -> list[str]:
    """Validate the cross-topic comparison metric contract when it is present."""
    keys = {"proposed_primary", "baseline_primary", "improvement_delta", "sample_count"}
    if not keys.issubset(metrics):
        return []
    errors = []
    proposed = metrics["proposed_primary"]
    baseline = metrics["baseline_primary"]
    delta = metrics["improvement_delta"]
    expected = proposed - baseline
    if not math.isclose(delta, expected, rel_tol=1e-6, abs_tol=1e-9):
        errors.append(
            f"improvement_delta={delta} does not equal proposed_primary-baseline_primary={expected}"
        )
    count = metrics["sample_count"]
    if count <= 0 or not float(count).is_integer():
        errors.append(f"sample_count={count} must be a positive integer")
    return errors


def validate_file(path: str, content: str) -> None:
    if not isinstance(content, str):
        raise ValueError(f"File content must be text: {path}")
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        compile(content, path, "exec")
    elif suffix in (".yaml", ".yml"):
        value = yaml.safe_load(content)
        if value is not None and not isinstance(value, (dict, list)):
            raise ValueError(f"YAML configuration must be a mapping/list: {path}")
    elif suffix == ".json":
        json.loads(content)


def validate_files(files: list[dict], base_dir: str | Path) -> None:
    if not isinstance(files, list) or not files:
        raise ValueError("No generated files")
    seen, trees = set(), {}
    for info in files:
        target = safe_path(base_dir, info["path"])
        key = target.as_posix().casefold()
        if key in seen:
            raise ValueError(f"Duplicate file: {info['path']}")
        if any(key.startswith(other + "/") or other.startswith(key + "/") for other in seen):
            raise ValueError(f"File/directory path collision: {info['path']}")
        seen.add(key)
        validate_file(info["path"], info["content"])
        path = info["path"].replace("\\", "/")
        if path.endswith(".py"):
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[:-9]
            trees[module] = ast.parse(info["content"])
    # Check direct imports against statically declared local symbols.
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module not in trees:
                continue
            target = trees[node.module]
            symbols = {n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            symbols |= {n.name for n in ast.walk(target) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            symbols |= {a.asname or a.name.split('.')[0] for n in ast.walk(target)
                        if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            if "__getattr__" in symbols:
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in symbols and f"{node.module}.{alias.name}" not in trees:
                    raise ValueError(f"{module}: {node.module} does not define {alias.name}")


def validate_imports(files: list[dict], allowed_dependencies=None) -> None:
    """Enforce an allowlist against imports as well as requirements text."""
    if allowed_dependencies is None:
        return
    from packaging.utils import canonicalize_name
    allowed = {canonicalize_name(item).replace("-", "_") for item in allowed_dependencies}
    local = set()
    for info in files:
        path = info.get("path", "").replace("\\", "/")
        if path.endswith(".py"):
            local.add(path.split("/", 1)[0].removesuffix(".py"))
    unknown = set()
    for info in files:
        if not info.get("path", "").endswith(".py"):
            continue
        tree = ast.parse(info["content"])
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".", 1)[0]]
            for name in names:
                normalized = canonicalize_name(name).replace("-", "_")
                if name not in sys.stdlib_module_names and name not in local and normalized not in allowed:
                    unknown.add(name)
    if unknown:
        raise ValueError(f"Imports are outside the configured dependency allowlist: {', '.join(sorted(unknown))}")


def validate_dependencies(text: str, allowed_dependencies=None) -> list:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    allowed = None
    if allowed_dependencies is not None:
        if not isinstance(allowed_dependencies, (list, tuple, set)):
            raise ValueError("allowed_dependencies must be a collection of package names")
        allowed = {canonicalize_name(item) for item in allowed_dependencies}
    requirements = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.url:
            raise ValueError("Direct URLs in generated dependencies are not supported")
        if allowed is not None and canonicalize_name(requirement.name) not in allowed:
            raise ValueError(f"Dependency is outside the configured allowlist: {requirement.name}")
        requirements.append(requirement)
    return requirements
