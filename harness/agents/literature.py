"""
LiteratureAgent — 文献调研

输入：keywords, research_question
输出：
  - papers: 相关论文列表（含摘要、贡献、局限）
  - research_gaps: 识别出的研究空白
  - related_work_draft: 相关工作段落草稿
"""

import json
import re
from typing import Any

from harness.core.agent import BaseAgent
from harness.tools.arxiv import search_arxiv


SYSTEM_PROMPT = """你是一位文献综述专家，能够快速识别研究空白并提炼相关工作的核心贡献。
输出严格遵循 JSON 格式。"""


class LiteratureAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 16000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        keywords = inputs.get("keywords", [])
        research_question = inputs.get("research_question", "")

        # 调用 arxiv 工具获取真实论文
        papers_context = ""
        if keywords:
            try:
                results = search_arxiv(
                    query=" ".join(keywords[:3]),
                    max_results=10,
                )
                papers_context = "\n\n从 arXiv 检索到的相关论文：\n" + json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                papers_context = f"\n\n（arXiv 检索失败: {e}，请基于已有知识分析）"

        return f"""请对以下研究问题进行文献调研分析。

研究问题：{research_question}
检索关键词：{', '.join(keywords)}
{papers_context}

请输出如下 JSON 结构：
{{
  "papers": [
    {{
      "title": "论文标题",
      "authors": "作者",
      "year": 2024,
      "venue": "发表会议/期刊",
      "arxiv_id": "xxxx.xxxxx（如有）",
      "core_contribution": "核心贡献（2-3句）",
      "limitations": "局限性（1-2句）",
      "relevance": "high|medium|low"
    }}
  ],
  "research_gaps": [
    {{
      "gap": "研究空白描述",
      "evidence": "支撑证据（引用哪些论文）",
      "opportunity": "可能的解决方向"
    }}
  ],
  "related_work_draft": "相关工作段落草稿（学术写作风格，300-500字）",
  "key_baselines": ["基线方法1", "基线方法2"],
  "recommended_datasets": ["数据集1", "数据集2"]
}}"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
