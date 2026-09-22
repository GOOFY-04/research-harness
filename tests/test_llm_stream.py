"""Complete-response semantics over chunked SSE, including real HTTP transport."""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harness.core.llm import LLMClient


def event(content=None, reason=None):
    return {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": reason}]}


def sse(*events):
    return b"".join(("data: " + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False))
                      + "\n\n").encode("utf-8") for e in events)


def test_stream_preserves_unicode_and_requires_complete_finish():
    data = b": keepalive\r\n\r\n" + sse(event("研究"), event("\nreturn 1"),
                                          event(reason="stop"), {"choices": [], "usage": {}}, "[DONE]")
    assert LLMClient()._read_stream(io.BytesIO(data)) == "研究\nreturn 1"


@pytest.mark.parametrize("data, message", [
    (sse(event("partial")), "disconnected"),
    (sse(event("partial"), event(reason="stop")), "disconnected"),
    (sse(event("partial"), "[DONE]"), "missing finish reason"),
    (sse(event("partial"), event(reason="length"), "[DONE]"), "length"),
    (sse(event(reason="content_filter")), "content_filter"),
    (sse(event(reason="stop"), "[DONE]"), "Empty"),
    (sse({"choices": "bad"}), "choices"),
    (sse({"choices": [{"delta": ["wrong"]}]}), "delta"),
    (sse(event(["not text"])), "content"),
])
def test_incomplete_or_malformed_stream_never_returns_partial_output(data, message):
    with pytest.raises(ValueError, match=message):
        LLMClient()._read_stream(io.BytesIO(data))


def test_stream_error_and_elapsed_time_limit(monkeypatch):
    with pytest.raises(RuntimeError, match="provider failed"):
        LLMClient()._read_stream(io.BytesIO(sse({"error": {"message": "provider failed"}})))
    from harness.core import llm
    times = iter([0, 6])
    monkeypatch.setattr(llm, "monotonic", lambda: next(times))
    client = LLMClient()
    client.deadline = 5
    with pytest.raises(TimeoutError, match="elapsed-time"):
        client._read_stream(io.BytesIO(sse(event("late"))))


@pytest.mark.parametrize("streamed", [True, False])
def test_http_adapter_assembles_fragmented_sse_and_accepts_json_fallback(streamed):
    captured = []
    body = (sse(event("研究"), event(" complete"), event(reason="stop"), "[DONE]") if streamed
            else json.dumps({"choices": [{"finish_reason": "stop",
                                         "message": {"content": "研究 complete"}}]}).encode())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if streamed else "application/json")
            self.end_headers()
            for offset in range(0, len(body), 3):
                self.wfile.write(body[offset:offset + 3])
                self.wfile.flush()

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        try:
            client = LLMClient(api_key="test-only", protocol="openai_compatible",
                               base_url=f"http://127.0.0.1:{server.server_port}/v1",
                               stream_responses=True, timeout=5)
            assert client.complete("test streaming") == "研究 complete"
            assert captured[0]["stream"] is True
        finally:
            server.shutdown()
            thread.join(timeout=5)
