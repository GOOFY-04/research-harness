"""Lazy access to Anthropic and OpenAI-compatible chat-completion APIs."""
import json
import logging
import os
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, api_key=None, base_url=None, model=None, max_tokens=8192,
                 timeout=120, client=None, protocol="anthropic", api_key_env=None,
                 stream_responses=False):
        self.api_key = api_key
        self.base_url = base_url
        self.protocol = protocol
        if not isinstance(stream_responses, bool):
            raise ValueError("stream_responses must be a boolean")
        self.stream_responses = stream_responses
        if protocol not in ("anthropic", "openai_compatible"):
            raise ValueError(f"Unsupported LLM protocol: {protocol}")
        default_model = (os.getenv("AGNES_MODEL", "agnes-3.0-flash")
                         if protocol == "openai_compatible"
                         else os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6"))
        self.model = model or default_model
        self.api_key_env = api_key_env or (
            "AGNES_API_KEY" if protocol == "openai_compatible" else "ANTHROPIC_API_KEY"
        )
        self.max_tokens = max_tokens
        self.timeout = timeout
        if timeout <= 0:
            raise ValueError("Model request timeout must be positive")
        self._client = client
        self.deadline = None

    def create(self, **kwargs):
        remaining = self.deadline - monotonic() if self.deadline else self.timeout
        if remaining <= 0:
            raise TimeoutError("Stage deadline exceeded before model call")
        request_timeout = min(remaining, self.timeout)
        if self.protocol == "openai_compatible":
            return self._create_openai_compatible(kwargs, request_timeout)
        kwargs["timeout"] = request_timeout
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise RuntimeError("Install project dependencies: pip install -r requirements.txt") from exc
            key = self.api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
            if not key:
                raise ValueError("Missing ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN")
            options = dict(api_key=key, timeout=self.timeout, max_retries=0)
            url = self.base_url or os.getenv("ANTHROPIC_BASE_URL")
            if url:
                options["base_url"] = url
            self._client = anthropic.Anthropic(**options)
        response = self._client.messages.create(**kwargs)
        if getattr(response, "stop_reason", None) in ("max_tokens", "refusal", "pause_turn"):
            raise ValueError(f"Incomplete model response: {response.stop_reason}")
        text = "\n".join(b.text for b in response.content if b.type == "text")
        if not text.strip():
            raise ValueError("Empty model response")
        return text

    def _create_openai_compatible(self, kwargs, timeout):
        key = self.api_key or os.getenv(self.api_key_env)
        if not key:
            raise ValueError(f"Missing API key environment variable: {self.api_key_env}")
        base_url = (self.base_url or os.getenv("AGNES_API_BASE")
                    or "https://apihub.agnes-ai.com/v1").rstrip("/")
        messages = list(kwargs.get("messages", []))
        if kwargs.get("system"):
            messages.insert(0, {"role": "system", "content": kwargs["system"]})
        payload = {
            "model": kwargs.get("model", self.model),
            "messages": messages,
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "stream": self.stream_responses,
        }
        for name in ("temperature", "top_p", "stop", "seed", "frequency_penalty",
                     "presence_penalty", "tools", "tool_choice"):
            if name in kwargs:
                payload[name] = kwargs[name]
        request = Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "text/event-stream" if self.stream_responses else "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if self.stream_responses and "text/event-stream" in response.headers.get("Content-Type", ""):
                    return self._read_stream(response)
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                parsed = json.loads(detail)
                detail = parsed.get("error", {}).get("message", detail)
            except (ValueError, AttributeError):
                pass
            raise RuntimeError(f"LLM API HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM API connection failed: {exc.reason}") from exc
        try:
            choice = body["choices"][0]
            finish_reason = choice.get("finish_reason")
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid OpenAI-compatible response shape") from exc
        if finish_reason in ("length", "content_filter"):
            raise ValueError(f"Incomplete model response: {finish_reason}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Empty model response")
        return text

    def _read_stream(self, response):
        """Assemble complete SSE output; never publish a disconnected partial response."""
        started = monotonic()
        deadline = min(self.deadline or float("inf"), started + 360)
        chunks, event_lines = [], []
        size = event_size = 0
        finish_reason = None
        last_progress = None
        for raw in response:
            now = monotonic()
            if now > deadline:
                raise TimeoutError("Model stream exceeded its elapsed-time limit")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data = line[5:].lstrip(" ")
                event_size += len(data)
                if event_size > 2_000_000:
                    raise ValueError("Model stream event exceeded the size limit")
                event_lines.append(data)
                continue
            if line or not event_lines:
                continue
            data = "\n".join(event_lines)
            event_lines, event_size = [], 0
            if data == "[DONE]":
                if finish_reason != "stop":
                    raise ValueError(f"Incomplete model stream: {finish_reason or 'missing finish reason'}")
                text = "".join(chunks)
                if not text.strip():
                    raise ValueError("Empty model response")
                return text
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError("Invalid model stream event")
            if event.get("error"):
                raise RuntimeError(f"LLM stream error: {str(event['error'])[:500]}")
            choices = event.get("choices")
            if not isinstance(choices, list):
                raise ValueError("Invalid model stream choices")
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError("Invalid model stream choice")
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ValueError("Invalid model stream delta")
                content = delta.get("content")
                if content is not None:
                    if not isinstance(content, str):
                        raise ValueError("Invalid model stream content")
                    chunks.append(content)
                    size += len(content)
                    if size > 8_000_000:
                        raise ValueError("Model stream response exceeded the size limit")
                    if content and (last_progress is None or now - last_progress >= 10):
                        logger.info("[LLM stream] received %s chars after %.1fs", size, now - started)
                        last_progress = now
                reason = choice.get("finish_reason")
                if reason is not None:
                    if reason != "stop":
                        raise ValueError(f"Incomplete model response: {reason}")
                    finish_reason = reason
        raise ValueError("Incomplete model stream: disconnected before [DONE]")

    def complete(self, prompt):
        return self.create(model=self.model, max_tokens=self.max_tokens,
                           messages=[{"role": "user", "content": prompt}])
