"""
ReviewerAgent — 自我审稿

模拟 NeurIPS/ICML/ICLR 审稿人视角，对研究进行批判性评估。
输入：research_question, method（来自 MethodAgent）, literature（来自 LiteratureAgent）
输出：
  - scores: 各维度评分
  - strengths: 优点列表
  - weaknesses: 缺点列表
  - questions: 审稿人问题
  - revision_plan: 修改计划
"""

import json
import re

from harness.core.agent import BaseAgent


REVIEWER_SYSTEM = """你是一位顶级 AI 会议（NeurIPS/ICML/ICLR）的资深审稿人，
以严格、公正、建设性著称。你会指出真正的问题，而不是泛泛而谈。"""


class ReviewerAgent(BaseAgent):
    model = "claude-opus-4-6"
    use_extended_thinking = True
    thinking_budget = 8000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        rq = inputs.get("research_question", "")
        method = inputs.get("method", {})
        gaps = inputs.get("research_gaps", [])
        baselines = inputs.get("key_baselines", [])

        return f"""请以顶级 AI 会议审稿人的视角，对以下研究进行严格评审。

研究问题：{rq}

方法概述：{method.get("overview", "")}
核心洞察：{method.get("key_insight", "")}
核心模块：{json.dumps(method.get("components", []), ensure_ascii=False)}

研究空白（作者声称解决的）：
{json.dumps(gaps[:3], ensure_ascii=False, indent=2)}

主要基线：{', '.join(baselines[:5]) if baselines else '未指定'}

请从以下维度评分（1-10分）并给出详细意见，输出 JSON：
{{
  "scores": {{
    "novelty": {{"score": 0, "comment": ""}},
    "technical_soundness": {{"score": 0, "comment": ""}},
    "significance": {{"score": 0, "comment": ""}},
    "clarity": {{"score": 0, "comment": ""}},
    "reproducibility": {{"score": 0, "comment": ""}}
  }},
  "overall_score": 0,
  "recommendation": "accept|weak_accept|weak_reject|reject",
  "strengths": ["优点1", "优点2", "优点3"],
  "weaknesses": [
    {{"issue": "问题描述", "severity": "major|minor", "suggestion": "改进建议"}}
  ],
  "questions": ["审稿人问题1", "审稿人问题2", "审稿人问题3"],
  "revision_plan": [
    {{"priority": "high|medium|low", "action": "具体修改动作", "rationale": "原因"}}
  ],
  "missing_experiments": ["缺失实验1", "缺失实验2"],
  "missing_baselines": ["缺失基线1", "缺失基线2"]
}}"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
