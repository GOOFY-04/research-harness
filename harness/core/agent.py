"""
BaseAgent — 所有专职 agent 的抽象基类

子类只需实现 build_prompt() 和 parse_output()，
BaseAgent 负责调用 Claude API、重试、记忆读写、日志。
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from time import monotonic
from typing import Any, Optional

from .memory import MemoryStore
from .llm import LLMClient
from .io import strip_outer_fence, validate_result, OutputValidationError

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    所有科研 agent 的基类。

    子类实现：
        build_prompt(stage_id, inputs, state) -> str
        parse_output(raw_text, stage_id, inputs) -> dict
    """

    # 子类可覆盖
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8192
    temperature: float = 1.0  # Claude 推荐 extended thinking 用 1.0
    required_fields: dict[str, type] = {}
    allow_empty_fields = {"dependencies", "requirements"}

    def __init__(
        self,
        memory: Optional[MemoryStore] = None,
        api_key: Optional[str] = None,
        use_extended_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        base_url: Optional[str] = None,
        request_timeout: float = 120,
        client=None,
        protocol: str = "anthropic",
        api_key_env: Optional[str] = None,
        stream_responses: bool = False,
    ):
        self.memory = memory
        self.use_extended_thinking = (getattr(type(self), "use_extended_thinking", False)
                                      if use_extended_thinking is None else use_extended_thinking)
        self.thinking_budget = (getattr(type(self), "thinking_budget", 5000)
                                if thinking_budget is None else thinking_budget)
        self.max_tokens = max_tokens if max_tokens is not None else max(
            type(self).max_tokens, self.thinking_budget + 4096 if self.use_extended_thinking else 0)
        if self.max_tokens <= 0 or (self.use_extended_thinking and
                not 1024 <= self.thinking_budget < self.max_tokens):
            raise ValueError("Thinking budget must be >=1024 and < max_tokens")

        # 支持自定义 base URL（代理 / 镜像站）
        # 优先级：参数 > ANTHROPIC_API_KEY > ANTHROPIC_AUTH_TOKEN
        if protocol == "openai_compatible":
            resolved_key = api_key or os.environ.get(api_key_env or "AGNES_API_KEY")
        else:
            resolved_key = (
                api_key
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            )
        self._llm = LLMClient(api_key=resolved_key, base_url=base_url,
                              timeout=request_timeout, client=client,
                              protocol=protocol, api_key_env=api_key_env,
                              stream_responses=stream_responses)

        # 从环境变量读取模型别名（允许在 .env 中统一覆盖）
        self.model = model or (self._llm.model if protocol == "openai_compatible" else (
            os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", self.model)
            if "sonnet" in self.model
            else os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", self.model)
            if "opus" in self.model
            else self.model
        ))

    # ------------------------------------------------------------------
    # 主接口（由 WorkflowEngine 调用）
    # ------------------------------------------------------------------

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """执行一个阶段，返回结构化输出。"""
        logger.info(f"[{self.__class__.__name__}] stage={stage_id}")

        # 构建 prompt
        prompt = self.build_prompt(stage_id, inputs, state)
        history = state.get("stages", {}).get(stage_id, {}).get("attempt_history", [])
        previous = next((item for item in reversed(history)
                         if isinstance(item, dict) and isinstance(item.get("output"), dict)
                         and item.get("error")), {})
        if isinstance(previous.get("output"), dict) and previous.get("error"):
            excerpt = json.dumps(previous["output"], ensure_ascii=False)[:6000]
            prompt += (
                f"\n\nPrevious response failed validation: {str(previous['error'])[:1000]}\n"
                "Correct the response using the required schema above. Preserve scientific criticism "
                "and measured evidence; do not weaken findings merely to satisfy the schema. "
                "Return the complete corrected response.\n"
                f"Previous invalid response (bounded excerpt): {excerpt}"
            )

        # 调用 LLM
        raw = self._call_llm(prompt)

        # 解析输出
        output = self.parse_output(raw, stage_id, inputs)
        try:
            self.validate_output(output)
        except (ValueError, TypeError, KeyError) as exc:
            raise OutputValidationError(str(exc), output) from exc

        # 写入记忆
        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={"inputs": inputs, "output": output},
                tags=[self.__class__.__name__, stage_id],
            )

        return output

    def validate_output(self, output: dict) -> None:
        validate_result(output)
        for key, expected in self.required_fields.items():
            if key not in output or not isinstance(output[key], expected):
                raise ValueError(f"{type(self).__name__}: missing/invalid {key} ({expected.__name__})")
            if expected is str and key not in self.allow_empty_fields and not output[key].strip():
                raise ValueError(f"Empty required field: {key}")

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    def build_prompt(self, stage_id: str, inputs: dict, state: dict) -> str:
        """根据阶段 ID 和输入构建发给 LLM 的 prompt。"""

    @abstractmethod
    def parse_output(self, raw_text: str, stage_id: str, inputs: dict) -> dict:
        """将 LLM 的原始文本解析为结构化 dict，供下游阶段使用。"""

    # ------------------------------------------------------------------
    # 通用 JSON 解析（所有子类共用）
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw_text: str) -> dict:
        """
        从 LLM 输出中提取 JSON，处理以下常见情况：
        1. ```json ... ``` 代码围栏包裹
        2. 裸 JSON（无围栏）
        3. JSON 前后有多余文字
        返回解析后的 dict，失败则返回 {"raw": raw_text, "parse_error": True}
        """
        import json, re

        latex_commands = (
            "alpha|beta|gamma|delta|epsilon|lambda|mu|phi|tau|sigma|nabla|"
            "text|frac|sum|prod|mathcal|left|right|times|cdot|in|cup|emptyset|"
            "sim|ge|le|Pi|begin|end|ref|rightarrow|leftarrow"
        )

        def load_json(candidate: str):
            # Mathematical JSON from models often contains single-backslash
            # LaTeX. Some commands fail JSON parsing (\alpha), while others
            # silently become control characters (\beta, \nabla, \text).
            repaired = re.sub(
                rf"(?<!\\)\\(?=(?:{latex_commands})(?![A-Za-z]))",
                lambda match: "\\" + match.group(0),
                candidate,
            )
            repaired = re.sub(
                r'(?<!\\)\\(?!["\\/bfnrtu])',
                lambda match: "\\" + match.group(0),
                repaired,
            )
            try:
                value = json.loads(repaired)
                return True, value
            except json.JSONDecodeError:
                return False, None

        text = strip_outer_fence(raw_text, ("json",))

        # 1. 剥离 markdown 代码围栏
        fence_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
        if fence_match:
            candidate = fence_match.group(1).strip()
            parsed, value = load_json(candidate)
            if parsed:
                return value if isinstance(value, dict) else {"raw": raw_text, "parse_error": True}

        # 2. 直接尝试整段文本
        parsed, value = load_json(text)
        if parsed:
            return value if isinstance(value, dict) else {"raw": raw_text, "parse_error": True}

        # 3. 提取最外层 {...}（贪婪匹配）
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            parsed, value = load_json(brace_match.group())
            if parsed and isinstance(value, dict):
                return value

        return {"raw": raw_text, "parse_error": True}

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str, system: Optional[str] = None) -> str:
        """调用 Claude API，返回文本响应。"""
        return self._call_llm_with_history([{"role": "user", "content": prompt}], system)

    def _call_llm_with_history(self, messages: list[dict], system: Optional[str] = None) -> str:
        """多轮对话调用。"""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if not self.use_extended_thinking:
            kwargs["temperature"] = self.temperature
        else:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_budget}
            kwargs["temperature"] = 1.0
        started = monotonic()
        logger.info("[%s] model request started: model=%s max_tokens=%s",
                    self.__class__.__name__, self.model, self.max_tokens)
        try:
            result = self._llm.create(**kwargs)
        except Exception as exc:
            logger.error("[%s] model request failed after %.1fs: %s",
                         self.__class__.__name__, monotonic() - started, exc)
            raise
        logger.info("[%s] model response received after %.1fs (%s chars)",
                    self.__class__.__name__, monotonic() - started, len(result))
        return result

    # ------------------------------------------------------------------
    # 工具方法（子类可用）
    # ------------------------------------------------------------------

    def recall(self, topic: str, n: int = 5) -> list[dict]:
        """从记忆中取最近 n 条。"""
        if self.memory is None:
            return []
        return self.memory.get_latest(topic, n)

    def remember(self, topic: str, content: Any, tags: Optional[list[str]] = None) -> None:
        """写入记忆。"""
        if self.memory:
            self.memory.append(topic, content, tags)
