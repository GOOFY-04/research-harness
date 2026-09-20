"""Real assertions, isolated registries, no credentials or network."""
from types import SimpleNamespace
import pytest
from harness.core.skill import Skill, SkillRegistry
from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill
from harness.agents.executor import ExecutorAgent
from harness.agents.documenter import DocumenterAgent


def test_imports_and_instantiation_without_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert ExecutorAgent().timeout == 600
    assert DocumenterAgent()._llm._client is None
    assert CodeReviewSkill()._llm._client is None
    assert TestGenerationSkill()._llm._client is None


def test_registry_errors_and_results():
    registry = SkillRegistry()
    with pytest.raises(ValueError):
        registry.execute("missing", {})
    class Broken(Skill):
        name = "broken"
        def execute(self, inputs):
            raise RuntimeError("failure")
    registry.register(Broken())
    result = registry.execute("broken", {})
    assert result["success"] is False
    assert result["error"] == "failure"


def test_dependency_constraints(monkeypatch):
    from harness.skills import dependency_check
    monkeypatch.setattr(dependency_check.metadata, "version", lambda name: "1.0")
    result = DependencyCheckSkill().execute({"dependencies": "numpy>=2\npytest; python_version<'2'"})
    assert result["satisfied"] is False
    assert result["conflicts"] == [{"package": "numpy", "installed": "1.0", "required": ">=2"}]
    assert result["skipped"] == ["pytest"]
    assert result["security_issues"] is None


def test_dependency_missing(monkeypatch):
    from harness.skills import dependency_check
    def missing(name):
        raise dependency_check.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(dependency_check.metadata, "version", missing)
    result = DependencyCheckSkill().execute({"dependencies": "absent==1"})
    assert result["not_found"] == ["absent"]


def test_test_generation_rejects_invalid_python(monkeypatch):
    skill = TestGenerationSkill()
    monkeypatch.setattr(skill._llm, "create", lambda **kwargs: "def broken(")
    assert skill.execute({"code": "x=1"})["success"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
