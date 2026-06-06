"""
PaperSummarySkill — 论文摘要与关键信息提取

输入：
  - paper_text: 论文标题 + 摘要文本（必填）
  - paper_id: 论文 arxiv ID 或标题（可选，用于日志）
  - detail_level: "brief" | "standard" | "detailed"（默认 standard）

输出：
  - summary: 结构化的论文摘要
  - keywords: 提取的关键词
  - contribution: 主要贡献（1-2句话）
  - method_category: 方法类型分类
  - relevance_score: 与研究方向的关联度评分（如果提供了 context）
  - success: True/False
"""

import json
import logging
import os
import re
from typing import Any

import anthropic

from harness.core.skill import Skill

logger = logging.getLogger(__name__)


class PaperSummarySkill(Skill):
    name = "paper_summary"
    description = "使用 LLM 对论文进行结构化摘要，提取贡献、方法和关键词"

    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**client_kwargs)
        self._model = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")

    def validate_inputs(self, inputs: dict) -> bool:
        return "paper_text" in inputs

    def execute(self, inputs: dict) -> dict:
        paper_text = inputs["paper_text"]
        paper_id = inputs.get("paper_id", "unknown")
        detail_level = inputs.get("detail_level", "standard")
        context = inputs.get("context", "")  # 可选的科研方向上下文

        detail_prompts = {
            "brief": "请用 2-3 句话概括这篇论文。",
            "standard": "请提供结构化的摘要，包括：研究问题、方法、主要结果、局限性。",
            "detailed": "请提供详细的结构化摘要，包括：研究问题与动机、方法细节、实验设计、关键结果与指标、与相关工作的对比、局限性分析。",
        }
        detail_instruction = detail_prompts.get(detail_level, detail_prompts["standard"])

        context_part = ""
        if context:
            context_part = f"\n\n研究背景/方向：{context}\n请在摘要中额外评估这篇论文与该研究方向的关联度（0-100分）。"

        prompt = f"""你是一位资深科研助理。请对以下论文进行结构化分析。

论文信息：
{paper_text}
{context_part}

{detail_instruction}

请输出 JSON 格式：
{{
  "title": "论文标题",
  "summary": "摘要内容",
  "keywords": ["关键词1", "关键词2", "关键词3", "关键词4", "关键词5"],
  "contribution": "1-2句话概括主要贡献",
  "method_category": "方法类别（如：Transformer/GNN/Diffusion/优化方法等）",
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["不足1", "不足2"],
  "relevance_score": 85,  // 仅在提供了 context 时填写，0-100
  "novelty_assessment": "high/medium/low"
}}"""

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join([block.text for block in response.content if block.type == "text"])

            json_match = re.search(r"\{[\s\S]*\}", text)
            if json_match:
                result = json.loads(json_match.group())
                result["success"] = True
                result["paper_id"] = paper_id
                return result
            else:
                return {"error": "无法解析 LLM 输出", "success": False, "raw": text, "paper_id": paper_id}

        except Exception as e:
            logger.error(f"[PaperSummarySkill] 执行失败: {e}")
            return {"error": str(e), "success": False, "paper_id": paper_id}
