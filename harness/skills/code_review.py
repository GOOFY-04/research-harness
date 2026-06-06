"""
CodeReviewSkill — 代码审查

输入：
  - code: 代码内容（字符串）
  - language: 编程语言（默认 python）

输出：
  - issues: 发现的问题列表
  - suggestions: 改进建议
  - score: 代码质量评分（0-100）
"""

import logging
import os
from typing import Any

import anthropic

from harness.core.skill import Skill

logger = logging.getLogger(__name__)


class CodeReviewSkill(Skill):
    name = "code_review"
    description = "对代码进行静态分析和质量审查，发现潜在问题并提供改进建议"

    def __init__(self):
        # 初始化 Claude client
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**client_kwargs)
        self._model = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")

    def validate_inputs(self, inputs: dict) -> bool:
        return "code" in inputs

    def execute(self, inputs: dict) -> dict:
        code = inputs["code"]
        language = inputs.get("language", "python")

        prompt = f"""你是一位资深代码审查专家。请审查以下 {language} 代码，关注：

1. 代码质量（可读性、可维护性）
2. 潜在 bug（边界条件、空指针、类型错误）
3. 性能问题（时间/空间复杂度）
4. 安全问题（注入、溢出、权限）
5. 最佳实践（设计模式、命名规范）

代码：
```{language}
{code}
```

请输出 JSON 格式的审查报告：
{{
  "score": 85,  // 0-100 分
  "issues": [
    {{"severity": "high/medium/low", "line": 10, "message": "问题描述"}},
  ],
  "suggestions": [
    "改进建议1",
    "改进建议2"
  ],
  "summary": "总体评价（2-3句话）"
}}"""

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join([block.text for block in response.content if block.type == "text"])

            # 解析 JSON
            import json
            import re
            json_match = re.search(r"\{[\s\S]*\}", text)
            if json_match:
                result = json.loads(json_match.group())
                result["success"] = True
                return result
            else:
                return {"error": "无法解析 LLM 输出", "success": False, "raw": text}

        except Exception as e:
            logger.error(f"[CodeReviewSkill] 执行失败: {e}")
            return {"error": str(e), "success": False}
