import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from harness.core.io import safe_path, strip_outer_fence, file_lock, atomic_json
from harness.tools.code_runner import write_code_files
from harness.tools.validation import validate_files, validate_dependencies, validate_imports
from harness.agents.documenter import DocumenterAgent
from harness.agents.writer import (WriterAgent, bibliography, canonical_metric_facts,
                                   validate_latex_structure, validate_metric_claims,
                                   strip_redundant_section_heading, normalize_latex_fragment)
from harness.agents.planner import PlannerAgent
from harness.agents.literature import LiteratureAgent, filter_relevant_sources
from harness.agents.coder import CoderAgent
from harness.core.agent import BaseAgent
from harness.agents.method import MethodAgent


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:\\outside.py",
    "..\\escape.py", "NUL", "folder/CON.py", "file.py:stream", "dir./x"])
def test_paths_cannot_escape(tmp_path, path):
    with pytest.raises(ValueError):
        write_code_files([{"path":path,"content":"x=1"}], tmp_path / "code")
    assert not (tmp_path / "code").exists()


def test_batch_preflight_prevents_partial_writes(tmp_path):
    with pytest.raises(ValueError):
        write_code_files([{"path":"good.py","content":"x=1"},
                          {"path":"../bad.py","content":"x=2"}], tmp_path / "code")
    assert not (tmp_path / "code").exists()


def test_static_file_and_local_import_validation(tmp_path):
    with pytest.raises(SyntaxError):
        validate_files([{"path":"bad.py","content":"def bad("}], tmp_path)
    with pytest.raises(Exception):
        validate_files([{"path":"config.yaml","content":"import yaml\nx = {}"}], tmp_path)
    with pytest.raises(ValueError, match="does not define"):
        validate_files([{"path":"a.py","content":"from b import Missing"},
                        {"path":"b.py","content":"class Actual: pass"}], tmp_path)


@pytest.mark.parametrize("text", ["--index-url https://example.com", "-r nested.txt",
                                   "pkg @ https://example.com/pkg.whl"])
def test_generated_dependency_options_rejected(text):
    with pytest.raises(ValueError):
        validate_dependencies(text)


def test_generated_dependencies_honor_allowlist():
    assert validate_dependencies("numpy>=2", ["numpy"])
    with pytest.raises(ValueError, match="allowlist"):
        validate_dependencies("torch", ["numpy"])


def test_generated_imports_honor_dependency_allowlist():
    validate_imports([{"path": "main.py", "content": "import json\nimport numpy as np"}], ["numpy"])
    with pytest.raises(ValueError, match="torch"):
        validate_imports([{"path": "main.py", "content": "import torch"}], ["numpy"])
    validate_imports([
        {"path": "pkg/model.py", "content": "VALUE = 1"},
        {"path": "main.py", "content": "from pkg.model import VALUE"},
    ], [])


def test_markdown_is_not_cut_at_inner_fences(monkeypatch):
    markdown = "# Title\n\nIntro\n\n```bash\npip install -r requirements.txt\n```\n\n## Usage\nDetails"
    agent = DocumenterAgent()
    monkeypatch.setattr(agent, "_call_llm", lambda prompt: markdown)
    assert agent.run("docs", {}, {})["readme"] == markdown
    assert strip_outer_fence("```markdown\n" + markdown + "\n```", ("markdown",)) == markdown


def test_documenter_includes_metrics_and_forbids_invented_metadata(monkeypatch):
    prompt = {}
    agent = DocumenterAgent()
    def call(value):
        prompt["value"] = value
        return "# Grounded README"
    monkeypatch.setattr(agent, "_call_llm", call)
    result = agent.run("docs", {"execution_summary": {
        "success": True, "metrics": {"accuracy_delta": -0.335}
    }}, {})
    assert result["readme"] == "# Grounded README"
    assert '"accuracy_delta": -0.335' in prompt["value"]
    assert "Do not invent repository URLs" in prompt["value"]
    assert "Do not add Citation or License sections" in prompt["value"]


def test_json_root_must_be_object():
    assert BaseAgent._parse_json('[{"a":1}]')["parse_error"]
    assert BaseAgent._parse_json("null")["parse_error"]
    assert BaseAgent._parse_json('```json\n{"a":1}\n```') == {"a":1}


def test_budget_validation_and_truncation():
    with pytest.raises(ValueError):
        MethodAgent(max_tokens=8192, thinking_budget=10000)
    agent = MethodAgent()
    assert agent.max_tokens > agent.thinking_budget
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs:
        SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"x":')])))
    agent = MethodAgent(client=fake)
    with pytest.raises(ValueError, match="Incomplete"):
        agent._call_llm("test")


def test_bibliography_uses_retrieved_sources_and_bib_file(monkeypatch):
    source = {"title":"A & B","authors":["Alice"],"published":"2024-01-01",
              "arxiv_id":"2401.12345","abstract":"An abstract."}
    records, bib = bibliography([source])
    assert records[0]["key"] == "source1"
    assert "A \\& B" in bib
    agent = WriterAgent()
    responses = iter([json.dumps({"title":"A & B","abstract":"No full experiments yet."})] +
                     [r"Evidence not yet measured. \cite{source1}"] * 5)
    monkeypatch.setattr(agent, "_call_llm", lambda prompt: next(responses))
    result = agent.run("paper", {"sources":[source],"execution":{"execution_kind":"smoke_test"}}, {})
    assert r"\bibliography{references}" in result["full_paper_latex"]
    assert "@misc" not in result["full_paper_latex"]
    assert "@misc{source1" in result["bibtex_entries"]
    assert r"\title{A \& B}" in result["full_paper_latex"]


def test_latex_structure_rejects_literal_newlines_and_unbalanced_environments():
    with pytest.raises(ValueError, match="literal"):
        validate_latex_structure(r"Abstract text. \n\nNext paragraph.")
    with pytest.raises(ValueError, match="Unbalanced"):
        validate_latex_structure(r"\begin{itemize}\item x\end{enumerate}")
    validate_latex_structure(r"\begin{itemize}\item x\end{itemize}")


def test_writer_rejects_inconsistent_relative_improvement_percentage():
    metrics, facts = canonical_metric_facts({"analysis": {"metrics": {"improvement": 0.000691}}})
    assert "0.0691%" in facts[0]
    validate_metric_claims(r"relative improvement was 0.069\%", metrics)
    with pytest.raises(ValueError, match="Inconsistent improvement percentage"):
        validate_metric_claims(r"relative improvement was 0.007\%", metrics)


def test_writer_cached_output_rechecks_latex_structure():
    agent = WriterAgent()
    with pytest.raises(ValueError, match="Unbalanced"):
        agent.validate_output({
            "title": "Title", "abstract": "Abstract", "latex_sections": {},
            "bibtex_entries": "", "full_paper_latex": r"\begin{itemize}\end{document}",
        })


def test_writer_rejects_unknown_citations(monkeypatch):
    agent = WriterAgent()
    responses = iter(['{"title":"Title","abstract":"Abstract"}'] + [r"\cite{invented}"]*5)
    monkeypatch.setattr(agent, "_call_llm", lambda prompt: next(responses))
    with pytest.raises(ValueError, match="Unknown citation"):
        agent.run("paper", {"sources":[]}, {})


def test_writer_retries_only_a_transient_subrequest(monkeypatch):
    agent = WriterAgent()
    replies = iter(['{"title":"Title","abstract":"Abstract"}'] + ["Evidence pending."] * 5)
    calls = {"count": 0}
    prompts = []
    def call(prompt):
        calls["count"] += 1
        prompts.append(prompt)
        if calls["count"] == 1:
            raise RuntimeError("LLM API connection failed: timed out")
        return next(replies)
    monkeypatch.setattr(agent, "_call_llm", call)
    result = agent.run("paper", {"sources":[]}, {})
    assert result["title"] == "Title"
    assert calls["count"] == 7


def test_writer_prompt_contains_implementation_as_source_of_truth(monkeypatch):
    agent = WriterAgent()
    prompts = []
    replies = iter(['{"title":"Title","abstract":"Abstract"}'] + ["Evidence pending."] * 5)
    def call(prompt):
        prompts.append(prompt)
        return next(replies)
    monkeypatch.setattr(agent, "_call_llm", call)
    agent.run("paper", {"sources": [], "implementation": [
        {"path": "model.py", "content": "class ActualModel: pass"},
        {"path": "notes.md", "content": "unsupported"},
    ]}, {})
    assert "class ActualModel: pass" in prompts[0]
    assert "unsupported" not in prompts[0]
    assert "sole authority" in prompts[0]


def test_planner_rejects_non_english_search_keywords():
    agent = PlannerAgent()
    raw = json.dumps({"research_question": "Q", "novelty_hypothesis": "N",
                      "keywords": ["标签噪声"]}, ensure_ascii=False)
    with pytest.raises(ValueError, match="English ASCII"):
        agent.validate_output(agent.parse_output(raw, "planning", {}))


def test_literature_rejects_empty_arxiv_results(monkeypatch):
    agent = LiteratureAgent()
    monkeypatch.setattr("harness.agents.literature.search_arxiv", lambda **kwargs: [])
    with pytest.raises(RuntimeError, match="no papers"):
        agent.build_prompt("literature", {"keywords": ["label noise", "tabular"]}, {})


def test_literature_filters_obvious_cross_domain_search_hits():
    results = [
        {"title": "Continuous-time quantum error correction", "abstract": "quantum error",
         "categories": ["quant-ph"]},
        {"title": "Multi-view camera pose recovery", "abstract": "SfM reprojection error",
         "categories": ["cs.CV"]},
    ]
    kept = filter_relevant_sources(results, ["SfM error analysis on multi-view camera images"])
    assert [item["title"] for item in kept] == ["Multi-view camera pose recovery"]


def test_literature_uses_verified_session_cache_on_transient_failure(tmp_path, monkeypatch):
    source = {"title": "Multi-view camera pose recovery", "abstract": "SfM reprojection error",
              "categories": ["cs.CV"], "arxiv_id": "2401.12345"}
    (tmp_path / "literature_sources.json").write_text(json.dumps([source]), encoding="utf-8")
    monkeypatch.setattr("harness.agents.literature.search_arxiv",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("timed out")))
    agent = LiteratureAgent()
    prompt = agent.build_prompt(
        "literature", {"keywords": ["SfM error analysis on multi-view camera images"]},
        {"session_dir": str(tmp_path)})
    assert "此前缓存" in prompt
    assert agent._sources == [source]


def test_template_removes_duplicate_section_heading():
    assert strip_redundant_section_heading(
        "\\section{Introduction}\nBody", "Introduction") == "Body"


def test_markdown_inline_code_is_normalized_to_latex():
    assert normalize_latex_fragment("Use `scm_sampler.py`.") == (
        r"Use \texttt{scm\_sampler.py}.")


def test_coder_generates_yaml_and_checks_interfaces(tmp_path, monkeypatch):
    agent = CoderAgent()
    manifest = {"files":[{"path":"model.py","interface":"def forward(x)","description":"model"},
                         {"path":"config.yaml","description":"config"},
                         {"path":"train.py","description":"entry"}],
                "entry_point":"train.py","dependencies":"","run_instructions":"python train.py"}
    responses = iter([json.dumps(manifest), "def forward(x): return x",
                      "batch_size: 2", "from model import forward\nprint(forward(1))",
                      "from model import forward\nassert forward(1) == 1"])
    prompts = []
    def call(prompt):
        prompts.append(prompt)
        return next(responses)
    monkeypatch.setattr(agent, "_call_llm", call)
    result = agent.run("coding", {}, {"session_dir":str(tmp_path)})
    assert result["files"][1]["content"] == "batch_size: 2"
    assert "fixed, equal update counts" in prompts[0]
    assert "COMPLETE yaml file config.yaml" in prompts[2]
    assert "never use a wall-clock while-loop" in prompts[3]
    assert "def forward(x)" in prompts[3]


def test_coder_repairs_files_from_executor_feedback(tmp_path, monkeypatch):
    agent = CoderAgent()
    previous = {
        "files": [
            {"path": "model.py", "content": "def value():\n    return missing_name\n"},
            {"path": "main.py", "content": "from model import value\nprint(value())\n"},
        ],
        "entry_point": "main.py",
        "dependencies": "",
        "run_instructions": "python main.py",
        "test_snippet": "from model import value\nassert value() == 3",
    }
    replies = iter([
        json.dumps({"diagnosis": "undefined runtime name", "files": ["model.py"],
                    "dependencies": []}),
        "def value():\n    return 3\n",
    ])
    calls = {"count": 0}
    prompts = []
    def call(prompt):
        calls["count"] += 1
        prompts.append(prompt)
        if calls["count"] == 1:
            raise TimeoutError("read operation timed out")
        return next(replies)
    monkeypatch.setattr(agent, "_call_llm", call)
    failure = {"success": False, "error": "Generated program failed", "execution_kind": "smoke_test",
               "runs": [{"command": ["python", "test.py"], "returncode": 1,
                         "stdout": "", "stderr": "NameError: missing_name"}]}
    repaired = agent.repair("coding", previous, failure, {"session_dir": str(tmp_path)})
    assert repaired["files"][0]["content"].endswith("return 3")
    assert repaired["repair_history"][0]["changed_files"] == ["model.py"]
    assert (repaired["repair_history"][0]["file_hashes"]["model.py"]["before"]
            != repaired["repair_history"][0]["file_hashes"]["model.py"]["after"])
    assert "NameError" in repaired["repair_history"][0]["failure"]["runs"][0]["stderr"]
    assert repaired["dependencies"] == "" and calls["count"] == 3
    assert "assert value() == 3" in prompts[0]


def test_coder_regenerates_smoke_test_when_it_guessed_the_wrong_contract(tmp_path, monkeypatch):
    agent = CoderAgent()
    previous = {
        "files": [{"path": "model.py", "content": "def interval():\n    return (0.0, 1.0)\n"}],
        "entry_point": "model.py",
        "dependencies": "",
        "run_instructions": "python model.py",
        "test_snippet": "from model import interval\nassert isinstance(interval(), float)",
    }
    replies = iter([
        json.dumps({"diagnosis": "test guessed a scalar return", "files": [],
                    "dependencies": None, "regenerate_test": True}),
        "from model import interval\nvalue = interval()\nassert len(value) == 2",
    ])
    monkeypatch.setattr(agent, "_call_llm", lambda _prompt: next(replies))
    failure = {"success": False, "error": "smoke test failed", "execution_kind": "smoke_test",
               "runs": [{"returncode": 1, "stdout": "expected float", "stderr": ""}]}

    repaired = agent.repair("coding", previous, failure, {"session_dir": str(tmp_path)})

    assert repaired["files"] == previous["files"]
    assert "len(value) == 2" in repaired["test_snippet"]
    assert repaired["repair_history"][0]["test_regenerated"] is True
    assert repaired["repair_history"][0]["changed_files"] == []


def test_session_file_lock(tmp_path):
    with file_lock(tmp_path / "session.lock"):
        with pytest.raises(RuntimeError):
            with file_lock(tmp_path / "session.lock"):
                pass
    with file_lock(tmp_path / "session.lock"):
        pass


def test_atomic_json_retries_transient_replace_permission_error(tmp_path, monkeypatch):
    import os

    real_replace = os.replace
    attempts = {"count": 0}

    def transient_replace(source, destination):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError("temporarily held by indexer")
        return real_replace(source, destination)

    monkeypatch.setattr("harness.core.io.os.replace", transient_replace)
    monkeypatch.setattr("harness.core.io.sleep", lambda _seconds: None)
    path = tmp_path / "checkpoint.json"

    atomic_json(path, {"status": "complete"})

    assert attempts["count"] == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "complete"}


def test_file_directory_collision_is_rejected_before_write(tmp_path):
    with pytest.raises(ValueError, match="collision"):
        write_code_files([{"path":"parent.py","content":"x=1"},
                          {"path":"parent.py/child.py","content":"x=2"}], tmp_path/"code")
    assert not (tmp_path/"code").exists()


def test_generated_latex_and_bibliography_compile(tmp_path, monkeypatch):
    import shutil
    from harness.tools.process import run_command
    latex, bibtex = shutil.which("pdflatex"), shutil.which("bibtex")
    if not latex or not bibtex:
        pytest.skip("Optional TeX toolchain not installed")
    agent = WriterAgent()
    replies = iter(['{"title":"A & B: 50%","abstract":"Benchmark evaluation is pending."}'] +
                   [r"A referenced source. \cite{source1}"] * 5)
    monkeypatch.setattr(agent, "_call_llm", lambda prompt: next(replies))
    source = {"title":"Identity & Evaluation","authors":["Alice Smith"],
              "published":"2024-01-01","arxiv_id":"2401.12345"}
    result = agent.run("paper", {"sources":[source]}, {})
    (tmp_path/"paper.tex").write_text(result["full_paper_latex"], encoding="utf-8")
    (tmp_path/"references.bib").write_text(result["bibtex_entries"], encoding="utf-8")
    for command in ([latex,"-interaction=nonstopmode","-halt-on-error","paper.tex"],
                    [bibtex,"paper"],
                    [latex,"-interaction=nonstopmode","-halt-on-error","paper.tex"],
                    [latex,"-interaction=nonstopmode","-halt-on-error","paper.tex"]):
        run = run_command(command, tmp_path, 30)
        assert run["success"], run["stdout"] + run["stderr"]
    assert (tmp_path/"paper.pdf").stat().st_size > 0
    assert "undefined" not in (tmp_path/"paper.log").read_text(encoding="utf-8", errors="replace").lower()
