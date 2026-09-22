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
import hashlib
import re

from harness.core.agent import BaseAgent
from harness.core.io import atomic_json, safe_path


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
            blocker_count = sum(1 for item in review_feedback.get("weaknesses", [])
                                if isinstance(item, dict)
                                and item.get("severity") in {"critical", "major"})
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
在线或时间序列实验必须遵守因果顺序：时刻 t 的预测只能使用截至 t-1 的信息，
输出预测后才能观测 y_t 并更新状态；不得用同一观测同时选参和评估。
若 alpha 定义为保形预测的误覆盖率且 q=Q_(1-alpha)，必须区分两种方向：
alpha 越小，q 和宽度越大、越保守；无可行解时的保守回退选最小 alpha，
满足覆盖约束后追求最窄区间则选可行集合中的最大 alpha。
revision_response 必须至少包含 {blocker_count} 条非空字符串，逐条对应 critical/major 问题。
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
        revision = isinstance(inputs.get("review_feedback"), dict)
        draft_path = None
        feedback_path = None
        context_digest = None
        output = None
        if revision and state.get("session_dir"):
            context_digest = hashlib.sha256(("method-audit-v1\n" + prompt).encode("utf-8")).hexdigest()
            draft_path = safe_path(state["session_dir"],
                                   f".drafts/method_{context_digest[:20]}.json")
            feedback_path = safe_path(state["session_dir"],
                                      f".drafts/audit_feedback_{context_digest[:20]}.json")
            if draft_path.is_file():
                try:
                    saved = json.loads(draft_path.read_text(encoding="utf-8"))
                    candidate = saved.get("candidate")
                    if saved.get("context_sha256") == context_digest and isinstance(candidate, dict):
                        self._validate_candidate(candidate)
                        output = candidate
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    output = None
            feedback_issues = []
            if output is None and feedback_path.is_file():
                try:
                    saved_feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
                    issues = saved_feedback.get("issues")
                    if (saved_feedback.get("context_sha256") == context_digest
                            and isinstance(issues, list) and issues):
                        feedback_issues = issues
                except (OSError, TypeError, json.JSONDecodeError):
                    pass
            prior_errors = state.get("stages", {}).get(stage_id, {}).get("errors", [])
            if output is None and not feedback_issues:
                relevant_errors = [error for error in prior_errors
                                   if isinstance(error, str) and (
                                       error.startswith("Method consistency audit failed")
                                       or error.startswith("Revised method must include"))]
                feedback_issues = [{"severity": "major", "contradiction": error[:2000]}
                                   for error in relevant_errors[-3:]]
            if output is None and feedback_issues:
                prompt += f"""

上一候选被独立一致性审计拒绝。重新设计时必须逐项修复以下问题，不能仅改写表述：
{json.dumps(feedback_issues, ensure_ascii=False)[:4000]}
"""
            audit_failures = sum(1 for error in prior_errors if isinstance(error, str)
                                 and error.startswith("Method consistency audit failed"))
            if output is None and audit_failures >= 3:
                prompt += """

该阶段已连续多次因内部一致性失败。必须降低算法复杂度：优先直接使用有明确
单调语义的原始参数、有限候选或网格搜索、以及可枚举验证的约束。除非能逐式
证明更新方向，否则不要引入参数变换、代理梯度、拉格朗日乘子或耦合控制器。
宁可缩小主张，也不要用未经验证的复杂机制维持原主张。
"""
        if output is None:
            output = self.parse_output(self._call_llm(prompt), stage_id, inputs)
            self._validate_candidate(output)
            if draft_path is not None:
                atomic_json(draft_path, {"schema_version": 1, "context_sha256": context_digest,
                                         "candidate": output})
        else:
            import logging
            logging.info("[MethodAgent] resuming validated method candidate; audit pending")
        if revision:
            audit_prompt = f"""你是方法一致性审计员。只检查内部数学与算法一致性，不评价新颖性。
研究问题：{inputs.get('research_question', '')}
候选方法：{json.dumps(output, ensure_ascii=False)}

逐项核对：变量定义与取值范围；单调方向与更新符号；目标、约束与投影方向；
声称的闭式梯度是否真的对所写目标求导；伪代码与文字是否一致。
若是在线或时间序列方法，还要核对每个时刻是否先用截至 t-1 的信息输出预测，
再观测 y_t 并更新；使用当前或未来标签选参、校准或构造同一预测属于 major 问题。
若 alpha 是误覆盖率且 q=Q_(1-alpha)，alpha 减小会使 q 与宽度增大；最保守
回退应选最小 alpha，最窄的可行解应选满足覆盖约束的最大 alpha，不得混淆。
输出 JSON：{{"valid": true|false, "issues": [{{"severity":"critical|major|minor",
"invariant":"被违反的不变量", "contradiction":"具体矛盾", "repair":"最小修复"}}]}}。
只要存在 critical 或 major 内部矛盾，valid 必须为 false。最多返回 4 个问题，
每个字符串字段不超过 80 个汉字；不要输出推理过程、Markdown 或额外字段。"""
            previous_tokens, previous_thinking = self.max_tokens, self.use_extended_thinking
            try:
                self.max_tokens = 1536
                self.use_extended_thinking = False
                audit = self._parse_json(self._call_llm(audit_prompt))
            finally:
                self.max_tokens, self.use_extended_thinking = previous_tokens, previous_thinking
            issues = audit.get("issues") if isinstance(audit, dict) else None
            blockers = [item for item in issues or [] if isinstance(item, dict)
                        and item.get("severity") in {"critical", "major"}]
            if (audit.get("parse_error") or not isinstance(audit.get("valid"), bool)
                    or not isinstance(issues, list) or audit.get("valid") is not True or blockers):
                summary = "; ".join(str(item.get("contradiction", ""))[:240]
                                    for item in blockers[:3])
                if draft_path is not None:
                    draft_path.unlink(missing_ok=True)
                if feedback_path is not None and isinstance(issues, list) and issues:
                    atomic_json(feedback_path, {
                        "schema_version": 1,
                        "context_sha256": context_digest,
                        "issues": issues[:4],
                    })
                raise ValueError("Method consistency audit failed"
                                 + (f": {summary}" if summary else ""))
            output["consistency_audit"] = audit
            if feedback_path is not None:
                feedback_path.unlink(missing_ok=True)
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
        self._validate_candidate(output)
        if output.get("revision_response"):
            audit = output.get("consistency_audit")
            if not isinstance(audit, dict) or audit.get("valid") is not True:
                raise ValueError("Revised method is missing a passing consistency audit")

    def _validate_candidate(self, output: dict) -> None:
        BaseAgent.validate_output(self, output)
        if not output["invariants"]:
            raise ValueError("Method must define at least one falsifiable invariant")
        required = {"quantity", "definition", "monotonic_effect", "falsification_test"}
        for invariant in output["invariants"]:
            if (not isinstance(invariant, dict) or not required.issubset(invariant)
                    or not all(isinstance(invariant[key], str) and invariant[key].strip()
                               for key in required)):
                raise ValueError("Each method invariant needs definition, direction, and test")
