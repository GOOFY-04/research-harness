"""
MethodAgent — 方法设计

输入：research_question, research_gaps, key_baselines（来自 LiteratureAgent）
输出：
  - method_name: 方法名称
  - overview: 方法概述
  - components: 核心模块列表
  - algorithm: 伪代码
  - method_section_draft: 论文 Method 节草稿
"""

import json
import re

from harness.core.agent import BaseAgent


class MethodAgent(BaseAgent):
    model = "claude-sonnet-4-6"
    use_extended_thinking = False
    thinking_budget = 10000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        rq = inputs.get("research_question", "")
        gaps = inputs.get("research_gaps", [])
        baselines = inputs.get("key_baselines", [])
        related_work = inputs.get("related_work_draft", "")

        return f"""请为以下研究问题设计一个创新性方法。

研究问题：{rq}

已识别的研究空白：
{json.dumps(gaps, ensure_ascii=False, indent=2)}

主要基线方法：{', '.join(baselines) if baselines else '未指定'}

相关工作摘要：
{related_work[:1000] if related_work else '（无）'}

请输出如下 JSON 结构：
{{
  "method_name": "方法名称（英文缩写 + 中文全称）",
  "overview": "方法概述（3-5句话，说明核心思路）",
  "key_insight": "核心洞察（1-2句话，说明为什么这个方法能解决问题）",
  "components": [
    {{
      "name": "模块名称",
      "role": "该模块的作用",
      "novelty": "相比现有方法的创新点",
      "implementation_hint": "实现要点"
    }}
  ],
  "algorithm": "伪代码（用缩进表示层级，不超过30行）",
  "complexity": {{
    "time": "时间复杂度分析",
    "space": "空间复杂度分析"
  }},
  "method_section_draft": "论文 Method 节草稿（学术写作风格，600-800字，包含公式占位符如 Eq.(1)）",
  "ablation_targets": ["消融实验目标1", "消融实验目标2", "消融实验目标3"]
}}"""

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        return self._parse_json(raw_text)
