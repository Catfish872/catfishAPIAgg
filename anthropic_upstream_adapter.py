import copy
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from anthropic_adapter import AnthropicAdapterError, _as_text, extract_text_from_anthropic_content


DEFAULT_ANTHROPIC_MAX_TOKENS = 32768


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _copy_extra_block_fields(source: Dict[str, Any], target: Dict[str, Any]):
    """复制 Anthropic/OpenAI 多模态块上的扩展字段，重点保留 cache_control。"""
    for key, value in source.items():
        if key not in target and key not in {"type", "text", "image_url", "url", "source", "detail"}:
            target[key] = copy.deepcopy(value)


def _data_url_to_anthropic_source(url: str) -> Optional[Dict[str, Any]]:
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        header, data = url.split(",", 1)
        media_type = header[5:].split(";", 1)[0] or "image/png"
        if ";base64" not in header:
            return None
        return {"type": "base64", "media_type": media_type, "data": data}
    except Exception:
        return None


def _openai_image_url_to_anthropic_block(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    image_url = item.get("image_url")
    url = None
    if isinstance(image_url, dict):
        url = image_url.get("url")
    elif isinstance(image_url, str):
        url = image_url
    elif isinstance(item.get("url"), str):
        url = item.get("url")

    if not _is_non_empty_str(url):
        return None

    data_source = _data_url_to_anthropic_source(url.strip())
    block = {"type": "image", "source": data_source or {"type": "url", "url": url.strip()}}
    _copy_extra_block_fields(item, block)
    if isinstance(image_url, dict):
        _copy_extra_block_fields(image_url, block)
    return block


def openai_content_to_anthropic_blocks(content: Any) -> List[Dict[str, Any]]:
    """OpenAI message content -> Anthropic content blocks，尽量保留 cache_control 等扩展字段。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": _as_text(content)}]

    blocks: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"text", "input_text"}:
            block = {"type": "text", "text": _as_text(item.get("text"))}
            _copy_extra_block_fields(item, block)
            blocks.append(block)
        elif item_type in {"image_url", "input_image", "image"}:
            block = _openai_image_url_to_anthropic_block(item)
            if block:
                blocks.append(block)
        elif item_type == "tool_result":
            block = {
                "type": "tool_result",
                "tool_use_id": item.get("tool_use_id") or item.get("tool_call_id") or f"toolu_{uuid.uuid4().hex}",
                "content": item.get("content") if item.get("content") is not None else "",
            }
            _copy_extra_block_fields(item, block)
            blocks.append(block)
        else:
            # 未知 block 透传，最大限度保留第三方扩展能力。
            blocks.append(copy.deepcopy(item))
    return blocks or [{"type": "text", "text": ""}]


def _openai_tool_call_to_anthropic_block(call: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(call, dict):
        return None
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = fn.get("name") or call.get("name")
    if not _is_non_empty_str(name):
        return None
    raw_args = fn.get("arguments") if "arguments" in fn else call.get("input", {})
    try:
        input_obj = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else raw_args
    except Exception:
        input_obj = {"arguments": raw_args}
    if not isinstance(input_obj, dict):
        input_obj = {"value": input_obj}
    block = {
        "type": "tool_use",
        "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
        "name": name,
        "input": input_obj,
    }
    _copy_extra_block_fields(call, block)
    return block


def _openai_message_to_anthropic_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in {"user", "assistant", "tool"}:
        return None

    if role == "tool":
        content = message.get("content")
        block = {
            "type": "tool_result",
            "tool_use_id": message.get("tool_call_id") or message.get("tool_use_id") or f"toolu_{uuid.uuid4().hex}",
            "content": content if content is not None else "",
        }
        if "is_error" in message:
            block["is_error"] = bool(message.get("is_error"))
        return {"role": "user", "content": [block]}

    content_blocks = openai_content_to_anthropic_blocks(message.get("content"))
    if role == "assistant":
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                block = _openai_tool_call_to_anthropic_block(call)
                if block:
                    content_blocks.append(block)

    return {"role": role, "content": content_blocks}


def _merge_adjacent_same_role_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Anthropic 不接受连续相同 role 的 messages；转换 OpenAI 历史时合并相邻同角色消息。"""
    merged: List[Dict[str, Any]] = []
    for msg in messages:
        if not merged or merged[-1].get("role") != msg.get("role"):
            merged.append(msg)
            continue
        prev_content = merged[-1].setdefault("content", [])
        current_content = msg.get("content")
        if not isinstance(prev_content, list):
            prev_content = [{"type": "text", "text": _as_text(prev_content)}]
            merged[-1]["content"] = prev_content
        if isinstance(current_content, list):
            prev_content.extend(current_content)
        else:
            prev_content.append({"type": "text", "text": _as_text(current_content)})
    return merged


def openai_tools_to_anthropic_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tools, list):
        return None
    result: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            name = fn.get("name")
            if not _is_non_empty_str(name):
                continue
            item = {
                "name": name,
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            _copy_extra_block_fields(tool, item)
            result.append(item)
        elif _is_non_empty_str(tool.get("name")):
            result.append(copy.deepcopy(tool))
    return result or None


def openai_tool_choice_to_anthropic(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return {"type": "none"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function" and isinstance(tool_choice.get("function"), dict):
            name = tool_choice["function"].get("name")
            if _is_non_empty_str(name):
                return {"type": "tool", "name": name}
        if tool_choice.get("type") in {"auto", "any", "tool", "none"}:
            return copy.deepcopy(tool_choice)
    return None


def openai_chat_to_anthropic_request(body: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI Chat Completions 请求 -> Anthropic Messages 请求。"""
    if not isinstance(body, dict):
        raise AnthropicAdapterError("OpenAI request body must be a JSON object")
    model = body.get("model")
    if not _is_non_empty_str(model):
        raise AnthropicAdapterError("model is required")

    result: Dict[str, Any] = {}
    passthrough_keys = [
        "metadata", "temperature", "top_p", "top_k", "stream", "service_tier",
        "thinking", "container", "mcp_servers", "context_management",
    ]
    for key in passthrough_keys:
        if key in body:
            result[key] = copy.deepcopy(body[key])
    result["model"] = model

    if "max_tokens" in body and body.get("max_tokens") is not None:
        result["max_tokens"] = body.get("max_tokens")
    elif "max_completion_tokens" in body and body.get("max_completion_tokens") is not None:
        result["max_tokens"] = body.get("max_completion_tokens")
    else:
        result["max_tokens"] = DEFAULT_ANTHROPIC_MAX_TOKENS

    if "stop" in body:
        result["stop_sequences"] = body.get("stop") if isinstance(body.get("stop"), list) else [body.get("stop")]

    tools = openai_tools_to_anthropic_tools(body.get("tools"))
    if tools:
        result["tools"] = tools
    tool_choice = openai_tool_choice_to_anthropic(body.get("tool_choice"))
    if tool_choice is not None:
        result["tool_choice"] = tool_choice

    messages: List[Dict[str, Any]] = []
    system_blocks: List[Dict[str, Any]] = []
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raise AnthropicAdapterError("messages must be an array")

    for message in raw_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            blocks = openai_content_to_anthropic_blocks(message.get("content"))
            for block in blocks:
                if block.get("type") == "text":
                    if "cache_control" not in block and isinstance(message.get("cache_control"), dict):
                        block["cache_control"] = copy.deepcopy(message["cache_control"])
                    system_blocks.append(block)
            continue
        converted = _openai_message_to_anthropic_message(message)
        if converted:
            messages.append(converted)

    if system_blocks:
        result["system"] = system_blocks if len(system_blocks) > 1 or any("cache_control" in b for b in system_blocks) else system_blocks[0].get("text", "")

    result["messages"] = _merge_adjacent_same_role_messages(messages)
    if not result["messages"]:
        raise AnthropicAdapterError("messages must contain at least one user or assistant message")

    known_keys = {
        "model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k",
        "stop", "stream", "tools", "tool_choice", "metadata", "service_tier", "thinking",
        "container", "mcp_servers", "context_management",
    }
    for key, value in body.items():
        if key not in known_keys and key not in result:
            result[key] = copy.deepcopy(value)

    return result


def _anthropic_stop_reason_to_openai(stop_reason: Any) -> Optional[str]:
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason in {"end_turn", "stop_sequence"}:
        return "stop"
    return stop_reason if isinstance(stop_reason, str) else None


def _anthropic_usage_to_openai(usage: Any) -> Dict[str, Any]:
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    result = copy.deepcopy(usage)
    result["prompt_tokens"] = prompt_tokens
    result["completion_tokens"] = completion_tokens
    result["total_tokens"] = prompt_tokens + completion_tokens
    return result


def anthropic_response_to_openai_chat(payload: Dict[str, Any], original_body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise AnthropicAdapterError("Anthropic response payload must be a JSON object")

    content = payload.get("content")
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "tool",
                        "arguments": json.dumps(block.get("input") if isinstance(block.get("input"), dict) else {}, ensure_ascii=False),
                    },
                })
    elif isinstance(content, str):
        text_parts.append(content)

    message: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": payload.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or original_body.get("model"),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _anthropic_stop_reason_to_openai(payload.get("stop_reason")),
        }],
        "usage": _anthropic_usage_to_openai(payload.get("usage")),
    }


def _openai_sse_data(data: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class AnthropicToOpenAISSEConverter:
    """有状态 Anthropic Messages SSE -> OpenAI Chat Completions SSE 转换器。"""

    def __init__(self, original_body: Dict[str, Any]):
        self.original_body = original_body
        self.response_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.model = original_body.get("model")
        self.created = int(time.time())
        self.block_types: Dict[int, str] = {}
        self.tool_index_by_block: Dict[int, int] = {}
        self.next_tool_index = 0
        self.finished = False
        self.sent_role = False
        self.usage: Optional[Dict[str, Any]] = None
        self.stop_reason: Optional[str] = None

    def _chunk(self, delta: Dict[str, Any], finish_reason: Optional[str] = None, usage: Optional[Dict[str, Any]] = None) -> bytes:
        payload: Dict[str, Any] = {
            "id": self.response_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            payload["usage"] = usage
        return _openai_sse_data(payload)

    def start_events(self) -> List[bytes]:
        if self.sent_role:
            return []
        self.sent_role = True
        return [self._chunk({"role": "assistant"})]

    def feed_line(self, line_text: str) -> List[bytes]:
        if self.finished:
            return []
        line_text = line_text.strip()
        if not line_text.startswith("data:"):
            return []
        raw = line_text[5:].strip()
        if not raw or raw == "[DONE]":
            return []
        try:
            event = json.loads(raw)
        except Exception:
            return []
        if not isinstance(event, dict):
            return []
        return self.feed_event(event)

    def feed_event(self, event: Dict[str, Any]) -> List[bytes]:
        events: List[bytes] = []
        event_type = event.get("type")

        if event_type == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if isinstance(message.get("id"), str):
                self.response_id = message["id"]
            if isinstance(message.get("model"), str):
                self.model = message["model"]
            if isinstance(message.get("usage"), dict):
                self.usage = _anthropic_usage_to_openai(message.get("usage"))
            events.extend(self.start_events())

        elif event_type == "content_block_start":
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            block_type = block.get("type")
            self.block_types[index] = block_type
            if block_type == "tool_use":
                tool_index = self.next_tool_index
                self.next_tool_index += 1
                self.tool_index_by_block[index] = tool_index
                tool_call = {
                    "index": tool_index,
                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {"name": block.get("name") or "tool", "arguments": ""},
                }
                events.append(self._chunk({"tool_calls": [tool_call]}))
            elif block_type == "text" and isinstance(block.get("text"), str) and block.get("text"):
                events.append(self._chunk({"content": block.get("text")}))

        elif event_type == "content_block_delta":
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            delta_type = delta.get("type")
            if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                events.append(self._chunk({"content": delta.get("text")}))
            elif delta_type == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                tool_index = self.tool_index_by_block.get(index, 0)
                events.append(self._chunk({
                    "tool_calls": [{
                        "index": tool_index,
                        "function": {"arguments": delta.get("partial_json")},
                    }]
                }))

        elif event_type == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if isinstance(delta.get("stop_reason"), str):
                self.stop_reason = _anthropic_stop_reason_to_openai(delta.get("stop_reason"))
            if isinstance(event.get("usage"), dict):
                self.usage = _anthropic_usage_to_openai(event.get("usage"))

        elif event_type == "message_stop":
            finish_reason = self.stop_reason or "stop"
            events.append(self._chunk({}, finish_reason=finish_reason, usage=self.usage))
            events.append(b"data: [DONE]\n\n")
            self.finished = True

        return events

    def finish_events(self) -> List[bytes]:
        if self.finished:
            return []
        self.finished = True
        return [self._chunk({}, finish_reason=self.stop_reason or "stop", usage=self.usage), b"data: [DONE]\n\n"]


def _parse_anthropic_sse_events(sse_bytes: bytes) -> List[Dict[str, Any]]:
    text = sse_bytes.decode("utf-8", errors="ignore")
    events: List[Dict[str, Any]] = []
    current_data: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current_data:
                payload = "\n".join(current_data).strip()
                current_data = []
                if payload and payload != "[DONE]":
                    try:
                        obj = json.loads(payload)
                        if isinstance(obj, dict):
                            events.append(obj)
                    except Exception:
                        pass
            continue
        if line.startswith("data:"):
            current_data.append(line[5:].strip())
    if current_data:
        payload = "\n".join(current_data).strip()
        if payload and payload != "[DONE]":
            try:
                obj = json.loads(payload)
                if isinstance(obj, dict):
                    events.append(obj)
            except Exception:
                pass
    return events


def convert_anthropic_stream_events_to_response(events: List[Dict[str, Any]], original_body: Dict[str, Any]) -> Dict[str, Any]:
    response: Dict[str, Any] = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": original_body.get("model"),
        "content": [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    content_blocks: Dict[int, Dict[str, Any]] = {}
    for event in events:
        event_type = event.get("type")
        if event_type == "message_start" and isinstance(event.get("message"), dict):
            response.update(copy.deepcopy(event["message"]))
            response["content"] = []
        elif event_type == "content_block_start":
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            block = copy.deepcopy(event.get("content_block")) if isinstance(event.get("content_block"), dict) else {"type": "text", "text": ""}
            if block.get("type") == "text":
                block.setdefault("text", "")
            if block.get("type") == "tool_use":
                block.setdefault("input", {})
                block["_partial_json"] = ""
            content_blocks[index] = block
        elif event_type == "content_block_delta":
            index = event.get("index") if isinstance(event.get("index"), int) else 0
            block = content_blocks.setdefault(index, {"type": "text", "text": ""})
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                block["text"] = block.get("text", "") + delta["text"]
            elif delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                block["_partial_json"] = block.get("_partial_json", "") + delta["partial_json"]
        elif event_type == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if "stop_reason" in delta:
                response["stop_reason"] = delta.get("stop_reason")
            if "stop_sequence" in delta:
                response["stop_sequence"] = delta.get("stop_sequence")
            if isinstance(event.get("usage"), dict):
                response["usage"] = event.get("usage")

    output_blocks: List[Dict[str, Any]] = []
    for index in sorted(content_blocks.keys()):
        block = content_blocks[index]
        if block.get("type") == "tool_use":
            partial = block.pop("_partial_json", "")
            if partial:
                try:
                    block["input"] = json.loads(partial)
                except Exception:
                    block["input"] = {"arguments": partial}
        output_blocks.append(block)
    response["content"] = output_blocks
    return response


def convert_anthropic_sse_bytes_to_non_stream_json(sse_bytes: bytes, original_body: Dict[str, Any]) -> Dict[str, Any]:
    return convert_anthropic_stream_events_to_response(_parse_anthropic_sse_events(sse_bytes), original_body)


def openai_injected_message_to_anthropic_fragment(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把现有 OpenAI 结构的注入消息转换为 Anthropic message/system 片段。"""
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role == "system":
        return {"role": "system", "content": extract_text_from_anthropic_content(openai_content_to_anthropic_blocks(message.get("content")))}
    return _openai_message_to_anthropic_message(message)


def anthropic_response_to_openai_stream_events(payload: Dict[str, Any], original_body: Dict[str, Any]) -> List[bytes]:
    """Anthropic 非流响应 -> OpenAI SSE，用于假流式。"""
    openai_payload = anthropic_response_to_openai_chat(payload, original_body)
    choice = openai_payload.get("choices", [{}])[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    events: List[bytes] = []
    events.append(_openai_sse_data({
        "id": openai_payload.get("id"),
        "object": "chat.completion.chunk",
        "created": openai_payload.get("created"),
        "model": openai_payload.get("model"),
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }))
    delta: Dict[str, Any] = {}
    if isinstance(message.get("content"), str) and message.get("content"):
        delta["content"] = message.get("content")
    if isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
        delta["tool_calls"] = [{**call, "index": idx} for idx, call in enumerate(message["tool_calls"]) if isinstance(call, dict)]
    if delta:
        events.append(_openai_sse_data({
            "id": openai_payload.get("id"),
            "object": "chat.completion.chunk",
            "created": openai_payload.get("created"),
            "model": openai_payload.get("model"),
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }))
    events.append(_openai_sse_data({
        "id": openai_payload.get("id"),
        "object": "chat.completion.chunk",
        "created": openai_payload.get("created"),
        "model": openai_payload.get("model"),
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        "usage": openai_payload.get("usage"),
    }))
    events.append(b"data: [DONE]\n\n")
    return events
