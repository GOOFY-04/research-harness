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
    required_fields = {"weaknesses": list, "revision_plan": list, "recommendation": str,
                       "evidence_verdict": str, "claim_scope": str}
    model = "claude-opus-4-6"
    max_tokens = 4096
    use_extended_thinking = True
    thinking_budget = 8000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        rq = inputs.get("research_question", "")
        original_direction = state.get("metadata", {}).get("research_direction", "")
        if original_direction:
            rq = (f"{rq}\n\nOriginal research direction and acceptance constraints: "
                  f"{original_direction}")
        implementation = inputs.get("implementation", [])
        source_evidence = []
        remaining = 60000
        for item in implementation if isinstance(implementation, list) else []:
            if remaining <= 0 or not isinstance(item, dict):
                break
            original = str(item.get("content", ""))
            limit = min(15000, remaining)
            content = original[:limit]
            source_evidence.append({"path": item.get("path"), "content": content,
                                    "complete": len(content) == len(original),
                                    "original_chars": len(original)})
            remaining -= len(content)
        if source_evidence:
            rq += ("\n\nExecuted implementation evidence (prefer this over early design hints):\n"
                   + json.dumps({"dependencies": inputs.get("dependencies", ""),
                                 "files": source_evidence}, ensure_ascii=False))
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

真实代码执行报告（快速验证不等于完整实验）：
{json.dumps(inputs.get('execution', {}), ensure_ascii=False)}

源码证据中的 complete=false 只表示评审上下文预算发生裁剪，不表示实际文件不完整。
不得因上下文裁剪声称源码缺失；只能把无法核验的具体实现列为 scope 限制。

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
  "evidence_verdict": "supported|contradicted|inconclusive|invalid",
  "claim_scope": "一句话说明执行证据实际支持或反驳到什么范围，不得超出真实数据、任务和运行次数",
  "strengths": ["优点1", "优点2", "优点3"],
  "weaknesses": [
    {{"issue": "问题描述", "severity": "critical|major|minor", "category": "validity|scope|presentation", "suggestion": "改进建议"}}
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

    def validate_output(self, output: dict) -> None:
        super().validate_output(output)
        if output["recommendation"] not in {"accept", "weak_accept", "weak_reject", "reject"}:
            raise ValueError("Invalid publication recommendation")
        if output["evidence_verdict"] not in {"supported", "contradicted", "inconclusive", "invalid"}:
            raise ValueError("Invalid evidence verdict")
        for weakness in output["weaknesses"]:
            if (not isinstance(weakness, dict)
                    or weakness.get("severity") not in {"critical", "major", "minor"}
                    or weakness.get("category") not in {"validity", "scope", "presentation"}):
                raise ValueError("Each weakness needs a valid severity and category")
