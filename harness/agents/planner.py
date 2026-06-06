"""
PlannerAgent — 选题与研究规划

输入：研究方向（字符串）
输出：
  - research_question: 精炼后的研究问题
  - novelty_hypothesis: 创新点假设
  - stage_plan: 各阶段目标列表
  - keywords: 文献检索关键词
"""

import json
import re
from typing import Any

from harness.core.agent import BaseAgent


SYSTEM_PROMPT = """你是一位资深 AI 科研导师，擅长将模糊的研究方向提炼为清晰、可执行的研究计划。
你的输出必须严格遵循 JSON 格式，不要输出任何 JSON 以外的内容。"""


class PlannerAgent(BaseAgent):
    model = "claude-opus-4-6"
    use_extended_thinking = True
    thinking_budget = 8000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        direction = inputs.get("research_direction", "")
        context = inputs.get("context", "")

        # 从记忆中取历史规划
        history = self.recall("planning", n=3)
        history_text = ""
        if history:
            history_text = "\n\n历史规划记录（供参考）：\n" + json.dumps(history, ensure_ascii=False, indent=2)

        return f"""请将以下研究方向提炼为一份结构化研究计划。

研究方向：{direction}
{f"背景信息：{context}" if context else ""}
{history_text}

请输出如下 JSON 结构：
{{
  "research_question": "精炼后的核心研究问题（一句话）",
  "novelty_hypothesis": "创新点假设（2-3句话，说明与现有工作的差异）",
  "stage_plan": [
    {{"stage": "文献调研", "goal": "...", "expected_output": "..."}},
    {{"stage": "方法设计", "goal": "...", "expected_output": "..."}},
    {{"stage": "实验规划", "goal": "...", "expected_output": "..."}},
    {{"stage": "代码实现", "goal": "...", "expected_output": "..."}},
    {{"stage": "结果分析", "goal": "...", "expected_output": "..."}},
    {{"stage": "论文撰写", "goal": "...", "expected_output": "..."}}
  ],
  "keywords": ["关键词1", "关键词2", "关键词3", "关键词4", "关键词5"],
  "related_venues": ["顶会/期刊1", "顶会/期刊2"],
  "estimated_novelty": "high|medium|low",
  "risks": ["风险1", "风险2"]
}}"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
