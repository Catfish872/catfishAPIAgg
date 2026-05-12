import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


class AnthropicAdapterError(Exception):
    """Anthropic 协议适配错误。"""


def anthropic_error(message: str, error_type: str = "invalid_request_error") -> Dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message
        }
    }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _anthropic_image_to_openai_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "url" and isinstance(source.get("url"), str):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    if source_type == "base64" and isinstance(source.get("data"), str):
        media_type = source.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{source['data']}"}}
    return None


def anthropic_content_to_openai_content(content: Any) -> Any:
    """Anthropic content -> OpenAI message content。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _as_text(content)

    openai_blocks: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                openai_blocks.append({"type": "text", "text": text})
                text_parts.append(text)
        elif block_type == "image":
            image_block = _anthropic_image_to_openai_block(block)
            if image_block:
                openai_blocks.append(image_block)
        elif block_type == "tool_use":
            # Anthropic assistant tool_use 请求历史转成可读文本，真正的 tool_calls 由 assistant 响应侧处理。
            name = block.get("name") or "tool"
            input_obj = block.get("input", {})
            openai_blocks.append({"type": "text", "text": f"[tool_use:{name}] {json.dumps(input_obj, ensure_ascii=False)}"})
        elif block_type == "tool_result":
            text = extract_text_from_anthropic_content(block.get("content"))
            openai_blocks.append({"type": "text", "text": text})

    if not openai_blocks:
        return ""
    if all(item.get("type") == "text" for item in openai_blocks):
        return "\n".join(text_parts)
    return openai_blocks


def extract_text_from_anthropic_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return _as_text(content)


def anthropic_tools_to_openai_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tools, list):
        return None
    result: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}}
            }
        })
    return result or None


def anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool" and isinstance(tool_choice.get("name"), str):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return None


def anthropic_to_openai_chat_request(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise AnthropicAdapterError("Anthropic request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise AnthropicAdapterError("model is required")

    openai_body: Dict[str, Any] = {"model": model}
    messages: List[Dict[str, Any]] = []

    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        system_text = extract_text_from_anthropic_content(system)
        if system_text.strip():
            messages.append({"role": "system", "content": system_text})

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise AnthropicAdapterError("messages must be an array")

    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            # Anthropic tool_result 放在 user 消息里；尽量转为 OpenAI tool role，否则作为普通 user 文本。
            if isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for block in content:
                    tool_call_id = block.get("tool_use_id") or f"toolu_{uuid.uuid4().hex}"
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": extract_text_from_anthropic_content(block.get("content"))})
            else:
                messages.append({"role": "user", "content": anthropic_content_to_openai_content(content)})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": anthropic_content_to_openai_content(content)})

    openai_body["messages"] = messages

    field_map = {
        "max_tokens": "max_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "metadata": "metadata",
    }
    for anthropic_key, openai_key in field_map.items():
        if anthropic_key in body:
            openai_body[openai_key] = body[anthropic_key]
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]
    if "stream" in body:
        openai_body["stream"] = bool(body.get("stream"))

    tools = anthropic_tools_to_openai_tools(body.get("tools"))
    if tools:
        openai_body["tools"] = tools
    tool_choice = anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if tool_choice is not None:
        openai_body["tool_choice"] = tool_choice

    # 透传图片预设常用字段，便于 Anthropic 入口也能走 images_generations。
    for key in ["size", "image_size", "resolution", "n", "response_format", "quality", "style", "user", "upscale", "prompt"]:
        if key in body:
            openai_body[key] = body[key]

    return openai_body


def _usage_openai_to_anthropic(usage: Any) -> Dict[str, int]:
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    }


def _openai_message_to_anthropic_content(message: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    content_blocks: List[Dict[str, Any]] = []
    stop_reason = "end_turn"
    content = message.get("content")
    if isinstance(content, str) and content:
        content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        text_parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        if text_parts:
            content_blocks.append({"type": "text", "text": "\n".join(text_parts)})

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = fn.get("name") or call.get("name") or "tool"
            raw_args = fn.get("arguments") or "{}"
            try:
                input_obj = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                input_obj = {"arguments": raw_args}
            content_blocks.append({
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
                "name": name,
                "input": input_obj if isinstance(input_obj, dict) else {"value": input_obj}
            })
            stop_reason = "tool_use"

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})
    return content_blocks, stop_reason


def openai_chat_to_anthropic_response(openai_payload: Dict[str, Any], original_body: Dict[str, Any]) -> Dict[str, Any]:
    choices = openai_payload.get("choices") if isinstance(openai_payload, dict) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) and isinstance(first_choice.get("message"), dict) else {}
    content, inferred_stop = _openai_message_to_anthropic_content(message)
    finish_reason = first_choice.get("finish_reason") if isinstance(first_choice, dict) else None
    stop_reason = inferred_stop
    if finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "stop":
        stop_reason = inferred_stop

    return {
        "id": openai_payload.get("id") or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": openai_payload.get("model") or original_body.get("model"),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _usage_openai_to_anthropic(openai_payload.get("usage"))
    }


def openai_error_to_anthropic(payload: Any, status_code: int = 500) -> Tuple[Dict[str, Any], int]:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = err.get("message") or str(err)
            error_type = err.get("type") or "api_error"
            return anthropic_error(message, error_type), status_code
        if isinstance(err, str):
            return anthropic_error(err, "api_error"), status_code
    return anthropic_error(str(payload), "api_error"), status_code


def _sse_event(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class OpenAIToAnthropicSSEConverter:
    """有状态 OpenAI Chat SSE -> Anthropic Messages SSE 转换器，支持正文流和工具调用流。"""

    def __init__(self, original_body: Dict[str, Any]):
        self.original_body = original_body
        self.next_content_index = 0
        self.text_block_index: Optional[int] = None
        self.text_block_open = False
        self.tool_blocks: Dict[int, Dict[str, Any]] = {}
        self.finished = False

    def start_events(self) -> List[bytes]:
        message = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": self.original_body.get("model"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}
        }
        return [_sse_event("message_start", {"type": "message_start", "message": message})]

    def _ensure_text_block(self) -> List[bytes]:
        if self.text_block_open:
            return []
        if self.text_block_index is None:
            self.text_block_index = self.next_content_index
            self.next_content_index += 1
        self.text_block_open = True
        return [_sse_event("content_block_start", {"type": "content_block_start", "index": self.text_block_index, "content_block": {"type": "text", "text": ""}})]

    def _close_text_block(self) -> List[bytes]:
        if not self.text_block_open or self.text_block_index is None:
            return []
        self.text_block_open = False
        return [_sse_event("content_block_stop", {"type": "content_block_stop", "index": self.text_block_index})]

    def _ensure_tool_block(self, openai_index: int, call: Dict[str, Any]) -> List[bytes]:
        if openai_index in self.tool_blocks:
            return []
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        tool_id = call.get("id") or f"toolu_{uuid.uuid4().hex}"
        tool_name = fn.get("name") or call.get("name") or "tool"
        block_index = self.next_content_index
        self.next_content_index += 1
        self.tool_blocks[openai_index] = {"content_index": block_index, "id": tool_id, "name": tool_name, "open": True}
        return [_sse_event("content_block_start", {"type": "content_block_start", "index": block_index, "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}}})]

    def _close_tool_blocks(self) -> List[bytes]:
        events: List[bytes] = []
        for state in self.tool_blocks.values():
            if state.get("open"):
                state["open"] = False
                events.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": state["content_index"]}))
        return events

    def _extract_message_text(self, message: Dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "\n".join(parts)
        return ""

    def feed_line(self, line_text: str) -> List[bytes]:
        if self.finished:
            return []
        line_text = line_text.strip()
        if not line_text.startswith("data:"):
            return []
        payload = line_text[5:].strip()
        if not payload or payload == "[DONE]":
            return []
        try:
            obj = json.loads(payload)
        except Exception:
            return []
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not isinstance(choices, list) or not choices:
            return []
        choice = choices[0]
        if not isinstance(choice, dict):
            return []

        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        events: List[bytes] = []

        text = delta.get("content")
        if not isinstance(text, str) or not text:
            text = self._extract_message_text(message)
        if isinstance(text, str) and text:
            events.extend(self._ensure_text_block())
            events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": self.text_block_index, "delta": {"type": "text_delta", "text": text}}))

        tool_calls = delta.get("tool_calls")
        if not isinstance(tool_calls, list):
            tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if isinstance(tool_calls, list):
            for fallback_index, call in enumerate(tool_calls):
                if not isinstance(call, dict):
                    continue
                openai_index = call.get("index") if isinstance(call.get("index"), int) else fallback_index
                # 若正文 block 正在输出，工具 block 前先关闭正文 block，符合 Anthropic content_block 顺序。
                events.extend(self._close_text_block())
                events.extend(self._ensure_tool_block(openai_index, call))
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                arguments = fn.get("arguments")
                state = self.tool_blocks.get(openai_index)
                if isinstance(arguments, str) and arguments and state:
                    events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": state["content_index"], "delta": {"type": "input_json_delta", "partial_json": arguments}}))

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            events.extend(self._close_text_block())
            events.extend(self._close_tool_blocks())
            stop_reason = "max_tokens" if finish_reason == "length" else "tool_use" if finish_reason == "tool_calls" else "end_turn"
            events.append(_sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}}))
            events.append(_sse_event("message_stop", {"type": "message_stop"}))
            self.finished = True
        return events

    def finish_events(self) -> List[bytes]:
        if self.finished:
            return []
        self.finished = True
        events: List[bytes] = []
        events.extend(self._close_text_block())
        events.extend(self._close_tool_blocks())
        events.append(_sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}}))
        events.append(_sse_event("message_stop", {"type": "message_stop"}))
        return events


def anthropic_stream_start(original_body: Dict[str, Any]) -> List[bytes]:
    """兼容旧调用：仅返回 message_start，不再预先打开 text block。"""
    return OpenAIToAnthropicSSEConverter(original_body).start_events()


def openai_sse_line_to_anthropic_events(line_text: str) -> List[bytes]:
    """兼容旧调用的无状态包装；复杂场景应使用 OpenAIToAnthropicSSEConverter。"""
    converter = OpenAIToAnthropicSSEConverter({})
    events = converter.start_events()
    events.extend(converter.feed_line(line_text))
    if not converter.finished:
        events.extend(converter.finish_events())
    return events


def openai_json_to_anthropic_stream(openai_payload: Dict[str, Any], original_body: Dict[str, Any]) -> List[bytes]:
    response = openai_chat_to_anthropic_response(openai_payload, original_body)
    events = [
        _sse_event("message_start", {"type": "message_start", "message": {**response, "content": []}})
    ]
    for index, block in enumerate(response.get("content", [])):
        events.append(_sse_event("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": block.get("type"), "text": ""} if block.get("type") == "text" else block}))
        if block.get("type") == "text":
            events.append(_sse_event("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block.get("text", "")}}))
        events.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(_sse_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": response.get("stop_reason"), "stop_sequence": None}, "usage": response.get("usage", {"output_tokens": 0})}))
    events.append(_sse_event("message_stop", {"type": "message_stop"}))
    return events
