from __future__ import annotations

import json
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


ChatHandler = Callable[[Any, bool], Awaitable[Response]]


class ChatRequestProxy:
    """Minimal Request-compatible wrapper for the existing chat proxy."""

    def __init__(self, request: Request, body: Dict[str, Any]):
        self._request = request
        self._body = body

    @property
    def headers(self):
        return self._request.headers

    @property
    def base_url(self):
        return self._request.base_url

    async def json(self) -> Dict[str, Any]:
        return self._body

    async def is_disconnected(self) -> bool:
        return await self._request.is_disconnected()


def register_responses_endpoint(
    app,
    chat_handler: ChatHandler,
    verify_key,
    *,
    log_message: Optional[Callable[[str], None]] = None,
) -> None:
    """Expose /v1/responses while keeping /v1/chat/completions as the core path."""

    if getattr(app.state, "responses_endpoint_registered", False):
        return

    @app.post("/v1/responses", tags=["Proxy"])
    async def proxy_responses(request: Request, auth: bool = Depends(verify_key)):
        try:
            responses_body = await request.json()
            chat_body = responses_to_chat_request(responses_body)
        except ValueError as exc:
            _log(log_message, f"Responses 请求转换失败: {exc}")
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except Exception:
            _log(log_message, "Responses 请求体 JSON 解析失败")
            return JSONResponse(status_code=400, content={"error": "无效的 JSON 请求体"})

        chat_response = await chat_handler(ChatRequestProxy(request, chat_body), True)

        if responses_body.get("stream") is True:
            if isinstance(chat_response, StreamingResponse):
                return StreamingResponse(
                    chat_stream_to_response_stream(chat_response.body_iterator, responses_body),
                    media_type="text/event-stream",
                )

            chat_json = await response_to_json(chat_response)
            if getattr(chat_response, "status_code", 200) >= 400:
                return JSONResponse(status_code=chat_response.status_code, content=chat_json)
            return StreamingResponse(
                one_response_stream(chat_to_response(chat_json, responses_body)),
                media_type="text/event-stream",
            )

        if isinstance(chat_response, StreamingResponse):
            chat_json = await collect_chat_stream(chat_response.body_iterator)
        else:
            chat_json = await response_to_json(chat_response)

        if getattr(chat_response, "status_code", 200) >= 400:
            return JSONResponse(status_code=chat_response.status_code, content=chat_json)
        return JSONResponse(
            status_code=getattr(chat_response, "status_code", 200),
            content=chat_to_response(chat_json, responses_body),
        )

    app.state.responses_endpoint_registered = True


def responses_to_chat_request(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("Responses 请求体必须是 JSON 对象")

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Responses 请求缺少 model")

    messages = input_to_messages(body.get("input"), body.get("instructions"))
    if not messages:
        raise ValueError("Responses 请求缺少可转换的 input")

    result: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }

    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "stop", "seed", "user"):
        if key in body:
            result[key] = body[key]

    token_limit = body.get("max_output_tokens", body.get("max_tokens"))
    if token_limit is not None:
        result["max_tokens"] = token_limit
        result["max_completion_tokens"] = token_limit

    tools = tools_to_chat(body.get("tools"))
    if tools:
        result["tools"] = tools
    if "tool_choice" in body:
        result["tool_choice"] = tool_choice_to_chat(body["tool_choice"])

    return result


def input_to_messages(input_value: Any, instructions: Any) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
        return messages

    if not isinstance(input_value, list):
        return messages

    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "call_0",
                "content": stringify(item.get("output")),
            })
            continue

        role = item.get("role") or "user"
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"

        content = item.get("content")
        if content is None and item_type in {"message", "input_text", "output_text"}:
            content = item.get("text")
        messages.append({"role": role, "content": content_to_chat(content)})

    return messages


def content_to_chat(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return stringify(content)

    text_parts: List[str] = []
    chat_parts: List[Dict[str, Any]] = []
    has_image = False
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            chat_parts.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            text = stringify(part)
            text_parts.append(text)
            chat_parts.append({"type": "text", "text": text})
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            text = stringify(part.get("text"))
            text_parts.append(text)
            chat_parts.append({"type": "text", "text": text})
        elif part_type == "input_image" and (part.get("image_url") or part.get("url")):
            has_image = True
            chat_parts.append({"type": "image_url", "image_url": {"url": part.get("image_url") or part.get("url")}})
        else:
            text = stringify(part)
            text_parts.append(text)
            chat_parts.append({"type": "text", "text": text})

    return chat_parts if has_image else "\n".join(x for x in text_parts if x)


def tools_to_chat(tools: Any) -> List[Dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    result: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        if isinstance(tool.get("function"), dict):
            result.append(tool)
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            })
    return result


def tool_choice_to_chat(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") == "function" and isinstance(value.get("name"), str):
        return {"type": "function", "function": {"name": value["name"]}}
    return value


def chat_to_response(chat: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    response_id = f"resp_{uuid.uuid4().hex}"
    output = chat_choices_to_output(chat.get("choices"))
    payload: Dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": chat.get("created") if isinstance(chat.get("created"), int) else int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": source.get("instructions"),
        "model": chat.get("model") or source.get("model"),
        "output": output,
        "output_text": output_text(output),
        "parallel_tool_calls": bool(source.get("parallel_tool_calls", True)),
        "temperature": source.get("temperature"),
        "tool_choice": source.get("tool_choice", "auto"),
        "tools": source.get("tools", []),
        "top_p": source.get("top_p"),
    }
    usage = usage_to_response(chat.get("usage"))
    if usage:
        payload["usage"] = usage
    return payload


def chat_choices_to_output(choices: Any) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        text = message_text(message.get("content"))
        if text or not message.get("tool_calls"):
            output.append(message_item(text, message.get("role") or "assistant"))
        for tool_call in message.get("tool_calls") or []:
            item = tool_call_item(tool_call)
            if item:
                output.append(item)
    return output or [message_item("")]


def message_item(text: str, role: str = "assistant") -> Dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "status": "completed",
        "role": role,
        "content": [{"type": "output_text", "text": text or "", "annotations": []}],
    }


def tool_call_item(tool_call: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return None
    fn = tool_call.get("function") or {}
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex}"
    return {
        "type": "function_call",
        "id": call_id,
        "call_id": call_id,
        "name": name,
        "arguments": fn.get("arguments") or "{}",
        "status": "completed",
    }


async def chat_stream_to_response_stream(chunks: Iterable[bytes], source: Dict[str, Any]):
    response_id = f"resp_{uuid.uuid4().hex}"
    message_id = f"msg_{uuid.uuid4().hex}"
    created_at = int(time.time())
    model = source.get("model")
    parts: List[str] = []
    item_started = False
    content_started = False
    buffer = ""

    def snapshot(status: str) -> Dict[str, Any]:
        text = "".join(parts)
        return {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": source.get("instructions"),
            "model": model,
            "output": [{
                "id": message_id,
                "type": "message",
                "status": "completed" if status == "completed" else "in_progress",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }],
            "output_text": text,
            "parallel_tool_calls": bool(source.get("parallel_tool_calls", True)),
            "temperature": source.get("temperature"),
            "tool_choice": source.get("tool_choice", "auto"),
            "tools": source.get("tools", []),
            "top_p": source.get("top_p"),
        }

    yield sse("response.created", {"type": "response.created", "response": snapshot("in_progress")})
    yield sse("response.in_progress", {"type": "response.in_progress", "response": snapshot("in_progress")})

    async for raw in chunks:
        buffer += raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            for data in sse_data(event):
                if data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                model = chunk.get("model") or model
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text_delta = delta.get("content")
                    if not item_started:
                        item_started = True
                        yield sse("response.output_item.added", {
                            "type": "response.output_item.added",
                            "response_id": response_id,
                            "output_index": 0,
                            "item": message_item(""),
                        })
                    if isinstance(text_delta, str) and text_delta:
                        if not content_started:
                            content_started = True
                            yield sse("response.content_part.added", {
                                "type": "response.content_part.added",
                                "response_id": response_id,
                                "item_id": message_id,
                                "output_index": 0,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            })
                        parts.append(text_delta)
                        yield sse("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "item_id": message_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": text_delta,
                        })

    final_text = "".join(parts)
    if content_started:
        yield sse("response.content_part.done", {
            "type": "response.content_part.done",
            "response_id": response_id,
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": final_text, "annotations": []},
        })
    yield sse("response.output_item.done", {
        "type": "response.output_item.done",
        "response_id": response_id,
        "output_index": 0,
        "item": {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": final_text, "annotations": []}]},
    })
    yield sse("response.completed", {"type": "response.completed", "response": snapshot("completed")})
    yield b"data: [DONE]\n\n"


async def one_response_stream(response: Dict[str, Any]):
    yield sse("response.created", {"type": "response.created", "response": {**response, "status": "in_progress"}})
    yield sse("response.completed", {"type": "response.completed", "response": response})
    yield b"data: [DONE]\n\n"


async def response_to_json(response: Response) -> Dict[str, Any]:
    body = getattr(response, "body", b"")
    if isinstance(body, str):
        body = body.encode("utf-8")
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return {"error": body.decode("utf-8", errors="ignore") if body else "empty response"}
    return data if isinstance(data, dict) else {"data": data}


async def collect_chat_stream(chunks: Iterable[bytes]) -> Dict[str, Any]:
    text_parts: List[str] = []
    model = None
    created = int(time.time())
    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    buffer = ""
    async for raw in chunks:
        buffer += raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            for data in sse_data(event):
                if data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                response_id = chunk.get("id") or response_id
                model = chunk.get("model") or model
                created = chunk.get("created") if isinstance(chunk.get("created"), int) else created
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        text_parts.append(delta["content"])
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(text_parts)}, "finish_reason": "stop"}],
    }


def sse(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def sse_data(raw_event: str) -> List[str]:
    lines = [line[5:].strip() for line in raw_event.splitlines() if line.strip().startswith("data:")]
    return ["\n".join(lines)] if lines else []


def output_text(output: List[Dict[str, Any]]) -> str:
    result: List[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                result.append(str(content.get("text") or ""))
    return "".join(result)


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result: List[str] = []
        for part in content:
            if isinstance(part, dict):
                result.append(str(part.get("text") or ""))
            elif part is not None:
                result.append(str(part))
        return "".join(result)
    return "" if content is None else str(content)


def usage_to_response(usage: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": usage.get("prompt_tokens_details") or {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": usage.get("completion_tokens_details") or {"reasoning_tokens": 0},
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _log(log_message: Optional[Callable[[str], None]], message: str) -> None:
    if log_message is None:
        return
    try:
        log_message(message)
    except Exception:
        return
