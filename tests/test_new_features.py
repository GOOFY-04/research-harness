"""Real assertions, isolated registries, no credentials or network."""
import json
from types import SimpleNamespace
import pytest
from harness.agents.coder import CoderAgent
from harness.core.skill import Skill, SkillRegistry
from harness.skills import CodeReviewSkill, DependencyCheckSkill, TestGenerationSkill
from harness.agents.executor import ExecutorAgent
from harness.agents.documenter import DocumenterAgent
from harness.agents.reviewer import ReviewerAgent


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


def test_coder_repeats_stdlib_policy_for_each_generation_request(tmp_path, monkeypatch):
    agent = CoderAgent(allowed_dependencies=[])
    manifest = {
        "files": [{"path": "main.py", "description": "entry", "interface": "def run()"}],
        "entry_point": "main.py",
        "dependencies": "",
        "run_instructions": "python main.py",
    }
    replies = iter([
        json.dumps(manifest),
        "def run():\n    return 1\n",
        "from main import run\nassert run() == 1\n",
    ])
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(agent, "_call_llm", respond)
    agent.run("coding", {}, {
        "session_dir": str(tmp_path),
        "metadata": {"research_direction": "use at least 600 observations"},
    })

    policy = "use only the Python standard library"
    assert len(prompts) == 3
    assert all(policy in prompt for prompt in prompts)
    assert all("use at least 600 observations" in prompt for prompt in prompts)


def test_coder_regenerates_file_with_disallowed_import(tmp_path, monkeypatch):
    agent = CoderAgent(allowed_dependencies=[])
    manifest = {
        "files": [{"path": "main.py", "description": "entry", "interface": "def run()"}],
        "entry_point": "main.py",
        "dependencies": "",
        "run_instructions": "python main.py",
    }
    replies = iter([
        json.dumps(manifest),
        "import numpy\ndef run():\n    return numpy.array([1])\n",
        "def run():\n    return [1]\n",
        "from main import run\nassert run() == [1]\n",
    ])
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(agent, "_call_llm", respond)
    output = agent.run("coding", {}, {"session_dir": str(tmp_path)})

    assert output["files"][0]["content"].startswith("def run")
    assert "outside the configured dependency allowlist: numpy" in prompts[2]


def test_reviewer_prefers_executed_source_over_early_design_hint():
    prompt = ReviewerAgent().build_prompt("self_review", {
        "research_question": "Does the method work?",
        "method": {"overview": "An early hint suggested numpy."},
        "implementation": [{"path": "main.py", "content": "import statistics\n"}],
        "dependencies": "",
    }, {"metadata": {"research_direction": "standard library only"}})

    assert "prefer this over early design hints" in prompt
    assert '"path": "main.py"' in prompt
    assert "import statistics" in prompt
    assert "standard library only" in prompt


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
