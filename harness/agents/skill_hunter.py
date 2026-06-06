"""
SkillHunterAgent — 自动搜索和获取社区 skills

功能：
  1. 分析失败原因，识别所需能力
  2. 从 GitHub/PyPI/HuggingFace 搜索相关 skills
  3. 评估候选 skills（质量、安全性、兼容性）
  4. 返回推荐的 skills 列表

输出：
  - needed_capability: 所需能力描述
  - candidates: 候选 skills 列表
  - recommendation: 推荐的 skill（带理由）
"""

import json
import logging
import re
from typing import Optional

from harness.core.agent import BaseAgent

logger = logging.getLogger(__name__)


class SkillHunterAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        error_msg = inputs.get("error_msg", "")
        task_description = inputs.get("task_description", "")
        existing_skills = inputs.get("existing_skills", [])

        return f"""你是一位 AI 系统扩展专家。当前任务失败，你需要分析错误原因并寻找合适的工具/技能来解决问题。

任务描述：{task_description}

错误信息：
{error_msg}

现有技能库：
{json.dumps(existing_skills, ensure_ascii=False, indent=2)}

请分析并输出 JSON：
{{
  "error_analysis": "错误根因分析（1-2句）",
  "needed_capability": "所需能力描述（具体、可搜索）",
  "search_keywords": ["关键词1", "关键词2", "关键词3"],
  "candidate_sources": [
    {{
      "type": "github|pypi|huggingface|tool",
      "name": "项目/包名称",
      "url": "URL",
      "description": "功能描述",
      "why_suitable": "为什么合适",
      "estimated_quality": "high|medium|low",
      "potential_risks": ["风险1", "风险2"]
    }}
  ],
  "recommendation": {{
    "source": "推荐的来源（从 candidate_sources 中选择）",
    "integration_strategy": "direct_use|wrap_as_skill|adapt_code",
    "rationale": "推荐理由"
  }},
  "alternative_solution": "如果没有合适的 skill，提供替代方案"
}}

搜索策略：
1. 优先考虑成熟的 Python 包（PyPI 上有的）
2. 搜索 GitHub 上的相关工具和脚本
3. 考虑 Hugging Face 上的模型或 spaces
4. 评估时关注：stars/downloads、文档质量、活跃度、许可证"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
