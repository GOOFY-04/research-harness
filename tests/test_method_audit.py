import copy
import json

import pytest

from harness.agents.method import MethodAgent
from harness.agents.method_audit import candidate_digest, verify_record
from harness.core.io import OutputValidationError


@pytest.fixture
def candidate():
    return {
        "method_name": "Grid", "overview": "Use past residuals only", "components": [],
        "algorithm": "fallback = min(alpha_grid)\nq = quantile(scores, 1 - fallback)",
        "method_section_draft": "Choose the widest interval when no candidate is feasible.",
        "revision_response": ["Specify the fallback"],
        "invariants": [{"quantity": "alpha", "definition": "miscoverage rate in (0,1)",
                        "monotonic_effect": "smaller alpha gives a wider interval",
                        "falsification_test": "enumerate sorted residuals"}],
    }


@pytest.fixture
def criticism():
    return {"valid": False, "issues": [{"severity": "major", "invariant": "conservatism",
        "contradiction": "minimum alpha is not the most conservative fallback",
        "repair": "use maximum alpha"}]}


def verification(decision="dismissed", **overrides):
    return {"decisions": [{"issue_index": 0, "decision": decision,
        "candidate_quote": "fallback = min(alpha_grid)",
        "reason": "For alpha 0.1 and 0.2, quantile levels are 0.9 and 0.8; min alpha is wider.",
        **overrides}]}


def respond(agent, monkeypatch, replies, prompts=None):
    iterator = iter(replies)

    def call(prompt):
        if prompts is not None:
            prompts.append(prompt)
        reply = next(iterator)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    monkeypatch.setattr(agent, "_call_llm", call)


def invoke(agent, tmp_path):
    return agent.run("method_design", {"review_feedback": {"weaknesses": []}},
                     {"session_dir": str(tmp_path)})


def test_false_positive_is_dismissed_with_candidate_and_review_preserved(
        candidate, criticism, tmp_path, monkeypatch):
    agent = MethodAgent()
    respond(agent, monkeypatch, [candidate, criticism, verification()])
    output = invoke(agent, tmp_path)
    audit = output["consistency_audit"]
    assert output["algorithm"] == candidate["algorithm"]
    assert audit["valid"] is True and audit["issues"] == []
    assert audit["initial_review"] == criticism
    assert audit["verification"] == verification()["decisions"]
    assert audit["candidate_sha256"] == candidate_digest(candidate)
    verify_record(output, audit)
    changed = copy.deepcopy(output)
    changed["algorithm"] = "fallback = max(alpha_grid)"
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_record(changed, audit)


def test_initial_design_is_audited_and_retains_user_constraints(candidate, tmp_path, monkeypatch):
    agent, prompts = MethodAgent(), []
    candidate["revision_response"] = []
    respond(agent, monkeypatch, [candidate, {"valid": True, "issues": []}], prompts)
    output = agent.run("method_design", {"research_question": "Compare methods"},
                       {"session_dir": str(tmp_path), "metadata": {
                           "research_direction": "Use 50 paired seeds and hold out test samples"}})
    assert len(prompts) == 2
    assert "50 paired seeds" in prompts[0]
    assert output["consistency_audit"]["valid"] is True


def test_confirmation_timeout_resumes_only_confirmation_in_fresh_agent(
        candidate, criticism, tmp_path, monkeypatch):
    first = MethodAgent()
    respond(first, monkeypatch, [candidate, criticism, TimeoutError("provider timed out")])
    with pytest.raises(TimeoutError):
        invoke(first, tmp_path)
    saved = json.loads(next((tmp_path / ".drafts").glob("method_*.json")).read_text("utf-8"))
    assert saved["audit_state"]["initial_review"] == criticism
    resumed, prompts = MethodAgent(), []
    respond(resumed, monkeypatch, [verification()], prompts)
    assert invoke(resumed, tmp_path)["consistency_audit"]["valid"]
    assert len(prompts) == 1 and "独立复核" in prompts[0]


def test_confirmed_failure_archives_full_candidate_before_replacing_it(
        candidate, criticism, tmp_path, monkeypatch):
    agent = MethodAgent()
    respond(agent, monkeypatch, [candidate, criticism, verification("confirmed")])
    with pytest.raises(OutputValidationError) as failure:
        invoke(agent, tmp_path)
    assert failure.value.output["candidate"] == candidate
    assert failure.value.output["consistency_audit"]["valid"] is False
    archived = json.loads(next((tmp_path / ".audits").glob("method_*.json")).read_text("utf-8"))
    assert archived["candidate"] == candidate
    assert archived["audit"]["initial_review"] == criticism
    assert not list((tmp_path / ".drafts").glob("method_*.json"))
    assert list((tmp_path / ".drafts").glob("audit_feedback_*.json"))


@pytest.mark.parametrize("bad", [
    {"valid": True, "issues": [{"severity": "major", "invariant": "x",
       "contradiction": "x", "repair": "x"}]},
    {"valid": False, "issues": []},
    {"valid": True, "issues": [{"severity": "unknown"}]},
    {"valid": "true", "issues": []},
    {"valid": True, "issues": ["not an issue object"]},
])
def test_malformed_audit_never_discards_or_approves_candidate(candidate, bad, tmp_path, monkeypatch):
    agent = MethodAgent()
    respond(agent, monkeypatch, [candidate, bad])
    with pytest.raises(ValueError, match="candidate retained"):
        invoke(agent, tmp_path)
    assert list((tmp_path / ".drafts").glob("method_*.json"))
    assert not list((tmp_path / ".drafts").glob("audit_feedback_*.json"))
    resumed, prompts = MethodAgent(), []
    respond(resumed, monkeypatch, [{"valid": True, "issues": []}], prompts)
    assert invoke(resumed, tmp_path)["consistency_audit"]["valid"]
    assert len(prompts) == 1 and "一致性审计员" in prompts[0]


@pytest.mark.parametrize("response", [
    {"decisions": []}, verification(issue_index=1), verification(issue_index=True),
    verification(candidate_quote="invented source text"), verification(reason=""),
    verification("unresolved"),
    {"decisions": verification()["decisions"] * 2},
])
def test_unverified_criticisms_never_trigger_regeneration_or_acceptance(
        candidate, criticism, response, tmp_path, monkeypatch):
    agent = MethodAgent()
    respond(agent, monkeypatch, [candidate, criticism, response])
    with pytest.raises(ValueError, match="candidate retained"):
        invoke(agent, tmp_path)
    assert list((tmp_path / ".drafts").glob("method_*.json"))
    assert not list((tmp_path / ".drafts").glob("audit_feedback_*.json"))
    resumed, prompts = MethodAgent(), []
    respond(resumed, monkeypatch, [verification()], prompts)
    assert invoke(resumed, tmp_path)["consistency_audit"]["valid"]
    assert len(prompts) == 1 and "独立复核" in prompts[0]
