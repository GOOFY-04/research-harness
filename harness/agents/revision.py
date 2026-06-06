"""
RevisionAgent — 基于审稿意见决定是否修订

输入：review_result (来自 ReviewerAgent)
输出：
  - needs_revision: 是否需要修订
  - revision_type: code|experiment|baseline|writing
  - revision_instructions: 具体修订指令
  - rerun_stages: 需要重新运行的阶段列表
"""

import json

from harness.core.agent import BaseAgent


class RevisionAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    max_tokens = 8192

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        review = inputs.get("review_result", {})
        scores = review.get("scores", {})
        overall_score = review.get("overall_score", 0)
        weaknesses = review.get("weaknesses", [])
        revision_plan = review.get("revision_plan", [])
        missing_experiments = review.get("missing_experiments", [])
        missing_baselines = review.get("missing_baselines", [])

        return f"""你是一位研究修订规划专家。请分析以下审稿意见，决定是否需要修订以及如何修订。

审稿结果：
总分：{overall_score}/10
推荐：{review.get("recommendation", "")}

各维度评分：
{json.dumps(scores, ensure_ascii=False, indent=2)}

弱点：
{json.dumps(weaknesses, ensure_ascii=False, indent=2)}

修订计划：
{json.dumps(revision_plan, ensure_ascii=False, indent=2)}

缺失实验：
{json.dumps(missing_experiments, ensure_ascii=False)}

缺失基线：
{json.dumps(missing_baselines, ensure_ascii=False)}

决策标准：
- 如果总分 >= 8 且无 major 弱点，跳过修订
- 如果有代码错误或 reproducibility < 6，需要代码修订
- 如果缺少关键实验或基线，需要实验修订
- 如果 clarity < 6，需要写作修订

请输出 JSON：
{{
  "needs_revision": true/false,
  "skip_reason": "如果不需要修订，说明原因",
  "revision_type": "code|experiment|baseline|writing|none",
  "revision_priority": "high|medium|low",
  "revision_instructions": {{
    "code_changes": ["代码修改1", "代码修改2"],
    "new_experiments": ["新实验1", "新实验2"],
    "new_baselines": ["新基线1"],
    "writing_improvements": ["写作改进1"]
  }},
  "rerun_stages": ["coding", "code_execution"],
  "expected_improvement": "预期改进效果描述"
}}"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
