"""Offline default-workflow tests, including real generated-code subprocesses."""
import json
from argparse import Namespace
from types import SimpleNamespace
import pytest
import main
from harness.core.checkpoint import CheckpointManager
from harness.core.workflow import WorkflowEngine
from harness.agents import literature
from harness.agents.planner import PlannerAgent
from harness.core.llm import LLMClient


def test_default_eight_stage_cli_run_resume_and_reset(tmp_path, monkeypatch):
    config = main.load_config()
    config["paths"]["sessions_dir"] = str(tmp_path / "sessions")
    config["paths"]["memory_dir"] = str(tmp_path / "memory")
    config["agents"]["executor"]["install_dependencies"] = False
    source = {"title":"Real metadata fixture","authors":["A"],"published":"2024-01-01",
              "arxiv_id":"2401.12345","abstract":"Metadata for offline test."}
    monkeypatch.setattr(literature, "search_arxiv", lambda **kwargs: [source])
    real_build = main.build_agent_registry
    calls = []
    def build(config, memory):
        agents = real_build(config, memory)
        fixed = {
            "planner":{"research_question":"Does identity preserve values?", "keywords":["identity"],
                       "novelty_hypothesis":"A test hypothesis."},
            "literature":{"papers":[],"research_gaps":[],"key_baselines":[],
                          "related_work_draft":"A retrieved source."},
                "method":{"method_name":"Identity","overview":"Return the input","components":[],
                          "algorithm":"return x","method_section_draft":"Identity method.",
                          "invariants":[{"quantity":"x","definition":"input value",
                                         "monotonic_effect":"identity preserves order",
                                         "falsification_test":"assert identity(3) == 3"}]},
            "reviewer":{"recommendation":"weak_reject","evidence_verdict":"inconclusive",
                        "claim_scope":"The smoke test only establishes executable identity behavior.",
                        "weaknesses":[],"revision_plan":[]},
        }
        for name, output in fixed.items():
            monkeypatch.setattr(agents[name], "_call_llm", lambda prompt, output=output: json.dumps(output))
        manifest = {"files":[{"path":"main.py","description":"entry","interface":"def identity(x)"}],
                    "entry_point":"main.py","dependencies":"","run_instructions":"python main.py"}
        coder = iter([json.dumps(manifest), "def identity(x): return x",
                      "from main import identity\nassert identity(3) == 3"])
        monkeypatch.setattr(agents["coder"], "_call_llm", lambda prompt: next(coder))
        writer = iter(['{"title":"Identity Study","abstract":"A smoke test passed; benchmarks are pending."}']
                      + [r"Evidence pending. \cite{source1}"] * 5)
        monkeypatch.setattr(agents["writer"], "_call_llm", lambda prompt: next(writer))
        markdown = "# Identity\n\nSmoke test only.\n\n```bash\npython main.py\n```\n\n## TODO\nRun experiments."
        monkeypatch.setattr(agents["documenter"], "_call_llm", lambda prompt: markdown)
        for name, agent in agents.items():
            original = agent.run
            def tracked(stage_id, inputs, state, original=original):
                calls.append(stage_id)
                return original(stage_id, inputs, state)
            monkeypatch.setattr(agent, "run", tracked)
        return agents
    monkeypatch.setattr(main, "build_agent_registry", build)
    args = Namespace(session="integration", direction="identity", no_resume=False, workflow=None)
    assert main.cmd_run(args, config) == 0
    cp = CheckpointManager(config["paths"]["sessions_dir"], args.session)
    state = cp.load()
    assert state["status"] == "completed"
    assert calls == ["planning","literature","method_design","coding","code_execution",
                     "self_review","paper_writing","documentation"]
    assert state["stages"]["code_execution"]["output"]["success"]
    assert state["stages"]["documentation"]["output"]["readme"].endswith("Run experiments.")
    assert (cp.session_dir/"code/requirements.txt").exists()
    assert (cp.session_dir/"output/references.bib").exists()
    first_calls = len(calls)
    assert main.cmd_resume(Namespace(session="integration",workflow=None), config) == 0
    assert len(calls) == first_calls
    reset = Namespace(session="integration",workflow=None,stages=["code_execution"])
    assert main.cmd_reset_stage(reset, config) == 0
    assert cp.load()["completed_stages"] == ["planning","literature","method_design","coding"]
    assert not (cp.session_dir/"output").exists()
    assert list((cp.session_dir/"history").glob("*/output/paper.tex"))
    assert main.cmd_resume(Namespace(session="integration",workflow=None), config) == 0
    assert calls[-4:] == ["code_execution","self_review","paper_writing","documentation"]
    args = Namespace(session="integration", direction="new topic", no_resume=True, workflow=None)
    assert main.cmd_run(args, config) == 0
    assert cp.load()["metadata"]["research_direction"] == "new topic"
    assert cp.load()["stages"]["planning"]["attempts"] == 1
    assert list((cp.session_dir/"history").glob("*/checkpoint.json"))


def test_base_agent_parse_error_preserved_for_repair(tmp_path, monkeypatch):
    workflow = tmp_path/"workflow.yaml"
    workflow.write_text("name: parse\nstages:\n- id: planning\n  agent: planner\n  max_retries: 0\n")
    cp = CheckpointManager(tmp_path, "parse")
    agent = PlannerAgent()
    monkeypatch.setattr(agent, "_call_llm", lambda prompt: "unparseable")
    state = WorkflowEngine(workflow, cp, {"planner":agent}).run()
    assert state["status"] == "failed"
    assert state["stages"]["planning"]["output"] == {"parse_error":True,"raw":"unparseable"}


def test_cli_failure_has_nonzero_exit(tmp_path, monkeypatch):
    cfg = main.load_config()
    cfg["paths"]["sessions_dir"] = str(tmp_path/"sessions")
    cfg["paths"]["memory_dir"] = str(tmp_path/"memory")
    cfg["logging"]["file"] = ""
    monkeypatch.setattr(main, "load_config", lambda path: cfg)
    monkeypatch.setattr(PlannerAgent, "_call_llm", lambda *args: "invalid")
    assert main.main(["run","--direction","test","--session","failure"]) == 1
    assert CheckpointManager(cfg["paths"]["sessions_dir"],"failure").load()["status"] == "failed"


def test_llm_real_sdk_response_without_network(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    captured = {}
    # Use the real SDK message model and constructor, replacing only transport entrypoint.
    client = anthropic.Anthropic(api_key="offline-test", base_url="https://example.invalid", max_retries=0)
    response = anthropic.types.Message(id="msg_offline", type="message", role="assistant",
        model="claude-sonnet-4-6", content=[{"type":"text","text":"{\"ok\":true}"}],
        stop_reason="end_turn", stop_sequence=None,
        usage={"input_tokens":1,"output_tokens":1})
    def create(**kwargs):
        captured.update(kwargs)
        return response
    monkeypatch.setattr(client.messages, "create", create)
    llm = LLMClient(client=client, timeout=10)
    assert json.loads(llm.complete("offline")) == {"ok":True}
    assert captured["timeout"] == 10
    client.close()


def test_openai_compatible_adapter_without_network(monkeypatch):
    import harness.core.llm as module
    captured = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"choices":[{"message":{"content":"ready"},"finish_reason":"stop"}]}'
    def open_url(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()
    monkeypatch.setattr(module, "urlopen", open_url)
    client = LLMClient(api_key="secret", base_url="https://gateway.example/v1/",
                       model="model-x", protocol="openai_compatible", timeout=9)
    assert client.complete("ping") == "ready"
    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["authorization"] == "Bearer secret"
    assert captured["payload"]["model"] == "model-x"
    assert captured["payload"]["stream"] is False


def test_openai_compatible_rejects_truncated_response(monkeypatch):
    import harness.core.llm as module
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}'
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response())
    client = LLMClient(api_key="secret", protocol="openai_compatible")
    with pytest.raises(ValueError, match="Incomplete"):
        client.complete("ping")


def test_agent_selects_key_for_configured_protocol(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-provider-key")
    monkeypatch.setenv("AGNES_API_KEY", "agnes-key")
    agent = PlannerAgent(protocol="openai_compatible", api_key_env="AGNES_API_KEY",
                         model="agnes-3.0-flash", use_extended_thinking=False)
    assert agent._llm.api_key == "agnes-key"
