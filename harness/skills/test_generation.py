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

from harness.core.skill import Skill
from harness.core.llm import LLMClient
from harness.core.io import strip_outer_fence

logger = logging.getLogger(__name__)


class TestGenerationSkill(Skill):
    __test__ = False
    name = "test_generation"
    description = "为给定代码自动生成单元测试"

    def __init__(self, **client_options):
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client_kwargs.update(client_options)
        self._llm = LLMClient(**client_kwargs)
        self._model = self._llm.model

    def validate_inputs(self, inputs: dict) -> bool:
        return isinstance(inputs.get("code"), str) and bool(inputs["code"].strip())

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
            text = self._llm.create(
                model=self._model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )

            # 提取代码块
            test_code = strip_outer_fence(text, ("python",))
            compile(test_code, "<generated_tests>", "exec")
            if not test_code:
                raise ValueError("Empty generated test")

            return {
                "success": True,
                "test_code": test_code,
                "framework": framework,
                "coverage_estimate": "未知（需要运行 coverage.py）",
            }

        except Exception as e:
            logger.error(f"[TestGenerationSkill] 执行失败: {e}")
            return {"error": str(e), "success": False}
