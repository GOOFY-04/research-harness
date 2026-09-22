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
    required_fields = {"method_name": str, "overview": str, "components": list,
                       "algorithm": str, "method_section_draft": str, "invariants": list}
    model = "claude-opus-4-6"
    max_tokens = 6144
    use_extended_thinking = True
    thinking_budget = 10000

    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        rq = inputs.get("research_question", "")
        gaps = inputs.get("research_gaps", [])
        baselines = inputs.get("key_baselines", [])
        related_work = inputs.get("related_work_draft", "")
        review_feedback = inputs.get("review_feedback")
        previous_method = inputs.get("previous_method")
        previous_execution = inputs.get("previous_execution")
        revision = ""
        if isinstance(review_feedback, dict):
            revision = f"""

这是一次审稿驱动的修订。上一轮方法、执行证据和审稿意见如下：
上一轮方法：{json.dumps(previous_method, ensure_ascii=False)[:10000]}
上一轮执行：{json.dumps(previous_execution, ensure_ascii=False)[:6000]}
审稿反馈：{json.dumps(review_feedback, ensure_ascii=False)[:12000]}

必须逐项处理所有 major 问题和 high priority 修改，并把缺失基线、消融与
验证场景落实到可在 CPU 上执行的实验设计中。不得只修改措辞或隐藏负面结果。
新设计必须明确说明相对上一轮改变了什么，以及哪个实验能证伪修订后的主张。
对于每个控制变量，必须核对其定义、单调方向和伪代码更新符号。经验分位数、
指示函数等不可微算子不得被虚构为严格闭式梯度；若只能使用代理梯度、有限差分
或渐近近似，必须如实命名并写出适用条件。
"""

        return f"""请为以下研究问题设计一个创新性方法。

研究问题：{rq}

已识别的研究空白：
{json.dumps(gaps, ensure_ascii=False, indent=2)}

主要基线方法：{', '.join(baselines) if baselines else '未指定'}

相关工作摘要：
{related_work[:1000] if related_work else '（无）'}
{revision}

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
  "ablation_targets": ["消融实验目标1", "消融实验目标2", "消融实验目标3"],
  "invariants": [
    {{
      "quantity": "变量或统计量",
      "definition": "精确定义及取值范围",
      "monotonic_effect": "增大该量会让目标输出如何变化，并说明依据",
      "falsification_test": "可执行的方向性/边界测试"
    }}
  ],
  "revision_response": ["逐项说明如何处理上一轮审稿意见；首次设计时为空数组"]
}}"""

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """Audit revised designs before expensive code generation begins."""
        prompt = self.build_prompt(stage_id, inputs, state)
        output = self.parse_output(self._call_llm(prompt), stage_id, inputs)
        if isinstance(inputs.get("review_feedback"), dict):
            audit_prompt = f"""你是方法一致性审计员。只检查内部数学与算法一致性，不评价新颖性。
研究问题：{inputs.get('research_question', '')}
候选方法：{json.dumps(output, ensure_ascii=False)}

逐项核对：变量定义与取值范围；单调方向与更新符号；目标、约束与投影方向；
声称的闭式梯度是否真的对所写目标求导；伪代码与文字是否一致。
输出 JSON：{{"valid": true|false, "issues": [{{"severity":"critical|major|minor",
"invariant":"被违反的不变量", "contradiction":"具体矛盾", "repair":"最小修复"}}]}}。
只要存在 critical 或 major 内部矛盾，valid 必须为 false。"""
            audit = self._parse_json(self._call_llm(audit_prompt))
            issues = audit.get("issues") if isinstance(audit, dict) else None
            blockers = [item for item in issues or [] if isinstance(item, dict)
                        and item.get("severity") in {"critical", "major"}]
            if (audit.get("parse_error") or not isinstance(audit.get("valid"), bool)
                    or not isinstance(issues, list) or audit.get("valid") is not True or blockers):
                summary = "; ".join(str(item.get("contradiction", "")) for item in blockers[:3])
                raise ValueError("Method consistency audit failed"
                                 + (f": {summary}" if summary else ""))
            output["consistency_audit"] = audit
        self.validate_output(output)
        if self.memory:
            self.memory.append(stage_id, {"inputs": inputs, "output": output},
                               [self.__class__.__name__, stage_id])
        return output

    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        output = self._parse_json(raw_text)
        if isinstance(inputs.get("review_feedback"), dict):
            response = output.get("revision_response")
            blockers = [item for item in inputs["review_feedback"].get("weaknesses", [])
                        if isinstance(item, dict) and item.get("severity") in {"critical", "major"}]
            if (not isinstance(response, list) or not response
                    or not all(isinstance(item, str) and item.strip() for item in response)
                    or len(response) < len(blockers)):
                raise ValueError("Revised method must include a non-empty revision_response")
        return output

    def validate_output(self, output: dict) -> None:
        super().validate_output(output)
        if not output["invariants"]:
            raise ValueError("Method must define at least one falsifiable invariant")
        required = {"quantity", "definition", "monotonic_effect", "falsification_test"}
        for invariant in output["invariants"]:
            if (not isinstance(invariant, dict) or not required.issubset(invariant)
                    or not all(isinstance(invariant[key], str) and invariant[key].strip()
                               for key in required)):
                raise ValueError("Each method invariant needs definition, direction, and test")
        if output.get("revision_response"):
            audit = output.get("consistency_audit")
            if not isinstance(audit, dict) or audit.get("valid") is not True:
                raise ValueError("Revised method is missing a passing consistency audit")
