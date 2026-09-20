"""Write an evidence-grounded draft with deterministic source bibliography."""
import json
import logging
import math
import re
from harness.core.agent import BaseAgent
from harness.core.io import strip_outer_fence


def latex_escape(value):
    replacements = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
                    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
                    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(replacements.get(c, c) for c in str(value))


def bibliography(sources):
    """Use retrieved metadata rather than model-invented bibliographic records."""
    records, entries = [], []
    for i, source in enumerate(sources, 1):
        key = f"source{i}"
        title = source.get("title", "").strip()
        identifier = source.get("arxiv_id", "")
        if not title or not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z-]+/\d{7})(?:v\d+)?", identifier):
            continue
        authors = source.get("authors", [])
        if isinstance(authors, str):
            authors = [authors]
        year = source.get("published", "")[:4]
        entries.append(f"@misc{{{key},\n  title = {{{latex_escape(title)}}},\n"
                       f"  author = {{{' and '.join(latex_escape(a) for a in authors)}}},\n"
                       f"  year = {{{year if year.isdigit() else ''}}},\n"
                       f"  eprint = {{{identifier}}},\n  archivePrefix = {{arXiv}}\n}}")
        records.append({"key": key, "title": title, "abstract": source.get("abstract", ""),
                        "arxiv_id": identifier})
    return records, "\n\n".join(entries)


def validate_latex_structure(text):
    """Reject common model-output errors before marking the paper stage done."""
    if re.search(r"\\n(?:\\n|\s|$)", text):
        raise ValueError("LaTeX contains a literal \\n escape instead of a line break")
    stack = []
    for match in re.finditer(r"\\(begin|end)\{([^{}]+)\}", text):
        action, environment = match.groups()
        if action == "begin":
            stack.append(environment)
        elif not stack or stack.pop() != environment:
            raise ValueError(f"Unbalanced LaTeX environment: {environment}")
    if stack:
        raise ValueError(f"Unclosed LaTeX environment: {stack[-1]}")


def canonical_metric_facts(execution):
    """Return deterministic renderings for metrics the writer may quote."""
    metrics = execution.get("analysis", {}).get("metrics", {}) if isinstance(execution, dict) else {}
    if not isinstance(metrics, dict):
        return {}, []
    numeric, facts = {}, []
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        numeric[key] = value
        fact = f"{key} = {value:.12g}"
        if "improvement" in key.casefold() and abs(value) <= 1:
            fact += f"; as a percentage = {value * 100:.6g}%"
        facts.append(fact)
    return numeric, facts


def validate_metric_claims(text, metrics):
    """Reject inconsistent percentage conversions of a relative improvement."""
    if not isinstance(metrics, dict):
        return
    relative = metrics.get("improvement")
    if (isinstance(relative, bool) or not isinstance(relative, (int, float))
            or not math.isfinite(relative) or abs(relative) > 1):
        return
    expected = relative * 100
    pattern = re.compile(
        r"(?:relative\s+)?improvement.{0,120}?([+-]?\d+(?:\.\d+)?)\s*\\?%",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        claimed = float(match.group(1))
        tolerance = max(0.001, abs(expected) * 0.05)
        if not math.isclose(claimed, expected, rel_tol=0.05, abs_tol=tolerance):
            raise ValueError(
                f"Inconsistent improvement percentage: {claimed}%; expected about {expected:.6g}%"
            )


def strip_redundant_section_heading(text, title):
    """The deterministic template owns section headings."""
    pattern = rf"^\s*\\section\*?\{{\s*{re.escape(title)}\s*\}}\s*"
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)


def normalize_latex_fragment(text):
    """Convert common Markdown leakage into valid LaTeX."""
    return re.sub(r"`([^`\n]+)`",
                  lambda match: r"\texttt{" + latex_escape(match.group(1)) + "}", text)


_TEMPLATE = r"""\documentclass[10pt,twocolumn]{{article}}
\usepackage{{amsmath,amssymb,graphicx,booktabs}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{hyperref}}
\usepackage{{cleveref}}
\title{{{title}}}
\author{{Anonymous Authors}}
\date{{}}
\begin{{document}}
\maketitle
\begin{{abstract}}
{abstract}
\end{{abstract}}
{sections}
{references}
\end{{document}}
"""


class WriterAgent(BaseAgent):
    required_fields = {"title": str, "abstract": str, "latex_sections": dict,
                       "bibtex_entries": str, "full_paper_latex": str}
    allow_empty_fields = {"bibtex_entries"}
    # Each request produces at most a 500-word section. A smaller cap avoids
    # unnecessarily long responses from OpenAI-compatible providers.
    max_tokens = 3072

    def build_prompt(self, stage_id, inputs, state):
        return ""

    def _call_section(self, prompt):
        """Retry only the failed sub-request instead of restarting the whole paper."""
        for attempt in range(2):
            try:
                return self._call_llm(prompt)
            except (ConnectionError, OSError, RuntimeError, TimeoutError) as exc:
                message = str(exc).lower()
                transient = any(token in message for token in
                                ("timeout", "timed out", "connection", "temporarily", "unavailable"))
                if attempt or not transient:
                    raise
                logging.warning("Writer sub-request failed; retrying once: %s", exc)

    def run(self, stage_id, inputs, state):
        sources, bibtex = bibliography(inputs.get("sources", []))
        verified_metrics, metric_facts = canonical_metric_facts(inputs.get("execution", {}))
        implementation = []
        for item in inputs.get("implementation", []):
            if (isinstance(item, dict) and isinstance(item.get("path"), str)
                    and isinstance(item.get("content"), str) and item["path"].endswith(".py")):
                implementation.append({"path": item["path"], "content": item["content"]})
        context = json.dumps({**inputs, "sources": sources, "implementation": implementation,
                              "canonical_metric_facts": metric_facts},
                             ensure_ascii=False, indent=2)
        rules = """Write in English. Every factual experimental claim must be supported by the supplied execution log.
A smoke_test only validates code on synthetic inputs; it does not establish model quality.
An entry_point execution is the bounded experiment itself. Do not call its reported metrics smoke-test results.
Do not invent training runs, benchmark scores, statistical significance, published status or references.
When converting a relative metric to a percentage, use canonical_metric_facts exactly and keep the
same rounded value in every section.
Treat the method design as a proposal, not proof that every described component was implemented.
The supplied implementation source is the sole authority for claims about implemented algorithms.
When the method design and source differ, describe the source behavior and label the design-only feature TODO.
Do not claim bit-exact reproducibility unless supplied evidence compares outputs from repeated executions.
Describe proposed mechanisms as proposed; do not claim implementation details that source and execution do not verify.
Do not use words such as significant or state-of-the-art without a supplied statistical test or benchmark result.
Label all missing experiments and result tables explicitly as TODO / not yet measured.
Use only the supplied citation keys. If none exist, do not cite.
Use valid LaTeX with single backslashes; escape special characters.
Use itemize/enumerate environments for lists. Do not introduce unavailable packages or figures."""
        meta = self._parse_json(self._call_section(f"""{rules}
Research evidence: {context}
Return JSON with "title" (plain English text) and "abstract" (English LaTeX).
The abstract must distinguish verified execution from planned experiments."""))
        if meta.get("parse_error") or not all(isinstance(meta.get(k), str) and meta[k].strip()
                                             for k in ("title", "abstract")):
            raise ValueError("Invalid paper title/abstract")
        sections = {}
        for key, title in [("introduction", "Introduction"), ("related_work", "Related Work"),
                           ("method", "Method"), ("experiments", "Experiments"), ("conclusion", "Conclusion")]:
            previous = "\n".join(sections.values())
            raw = self._call_section(f"""{rules}
Research evidence: {context}
Previous sections for consistency: {previous}
Write the {title} section, 200-500 words. Return only LaTeX content, no section heading.
Use an optional latex fence around the complete response.""")
            section = strip_outer_fence(raw, ("latex", "tex"))
            section = strip_redundant_section_heading(section, title)
            section = normalize_latex_fragment(section)
            if not section.strip():
                raise ValueError(f"Empty paper section: {key}")
            sections[key] = section
        body = "\n\n".join(f"\\section{{{name}}}\n{sections[key]}" for key, name in
                            [("introduction", "Introduction"), ("related_work", "Related Work"),
                             ("method", "Method"), ("experiments", "Experiments"), ("conclusion", "Conclusion")])
        text = meta["abstract"] + body
        allowed = {s["key"] for s in sources}
        cited = {key.strip() for group in re.findall(r"\\cite(?:p|t)?\*?(?:\[[^\]]*\])*\{([^}]+)\}", text)
                 for key in group.split(",")}
        if cited - allowed:
            raise ValueError(f"Unknown citation keys: {sorted(cited - allowed)}")
        # The template uses standard LaTeX cite, not natbib.
        text_sections = {k: re.sub(r"\\cite[pt](?=[{\[])", r"\\cite", v) for k, v in sections.items()}
        body = re.sub(r"\\cite[pt](?=[{\[])", r"\\cite", body)
        abstract = re.sub(r"\\cite[pt](?=[{\[])", r"\\cite", meta["abstract"])
        full = _TEMPLATE.format(title=latex_escape(meta["title"]), abstract=abstract, sections=body,
                                references=r"\bibliographystyle{plain}\bibliography{references}" if sources else "")
        validate_latex_structure(full)
        validate_metric_claims(full, verified_metrics)
        output = {"title": meta["title"], "abstract": abstract, "latex_sections": text_sections,
                  "bibtex_entries": bibtex, "full_paper_latex": full,
                  "evidence_scope": inputs.get("execution", {}).get("execution_kind", "unknown"),
                  "verified_metrics": verified_metrics}
        self.validate_output(output)
        if self.memory:
            self.memory.append(stage_id, {"title": meta["title"], "evidence_scope": output["evidence_scope"]},
                               ["WriterAgent"])
        return output

    def parse_output(self, raw_text, stage_id, inputs):
        return self._parse_json(raw_text)

    def validate_output(self, output):
        super().validate_output(output)
        validate_latex_structure(output["full_paper_latex"])
        validate_metric_claims(output["full_paper_latex"], output.get("verified_metrics", {}))
