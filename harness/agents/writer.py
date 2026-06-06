"""
WriterAgent — 论文撰写（逐节生成，避免单次输出截断）

策略：
  第1轮：生成标题、摘要、关键词
  第2-6轮：逐节生成（Introduction / Related Work / Method / Experiments / Conclusion）
  第7轮：生成 BibTeX
  Python 侧拼装完整 LaTeX 文档
输出：
  - title, abstract
  - latex_sections: {introduction, related_work, method, experiments, conclusion}
  - bibtex_entries
  - full_paper_latex
"""

import json
import logging
import re

from harness.core.agent import BaseAgent

logger = logging.getLogger(__name__)

_LATEX_TEMPLATE = r"""\documentclass[10pt,twocolumn]{{article}}
\usepackage{{times}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
\usepackage{{booktabs}}
\usepackage{{hyperref}}
\usepackage{{cleveref}}
\usepackage{{xcolor}}
\usepackage[margin=1in]{{geometry}}

\title{{{title}}}
\author{{Anonymous Authors}}
\date{{}}

\begin{{document}}
\maketitle

\begin{{abstract}}
{abstract}
\end{{abstract}}

\section{{Introduction}}
{introduction}

\section{{Related Work}}
{related_work}

\section{{Method}}
{method}

\section{{Experiments}}
{experiments}

\section{{Conclusion}}
{conclusion}

\bibliographystyle{{plain}}
\begin{{thebibliography}}{{99}}
{bibtex}
\end{{thebibliography}}

\end{{document}}
"""


class WriterAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        return ""  # 实际 prompt 在 run() 里构建

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        rq = inputs.get("research_question", "")
        novelty = inputs.get("novelty_hypothesis", "")
        method = inputs.get("method", {})
        related_work_draft = inputs.get("related_work_draft", "")
        review = inputs.get("review", {})
        keywords = inputs.get("keywords", [])

        method_name = method.get("method_name", "") if isinstance(method, dict) else ""
        method_overview = method.get("overview", "") if isinstance(method, dict) else ""
        method_draft = method.get("method_section_draft", "") if isinstance(method, dict) else ""

        # 审稿意见摘要
        revision_notes = ""
        if review and not review.get("parse_error"):
            weaknesses = review.get("weaknesses", [])
            revision_plan = review.get("revision_plan", [])
            revision_notes = (
                "审稿意见（请在写作中针对性回应）：\n"
                + "\n".join(f"- {w.get('issue','')}" for w in weaknesses[:3])
                + "\n修改重点：\n"
                + "\n".join(f"- {r.get('action','')}" for r in revision_plan if r.get("priority") == "high")
            )

        base_context = f"""论文背景：
研究问题：{rq}
创新点：{novelty}
方法名称：{method_name}
方法概述：{method_overview}
关键词：{', '.join(keywords)}
{revision_notes}"""

        sections: dict[str, str] = {}

        # ------------------------------------------------------------------
        # 第1轮：标题 + 摘要
        # ------------------------------------------------------------------
        logger.info("[WriterAgent] 第1轮：生成标题和摘要")
        meta_raw = self._call_llm(f"""{base_context}

请生成论文标题和摘要，输出 JSON：
{{
  "title": "英文标题（简洁有力，10-15词）",
  "abstract": "英文摘要（150-200词，包含问题、方法、实验结果、贡献）"
}}""")
        meta = self._parse_json(meta_raw)
        title = meta.get("title", method_name or "Research Paper")
        abstract = meta.get("abstract", "")

        # ------------------------------------------------------------------
        # 第2-6轮：逐节生成
        # ------------------------------------------------------------------
        section_specs = [
            ("introduction", "Introduction", "400-500词，包含研究背景、问题陈述、主要贡献（bullet points）、论文结构"),
            ("related_work", "Related Work", f"300-400词，基于以下草稿扩展：\n{related_work_draft[:600] if related_work_draft else '请自行撰写'}"),
            ("method", "Method", f"500-600词，包含方法概述、核心模块描述、关键公式（用 \\\\begin{{equation}} 环境），基于以下草稿：\n{method_draft[:600] if method_draft else method_overview}"),
            ("experiments", "Experiments", "300-400词，包含数据集描述、评估指标、实验设置、结果表格占位符（\\\\begin{table}...\\\\end{table}）"),
            ("conclusion", "Conclusion", "150-200词，总结贡献、局限性、未来工作"),
        ]

        for key, sec_name, instruction in section_specs:
            logger.info(f"[WriterAgent] 生成 {sec_name} 节")
            raw = self._call_llm(f"""{base_context}

请撰写论文的 {sec_name} 节。

要求：{instruction}

直接输出 LaTeX 内容（不需要 \\\\section 标题行，不需要 JSON 包裹，用 ```latex 围栏包裹）。""")
            latex_match = re.search(r"```(?:latex|tex)?\s*\n?([\s\S]*?)\n?```", raw)
            sections[key] = latex_match.group(1).strip() if latex_match else raw.strip()

        # ------------------------------------------------------------------
        # 第7轮：BibTeX
        # ------------------------------------------------------------------
        logger.info("[WriterAgent] 生成 BibTeX 参考文献")
        bib_raw = self._call_llm(f"""{base_context}

请生成 5-8 条相关参考文献的 BibTeX 条目（格式正确，包含真实或合理的论文信息）。
直接输出 BibTeX 内容（用 ```bibtex 围栏包裹）。""")
        bib_match = re.search(r"```(?:bibtex)?\s*\n?([\s\S]*?)\n?```", bib_raw)
        bibtex = bib_match.group(1).strip() if bib_match else bib_raw.strip()

        # ------------------------------------------------------------------
        # 拼装完整 LaTeX
        # ------------------------------------------------------------------
        # 转义 LaTeX 中的花括号，避免 str.format() 崩溃
        def _escape(s: str) -> str:
            return s.replace("{", "{{").replace("}", "}}")

        full_latex = _LATEX_TEMPLATE.format(
            title=_escape(title),
            abstract=_escape(abstract),
            introduction=_escape(sections.get("introduction", "")),
            related_work=_escape(sections.get("related_work", "")),
            method=_escape(sections.get("method", "")),
            experiments=_escape(sections.get("experiments", "")),
            conclusion=_escape(sections.get("conclusion", "")),
            bibtex=_escape(bibtex),
        )

        output = {
            "title": title,
            "abstract": abstract,
            "latex_sections": sections,
            "bibtex_entries": bibtex,
            "full_paper_latex": full_latex,
        }

        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={"title": title, "sections": list(sections.keys())},
                tags=["WriterAgent", stage_id],
            )

        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
