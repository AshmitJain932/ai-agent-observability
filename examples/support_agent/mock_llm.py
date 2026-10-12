"""A scripted, deterministic stand-in for an OpenAI-compatible chat model.

It plugs into the *real* ``openai`` client through an HTTP mock transport, so the example and the
tests exercise the same code path as live mode — request building, response parsing, error
classes — without network access or credentials.

Script: ask for ``get_order_status`` for the order id in the question; if the question mentions a
refund, then ask for ``issue_refund``; then answer in text. Usage is reported like the real API,
unless ``report_usage=False`` (to demonstrate unknown usage).
"""

from __future__ import annotations

import json
import re
from typing import Any

try:  # openai >= 3 uses httpx2; openai 1.x/2.x uses httpx
    import httpx2 as httpx_mod
except ImportError:  # pragma: no cover
    import httpx as httpx_mod  # type: ignore[no-redef]

from openai import OpenAI


def _usage(prompt: int, completion: int) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


class ScriptedModel:
    def __init__(self, report_usage: bool = True) -> None:
        self.report_usage = report_usage
        self.calls = 0

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        messages = body["messages"]
        question = next(m["content"] for m in messages if m["role"] == "user")
        called = [m for m in messages if m["role"] == "assistant" and m.get("tool_calls")]
        tool_results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        order_id = (re.search(r"\b(\d{4})\b", question) or re.search(r"(\d+)", question) or [None, "0"])[1]

        if not called:
            message = self._tool_call("get_order_status", {"order_id": order_id}, 1)
        elif "refund" in question.lower() and len(called) == 1 and "amount" in tool_results[-1]:
            message = self._tool_call("issue_refund", {"order_id": order_id, "amount": tool_results[-1]["amount"]}, 2)
        else:
            last = tool_results[-1]
            text = (
                f"Your refund {last['reference']} was issued."
                if "reference" in last
                else f"Order {order_id} is {last.get('status', 'unknown')}."
            )
            message = {"role": "assistant", "content": text}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        response: dict[str, Any] = {
            "id": f"chatcmpl-mock-{self.calls}",
            "object": "chat.completion",
            "created": 1_760_000_000,
            "model": f"{body['model']}-2026-01-01",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        }
        if self.report_usage:
            prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4 + 20
            response["usage"] = _usage(prompt_tokens, 12)
        return response

    @staticmethod
    def _tool_call(name: str, arguments: dict[str, Any], n: int) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            ],
        }

    def handler(self, request: Any) -> Any:
        body = json.loads(request.content)
        return httpx_mod.Response(200, json=self.respond(body), headers={"x-request-id": f"req_mock_{self.calls}"})


def mock_client(report_usage: bool = True) -> OpenAI:
    model = ScriptedModel(report_usage=report_usage)
    return OpenAI(
        api_key="mock-key-not-used",
        base_url="http://mock-llm.invalid/v1",
        http_client=httpx_mod.Client(transport=httpx_mod.MockTransport(model.handler)),
        max_retries=0,
    )
