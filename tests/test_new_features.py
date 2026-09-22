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
from harness.agents.method import MethodAgent


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


def test_coder_regenerates_invalid_manifest_in_place(tmp_path, monkeypatch):
    agent = CoderAgent(allowed_dependencies=[])
    manifest = {"files": [{"path": "main.py", "description": "entry", "interface": "def run()"}],
                "entry_point": "main.py", "dependencies": "", "run_instructions": "python main.py"}
    replies = iter([
        "not json",
        json.dumps(manifest),
        "def run():\n    return 1\n\nprint(run())\n",
        "from main import run\nassert run() == 1\n",
    ])
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(agent, "_call_llm", respond)
    output = agent.run("coding", {}, {"session_dir": str(tmp_path)})

    assert output["entry_point"] == "main.py"
    assert "Previous manifest was invalid" in prompts[1]
    assert "without file contents" in prompts[1]


def test_reviewer_prefers_executed_source_over_early_design_hint():
    prompt = ReviewerAgent().build_prompt("self_review", {
        "research_question": "Does the method work?",
        "method": {"overview": "An early hint suggested numpy."},
        "implementation": [{"path": "main.py", "content": "import statistics\n"}],
        "dependencies": "",
    }, {"metadata": {"research_direction": "standard library only"}})

    assert "prefer this over early design hints" in prompt
    assert '"path": "main.py"' in prompt
    assert '"complete": true' in prompt
    assert "import statistics" in prompt
    assert "standard library only" in prompt
    assert '"evidence_verdict": "supported|contradicted|inconclusive|invalid"' in prompt


def test_reviewer_marks_context_truncation_without_calling_source_incomplete():
    prompt = ReviewerAgent().build_prompt("self_review", {
        "implementation": [{"path": "large.py", "content": "x" * 16000}],
    }, {})
    assert '"complete": false' in prompt
    assert '"original_chars": 16000' in prompt
    assert "不得因上下文裁剪声称源码缺失" in prompt


def test_reviewer_separates_evidence_validity_from_publication_recommendation():
    agent = ReviewerAgent()
    output = {
        "recommendation": "weak_reject",
        "evidence_verdict": "contradicted",
        "claim_scope": "The hypothesis failed on one deterministic synthetic task.",
        "weaknesses": [{"severity": "major", "category": "scope",
                        "issue": "Only one task", "suggestion": "add tasks"}],
        "revision_plan": [],
    }
    agent.validate_output(output)
    output["weaknesses"][0].pop("category")
    with pytest.raises(ValueError, match="severity and category"):
        agent.validate_output(output)


def test_method_revision_prompt_requires_concrete_review_repairs():
    prompt = MethodAgent().build_prompt("method_design", {
        "research_question": "Does it work?",
        "review_feedback": {
            "recommendation": "weak_reject",
            "weaknesses": [{"severity": "major", "issue": "invalid baseline"}],
            "revision_plan": [{"priority": "high", "action": "replace baseline"}],
            "missing_experiments": ["ablation"],
        },
        "previous_method": {"method_name": "OldMethod"},
        "previous_execution": {"analysis": {"metrics": {"score": 0.1}}},
    }, {})

    assert "这是一次审稿驱动的修订" in prompt
    assert "invalid baseline" in prompt and "replace baseline" in prompt
    assert "不得只修改措辞或隐藏负面结果" in prompt
    assert "OldMethod" in prompt and '"score": 0.1' in prompt
    assert '"invariants"' in prompt and "单调方向" in prompt

    with pytest.raises(ValueError, match="revision_response"):
        MethodAgent().parse_output(json.dumps({"method_name": "Changed"}), "method_design", {
            "review_feedback": {"recommendation": "reject"},
        })


def test_revised_method_runs_consistency_audit_before_coding(monkeypatch):
    agent = MethodAgent()
    method = {
        "method_name": "M", "overview": "O", "components": [], "algorithm": "theta += 1",
        "method_section_draft": "Draft", "revision_response": ["fixed direction"],
        "invariants": [{"quantity": "theta", "definition": "theta in [0, 1]",
                        "monotonic_effect": "larger theta narrows the interval",
                        "falsification_test": "compare widths at theta=0 and theta=1"}],
    }
    replies = iter([json.dumps(method), json.dumps({
        "valid": False, "issues": [{"severity": "critical", "invariant": "direction",
                                      "contradiction": "update sign expands instead of narrows",
                                      "repair": "reverse the sign"}],
    })])
    monkeypatch.setattr(agent, "_call_llm", lambda prompt: next(replies))
    with pytest.raises(ValueError, match="update sign"):
        agent.run("method_design", {"review_feedback": {
            "weaknesses": [{"severity": "critical", "issue": "wrong direction"}],
        }}, {})


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
