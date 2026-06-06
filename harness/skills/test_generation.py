"""
TestGenerationSkill — 测试生成

输入：
  - code: 源代码（字符串）
  - test_framework: 测试框架（pytest/unittest，默认 pytest）

输出：
  - test_code: 生成的测试代码
  - coverage_estimate: 预估覆盖率
"""

import logging
import os
from typing import Any

import anthropic

from harness.core.skill import Skill

logger = logging.getLogger(__name__)


class TestGenerationSkill(Skill):
    name = "test_generation"
    description = "为给定代码自动生成单元测试"

    def __init__(self):
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
        framework = inputs.get("test_framework", "pytest")

        prompt = f"""你是一位测试工程师。请为以下 Python 代码生成完整的单元测试。

源代码：
```python
{code}
```

要求：
1. 使用 {framework} 框架
2. 覆盖主要功能和边界情况
3. 包含正常情况和异常情况
4. 测试代码清晰易读
5. 包含必要的 fixtures 和 mocks

请直接输出测试代码（用 ```python 包裹），不要输出 JSON。"""

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join([block.text for block in response.content if block.type == "text"])

            # 提取代码块
            import re
            code_match = re.search(r"```(?:python)?\s*\n?([\s\S]*?)\n?```", text)
            test_code = code_match.group(1).strip() if code_match else text.strip()

            return {
                "success": True,
                "test_code": test_code,
                "framework": framework,
                "coverage_estimate": "未知（需要运行 coverage.py）",
            }

        except Exception as e:
            logger.error(f"[TestGenerationSkill] 执行失败: {e}")
            return {"error": str(e), "success": False}
