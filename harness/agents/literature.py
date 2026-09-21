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
from pathlib import Path
from typing import Any

from harness.core.agent import BaseAgent
from harness.tools.arxiv import search_arxiv


SYSTEM_PROMPT = """你是一位文献综述专家，能够快速识别研究空白并提炼相关工作的核心贡献。
输出严格遵循 JSON 格式。"""


_STOP_WORDS = {
    "about", "analysis", "benchmark", "effect", "for", "from", "impact", "lightweight",
    "model", "models", "of", "on", "recovery", "study", "the", "using", "with",
}
_VISION_CUES = {"camera", "image", "images", "multi-view", "reprojection", "sfm", "structure-from-motion"}
_VISION_CATEGORIES = {"cs.CV", "cs.RO", "cs.GR", "eess.IV"}


def filter_relevant_sources(results, keywords):
    """Remove obvious cross-domain arXiv matches before they can be cited."""
    query = " ".join(str(item) for item in keywords).casefold()
    terms = {token for token in re.findall(r"[a-z][a-z0-9-]{2,}", query)
             if token not in _STOP_WORDS}
    vision_query = any(cue in query for cue in _VISION_CUES)
    filtered = []
    for source in results:
        categories = set(source.get("categories", []))
        # Some offline fixtures and non-arXiv adapters do not expose categories.
        # Keep those records rather than pretending we can classify them.
        if not categories:
            filtered.append(source)
            continue
        if vision_query and categories and categories.isdisjoint(_VISION_CATEGORIES):
            continue
        title = source.get("title", "").casefold()
        abstract = source.get("abstract", "").casefold()
        title_terms = set(re.findall(r"[a-z][a-z0-9-]{2,}", title))
        abstract_terms = set(re.findall(r"[a-z][a-z0-9-]{2,}", abstract))
        score = 2 * len(terms & title_terms) + len(terms & abstract_terms)
        if not terms or score >= 3:
            filtered.append(source)
    return filtered


class LiteratureAgent(BaseAgent):
    required_fields = {"papers": list, "sources": list, "research_gaps": list,
                       "related_work_draft": str, "key_baselines": list}
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    @staticmethod
    def _source_cache(state):
        session = state.get("session_dir") if isinstance(state, dict) else None
        return Path(session) / "literature_sources.json" if session else None

    def _load_cached_sources(self, state, keywords):
        path = self._source_cache(state)
        if not path or not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return filter_relevant_sources(value, keywords) if isinstance(value, list) else []

    def _save_cached_sources(self, state, sources):
        path = self._source_cache(state)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        keywords = inputs.get("keywords", [])
        research_question = inputs.get("research_question", "")

        # 调用 arxiv 工具获取真实论文
        papers_context = ""
        self._sources = []
        if keywords:
            try:
                results = search_arxiv(
                    query=" ".join(keywords[:3]),
                    max_results=10,
                )
                results = filter_relevant_sources(results, keywords)
                if not results and len(keywords) > 1:
                    results = search_arxiv(query=keywords[0], max_results=10)
                    results = filter_relevant_sources(results, keywords)
                if not results:
                    raise RuntimeError("arXiv returned no papers relevant to the planned English keywords")
                self._sources = results
                self._save_cached_sources(state, results)
                papers_context = "\n\n从 arXiv 检索到的相关论文：\n" + json.dumps(results, ensure_ascii=False, indent=2)
            except Exception as e:
                cached = self._load_cached_sources(state, keywords)
                if not cached:
                    raise RuntimeError(f"文献检索失败，不能生成未经核实的参考文献: {e}") from e
                self._sources = cached
                papers_context = ("\n\n网络检索暂时失败，使用该会话此前缓存并验证过的 arXiv 元数据：\n"
                                  + json.dumps(cached, ensure_ascii=False, indent=2))

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
        output = self._parse_json(raw_text)
        output["sources"] = self._sources
        return output
