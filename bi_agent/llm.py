"""provider选择、统一消息、工具回合、超时及错误转换。

一层薄适配：qwen/deepseek差异收敛在同文件的地址映射常量；
不默认附加任何一家独有的strict/response_format/思考模式参数。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field, PrivateAttr

from .config import ModelSettings

logger = logging.getLogger(__name__)

# 执行时需按账号地域核对；核验日期2026-09-07，记录见docs/runbook.md
DEFAULT_BASE_URLS: dict[str, str] = {
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
MAX_BODY_BYTES = 2 * 1024 * 1024


class ModelError(Exception):
    """脱敏类别错误：authentication/rate_limit/timeout/unavailable/invalid_response。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, object] | None
    arguments_error: str | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    provider_context: dict[str, object] = Field(
        default_factory=dict, exclude=True, repr=False)


class ModelReply(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int | None] | None = None
    _message: Message = PrivateAttr()

    def as_message(self) -> Message:
        return self._message


class ChatModel(Protocol):
    def complete(self, messages: list[Message], tools: list[dict[str, object]],
                 *, timeout_s: float) -> ModelReply:
        raise NotImplementedError  # 类型协议；不建立抽象基类继承体系


def _parse_arguments(raw: object) -> tuple[dict[str, object] | None, str | None, str]:
    """解析工具参数：合法JSON object返回dict；非法返回None和短错误类别。"""
    if raw is None:
        return None, None, "{}"
    if isinstance(raw, dict):
        return raw, None, json.dumps(raw, ensure_ascii=False)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None, "invalid_json", raw
        if not isinstance(parsed, dict):
            return None, "not_object", raw
        return parsed, None, raw
    return None, "invalid_json", json.dumps(raw)


def _decode_reply(payload: dict[str, Any]) -> ModelReply:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError("invalid_response")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ModelError("invalid_response")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelError("invalid_response")
    text = message.get("content")
    if text is not None and not isinstance(text, str):
        raise ModelError("invalid_response")
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ModelError("invalid_response")
    tool_calls: list[ToolCall] = []
    raw_arguments: dict[str, str] = {}
    seen_ids: set[str] = set()
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise ModelError("invalid_response")
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise ModelError("invalid_response")
        call_id = raw_call.get("id")
        name = function.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ModelError("invalid_response")
        if call_id in seen_ids:
            raise ModelError("invalid_response")
        seen_ids.add(call_id)
        arguments, arguments_error, raw_args = _parse_arguments(function.get("arguments"))
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments,
                                   arguments_error=arguments_error))
        raw_arguments[call_id] = raw_args
    reasoning = message.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ModelError("invalid_response")
    provider_context: dict[str, object] = {}
    if reasoning:
        provider_context["reasoning_content"] = reasoning
    if raw_arguments:
        provider_context["raw_arguments"] = raw_arguments
    assistant = Message(role="assistant", content=text, tool_calls=tool_calls,
                        provider_context=provider_context)
    usage_raw = payload.get("usage")
    usage: dict[str, int | None] | None = None
    if isinstance(usage_raw, dict):
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens") if isinstance(usage_raw.get("prompt_tokens"), int) else None,
            "completion_tokens": usage_raw.get("completion_tokens") if isinstance(usage_raw.get("completion_tokens"), int) else None,
            "total_tokens": usage_raw.get("total_tokens") if isinstance(usage_raw.get("total_tokens"), int) else None,
        }
    reply = ModelReply(text=text, tool_calls=tool_calls, usage=usage)
    reply._message = assistant
    return reply


def _encode_messages(messages: list[Message]) -> list[dict[str, object]]:
    """从Message公开字段创建标准协议消息；额外上下文仅合并适配层认可字段。"""
    encoded: list[dict[str, object]] = []
    for message in messages:
        if message.role == "tool":
            encoded.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content or "",
            })
            continue
        item: dict[str, object] = {"role": message.role}
        if message.content is not None:
            item["content"] = message.content
        if message.role == "assistant" and message.tool_calls:
            raw_arguments = message.provider_context.get("raw_arguments")
            raw_map = raw_arguments if isinstance(raw_arguments, dict) else {}
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": raw_map.get(call.id)
                        or json.dumps(call.arguments or {}, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        reasoning = message.provider_context.get("reasoning_content")
        if message.role == "assistant" and isinstance(reasoning, str) and reasoning:
            item["reasoning_content"] = reasoning
        encoded.append(item)
    return encoded


class CompatibleChatModel:
    """唯一的ChatModel实现；transport只供mock。"""

    def __init__(self, settings: ModelSettings, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    @property
    def endpoint(self) -> str:
        base = self.settings.base_url or DEFAULT_BASE_URLS[self.settings.provider]
        return base.rstrip("/") + "/chat/completions"

    def complete(self, messages: list[Message], tools: list[dict[str, object]],
                 *, timeout_s: float) -> ModelReply:
        # 总时限由asyncio.wait_for限制，避免把httpx各阶段timeout误当总时限
        try:
            return asyncio.run(asyncio.wait_for(
                self._request(messages, tools), timeout=timeout_s))
        except TimeoutError:
            raise ModelError("timeout") from None

    async def _request(self, messages: list[Message],
                       tools: list[dict[str, object]]) -> ModelReply:
        body: dict[str, object] = {
            "model": self.settings.model,
            "messages": _encode_messages(messages),
        }
        if tools:
            body["tools"] = tools
        # 不默认附加strict/response_format/思考模式参数
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                timeout=httpx.Timeout(30.0),
            ) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.TimeoutException:
            raise ModelError("timeout") from None
        except httpx.TransportError:
            raise ModelError("unavailable") from None
        if response.status_code in (401, 403):
            raise ModelError("authentication")
        if response.status_code == 429:
            raise ModelError("rate_limit")
        if response.status_code >= 500:
            logger.warning("model server error status=%s", response.status_code)
            raise ModelError("unavailable")
        if response.status_code != 200:
            raise ModelError("unavailable")
        if response.headers.get("content-length", "").isdigit() and \
                int(response.headers["content-length"]) > MAX_BODY_BYTES:
            raise ModelError("invalid_response")
        content = response.content
        if len(content) > MAX_BODY_BYTES:
            raise ModelError("invalid_response")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            raise ModelError("invalid_response") from None
        if not isinstance(payload, dict):
            raise ModelError("invalid_response")
        return _decode_reply(payload)


def create_model(settings: ModelSettings) -> ChatModel:
    """启动时按配置选择provider；无自动切换和动态路由。"""
    if settings.provider not in DEFAULT_BASE_URLS:
        raise ModelError("invalid_response")
    return CompatibleChatModel(settings)
