"""
BaseAgent — 所有专职 agent 的抽象基类

子类只需实现 build_prompt() 和 parse_output()，
BaseAgent 负责调用 Claude API、重试、记忆读写、日志。
"""

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import anthropic

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

from .memory import MemoryStore

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
    enable_thinking: bool = False  # OpenAI 兼容 API (agnes) 的 thinking 模式

    def __init__(
        self,
        memory: Optional[MemoryStore] = None,
        api_key: Optional[str] = None,
        use_extended_thinking: Optional[bool] = None,
        thinking_budget: int = 5000,
    ):
        self.memory = memory
        # 参数未传入时回退到类属性值（子类可覆盖类属性作为默认值）
        if use_extended_thinking is None:
            use_extended_thinking = getattr(self.__class__, "use_extended_thinking", False)
        self.use_extended_thinking = use_extended_thinking
        self.thinking_budget = thinking_budget
        # OpenAI API 的 thinking 模式：优先读取类属性，可通过环境变量全局开启
        self.enable_thinking = (
            getattr(self.__class__, "enable_thinking", False)
            or os.environ.get("ENABLE_THINKING", "").lower() == "true"
        )
        self._conversations: list[dict] = []

        # 检查是否使用 OpenAI 兼容 API
        use_openai = os.environ.get("USE_OPENAI_API", "false").lower() == "true"

        if use_openai:
            if not HAS_OPENAI:
                raise ImportError("openai package not installed. Run: pip install openai")
            # OpenAI 兼容 API 配置
            api_key = (
                api_key
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
            )
            base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
            timeout_seconds = float(os.environ.get("ANTHROPIC_TIMEOUT", "300"))

            self._client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
            self._api_type = "openai"

            # Anthropic extended_thinking 映射到 OpenAI API 的 enable_thinking
            if self.use_extended_thinking:
                self.enable_thinking = True

            # 从环境变量读取模型名称
            self.model = os.environ.get("OPENAI_MODEL", self.model)
        else:
            # Anthropic API 配置
            resolved_key = (
                api_key
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            )
            base_url = os.environ.get("ANTHROPIC_BASE_URL")
            client_kwargs: dict[str, Any] = {"api_key": resolved_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            # 增加超时时间，适配代理/镜像站的较高延迟
            timeout_seconds = float(os.environ.get("ANTHROPIC_TIMEOUT", "300"))
            client_kwargs["timeout"] = anthropic.Timeout(timeout_seconds, connect=30.0)
            self._client = anthropic.Anthropic(**client_kwargs)
            self._api_type = "anthropic"

            # 从环境变量读取模型别名（允许在 .env 中统一覆盖）
            self.model = (
                os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", self.model)
                if "sonnet" in self.model
                else os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", self.model)
                if "opus" in self.model
                else self.model
            )

    # ------------------------------------------------------------------
    # 主接口（由 WorkflowEngine 调用）
    # ------------------------------------------------------------------

    def run(self, stage_id: str, inputs: dict, state: dict) -> dict:
        """执行一个阶段，返回结构化输出。"""
        logger.info(f"[{self.__class__.__name__}] stage={stage_id}")

        # 构建 prompt
        prompt = self.build_prompt(stage_id, inputs, state)

        # 调用 LLM
        raw = self._call_llm(prompt)

        # 解析输出
        output = self.parse_output(raw, stage_id, inputs)

        # 写入记忆
        if self.memory:
            self.memory.append(
                topic=stage_id,
                content={"inputs": inputs, "output": output},
                tags=[self.__class__.__name__, stage_id],
            )

        return output

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
        text = raw_text.strip()

        # 1. 剥离 markdown 代码围栏
        fence_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", text)
        if fence_match:
            candidate = fence_match.group(1).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        # 2. 直接尝试整段文本
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 3. 提取最外层 {...}（贪婪匹配）
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

        return {"raw": raw_text, "parse_error": True}

    # ------------------------------------------------------------------
    # 对话记录管理（由 WorkflowEngine 调用）
    # ------------------------------------------------------------------

    def clear_conversations(self) -> None:
        """清空对话记录缓存（每个 stage 开始前调用）。"""
        self._conversations = []

    def save_conversations(self, session_dir: str, stage_id: str) -> None:
        """将本轮对话记录写入 session 目录。"""
        if not session_dir or not self._conversations:
            return
        conv_dir = Path(session_dir) / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        conv_file = conv_dir / f"{stage_id}.json"
        existing: list[dict] = []
        if conv_file.exists():
            try:
                existing = json.loads(conv_file.read_text("utf-8"))
            except json.JSONDecodeError:
                pass
        existing.extend(self._conversations)
        conv_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), "utf-8")
        logger.info(f"[{self.__class__.__name__}] 对话记录已保存: {conv_file}")

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str, system: Optional[str] = None) -> str:
        """调用 LLM API，返回文本响应。支持 Anthropic 和 OpenAI 兼容 API。"""
        t_start = time.time()

        if self._api_type == "openai":
            # OpenAI 兼容 API 调用
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            create_kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }
            # agnes-2.0-flash thinking 模式
            if self.enable_thinking:
                create_kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": True}
                }

            response = self._client.chat.completions.create(**create_kwargs)
            result = response.choices[0].message.content
            elapsed = time.time() - t_start

            # 记录对话
            self._record_conversation(
                model=self.model,
                messages=messages,
                response=result,
                elapsed=elapsed,
                usage=response.usage,
            )
        else:
            # Anthropic API 调用
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            if self.use_extended_thinking:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                }
                kwargs["temperature"] = 1.0  # extended thinking 必须为 1.0
            else:
                kwargs["temperature"] = self.temperature

            response = self._client.messages.create(**kwargs)
            elapsed = time.time() - t_start

            # 提取文本块（跳过 thinking 块）
            text_parts = [
                block.text
                for block in response.content
                if block.type == "text"
            ]
            result = "\n".join(text_parts)

            # 记录对话
            self._record_conversation(
                model=kwargs["model"],
                messages=kwargs["messages"],
                response=result,
                elapsed=elapsed,
                usage=getattr(response, "usage", None),
            )

        return result

    def _call_llm_with_history(self, messages: list[dict], system: Optional[str] = None) -> str:
        """多轮对话调用。支持 Anthropic 和 OpenAI 兼容 API。"""
        t_start = time.time()

        if self._api_type == "openai":
            # OpenAI 兼容 API 调用
            openai_messages = []
            if system:
                openai_messages.append({"role": "system", "content": system})
            openai_messages.extend(messages)

            create_kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": openai_messages,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }
            if self.enable_thinking:
                create_kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": True}
                }

            response = self._client.chat.completions.create(**create_kwargs)
            result = response.choices[0].message.content
            elapsed = time.time() - t_start

            # 记录对话
            self._record_conversation(
                model=self.model,
                messages=openai_messages,
                response=result,
                elapsed=elapsed,
                usage=response.usage,
            )
        else:
            # Anthropic API 调用
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if self.use_extended_thinking:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget,
                }
                kwargs["temperature"] = 1.0
            else:
                kwargs["temperature"] = self.temperature

            response = self._client.messages.create(**kwargs)
            elapsed = time.time() - t_start

            text_parts = [b.text for b in response.content if b.type == "text"]
            result = "\n".join(text_parts)

            # 记录对话
            self._record_conversation(
                model=kwargs["model"],
                messages=messages,
                response=result,
                elapsed=elapsed,
                usage=getattr(response, "usage", None),
            )

        return result

    def _record_conversation(
        self,
        model: str,
        messages: list[dict],
        response: str,
        elapsed: float,
        usage: Any = None,
    ) -> None:
        """记录一次 LLM 对话。"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "elapsed_seconds": round(elapsed, 2),
            "messages": messages,
            "response": response,
        }
        if usage is not None:
            record["usage"] = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            }
        if not hasattr(self, "_conversations"):
            self._conversations = []
        self._conversations.append(record)

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
