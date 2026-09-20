"""Check installed distribution versions against PEP 508 requirements."""
from importlib import metadata
from harness.core.skill import Skill
from harness.tools.validation import validate_dependencies


class DependencyCheckSkill(Skill):
    name = "dependency_check"
    description = "Check installed distributions and version constraints (not a vulnerability audit)"

    def validate_inputs(self, inputs):
        return isinstance(inputs.get("dependencies"), str)

    def execute(self, inputs):
        requirements = validate_dependencies(inputs["dependencies"])
        missing, conflicts, skipped = [], [], []
        for req in requirements:
            if req.marker and not req.marker.evaluate():
                skipped.append(req.name)
                continue
            try:
                version = metadata.version(req.name)
            except metadata.PackageNotFoundError:
                missing.append(req.name)
                continue
            if req.specifier and not req.specifier.contains(version, prereleases=True):
                conflicts.append({"package": req.name, "installed": version, "required": str(req.specifier)})
        return {"success": True, "satisfied": not missing and not conflicts,
                "total_packages": len(requirements), "not_found": missing, "conflicts": conflicts,
                "skipped": skipped, "outdated": None, "security_issues": None,
                "checks_performed": ["installed_versions", "requested_constraints"],
                "summary": f"{len(requirements)} requirements; {len(missing)} missing; {len(conflicts)} version conflicts. Vulnerabilities not checked."}
