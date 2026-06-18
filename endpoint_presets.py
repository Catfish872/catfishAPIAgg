import base64
import copy
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin


class EndpointPresetError(Exception):
    """预设端点转换错误。"""


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def is_empty_text(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def extract_text_from_message_content(content: Any) -> str:
    """从 OpenAI chat message content 中提取纯文本；多模态 content 只取 text 部分。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in ("text", "input_text") and isinstance(item.get("text"), str):
                text = item["text"].strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def extract_image_urls_from_messages(messages: Any) -> List[str]:
    """从 OpenAI 多模态 messages 中提取 image_url / input_image / image URL 或 data URL。"""
    if not isinstance(messages, list):
        return []
    urls: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            url = None
            if item_type == "image_url":
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                elif isinstance(image_url, str):
                    url = image_url
            elif item_type == "input_image":
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                elif isinstance(image_url, str):
                    url = image_url
            elif item_type == "image":
                raw_url = item.get("url") or item.get("image_url")
                if isinstance(raw_url, str):
                    url = raw_url
                elif isinstance(raw_url, dict):
                    url = raw_url.get("url")
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())
    return urls


def build_image_prompt_from_messages(messages: Any, top_level_prompt: Optional[str] = None, recent_context_count: int = 8) -> str:
    """把 chat messages 固定转换为 Images Generations prompt。"""
    if not isinstance(messages, list):
        fallback = (top_level_prompt or "").strip()
        return fallback

    system_texts: List[str] = []
    non_system: List[Dict[str, str]] = []
    last_user_text = ""

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = extract_text_from_message_content(message.get("content"))
        if not text:
            continue
        if role == "system":
            system_texts.append(text)
        elif role in ("user", "assistant"):
            non_system.append({"role": str(role), "text": text})
            if role == "user":
                last_user_text = text

    if not last_user_text and isinstance(top_level_prompt, str) and top_level_prompt.strip():
        last_user_text = top_level_prompt.strip()

    current_request = last_user_text
    context_messages = non_system[-recent_context_count:] if recent_context_count > 0 else non_system
    if current_request:
        for index in range(len(context_messages) - 1, -1, -1):
            item = context_messages[index]
            if item.get("role") == "user" and item.get("text") == current_request:
                context_messages = context_messages[:index] + context_messages[index + 1:]
                break

    sections: List[str] = []
    if system_texts:
        sections.append("System instructions:\n" + "\n\n".join(system_texts))
    if context_messages:
        context_lines = [f"{item['role']}: {item['text']}" for item in context_messages if item.get("text")]
        if context_lines:
            sections.append("Conversation context:\n" + "\n".join(context_lines))
    if current_request:
        sections.append("Current request:\n" + current_request)

    return "\n\n".join(sections).strip()


def _clamp_n(value: Any) -> int:
    try:
        n = int(value)
    except Exception:
        n = 1
    return max(1, min(4, n))


def _clean_path(value: Any, default: str) -> str:
    path = str(value or "").strip() or default
    if not path.startswith("/"):
        path = "/" + path
    return path


def build_images_generations_payload(raw_body: Dict[str, Any], config: Any) -> Dict[str, Any]:
    """向后兼容：仅返回图片请求 body。新逻辑请使用 build_images_request_plan。"""
    _, payload = build_images_request_plan(raw_body, config)
    return payload


def build_images_request_plan(raw_body: Dict[str, Any], config: Any) -> Tuple[str, Dict[str, Any]]:
    """构造图片上游请求计划，返回 (path, payload)。

    endpoint_preset=images_generations 只是选择图片预设；实际 path 由 image_upstream_mode 决定：
    - openai_edit_image: 有参考图走 /images/edits，没图走 /images/generations
    - generation_*: 始终走 /images/generations，并按模式映射参考图字段
    - custom: 使用自定义路径和字段
    """
    if not isinstance(raw_body, dict):
        raise EndpointPresetError("请求体必须是 JSON 对象")

    model = _config_get(config, "model") or raw_body.get("model") or "gpt-image-2"
    prompt = build_image_prompt_from_messages(raw_body.get("messages"), raw_body.get("prompt"))
    if is_empty_text(prompt):
        raise EndpointPresetError("images_generations preset 无法从 messages 或顶层 prompt 构造 prompt")

    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": _clamp_n(raw_body.get("n", 1)),
        "size": raw_body.get("size") or raw_body.get("image_size") or raw_body.get("resolution") or "1024x1024",
    }

    for key in ["response_format", "quality", "style", "user", "upscale", "background", "output_format", "output_compression", "input_fidelity"]:
        if key in raw_body and raw_body.get(key) is not None:
            payload[key] = raw_body.get(key)

    reference_images = extract_image_urls_from_messages(raw_body.get("messages"))
    has_refs = bool(reference_images)
    mode = str(_config_get(config, "image_upstream_mode", "generation_reference_images_array") or "generation_reference_images_array")
    generation_path = _clean_path(_config_get(config, "image_generation_path", "/images/generations"), "/images/generations")
    edit_path = _clean_path(_config_get(config, "image_edit_path", "/images/edits"), "/images/edits")

    if mode == "openai_edit_image":
        if has_refs:
            payload["image"] = reference_images[0]
            return edit_path, payload
        return generation_path, payload

    if mode == "generation_images_array":
        if has_refs:
            payload["images"] = reference_images
        return generation_path, payload

    if mode == "generation_ref_assets_array":
        if has_refs:
            payload["ref_assets"] = reference_images
        return generation_path, payload

    if mode == "custom":
        custom_generation_path = _clean_path(_config_get(config, "image_custom_generation_path", None), generation_path)
        custom_edit_path = _clean_path(_config_get(config, "image_custom_edit_path", None), edit_path)
        custom_field = str(_config_get(config, "image_custom_reference_field", "") or "").strip()
        custom_mode = str(_config_get(config, "image_custom_reference_mode", "array") or "array")
        object_url_field = str(_config_get(config, "image_custom_reference_object_url_field", "image_url") or "image_url").strip() or "image_url"
        include_empty = bool(_config_get(config, "image_custom_include_reference_when_empty", False))
        path = custom_edit_path if has_refs else custom_generation_path
        if custom_field and (has_refs or include_empty):
            if custom_mode == "single":
                payload[custom_field] = reference_images[0] if has_refs else ""
            elif custom_mode == "object_array":
                payload[custom_field] = [{object_url_field: image_url} for image_url in reference_images] if has_refs else []
            else:
                payload[custom_field] = reference_images
        return path, payload

    # 默认保持旧桥接行为，避免破坏既有配置。
    if has_refs:
        payload["reference_images"] = reference_images
    return generation_path, payload


def _origin_from_upstream_url(upstream_url: str) -> Optional[str]:
    if not isinstance(upstream_url, str) or not upstream_url.strip():
        return None
    parsed = urlparse(upstream_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_image_response_urls(response: Any, upstream_url: str) -> Any:
    """补全 Images response 中 data[i].url 的相对路径。"""
    if not isinstance(response, dict):
        return response
    origin = _origin_from_upstream_url(upstream_url)
    data = response.get("data")
    if not isinstance(data, list):
        return response
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        if url.startswith("http://") or url.startswith("https://") or url.startswith("data:"):
            continue
        if url.startswith("/") and origin:
            item["url"] = urljoin(origin, url)
    return response


_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)


def _image_extension_from_media_type(media_type: Optional[str]) -> str:
    normalized = (media_type or "image/png").lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }
    return mapping.get(normalized, ".png")


def _decode_base64_image(value: str) -> Optional[Dict[str, Any]]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    media_type = "image/png"
    match = _DATA_URL_RE.match(raw)
    if match:
        media_type = match.group(1)
        raw = match.group(2)
    try:
        image_bytes = base64.b64decode(raw, validate=False)
    except Exception:
        return None
    if not image_bytes:
        return None
    return {
        "bytes": image_bytes,
        "media_type": media_type,
        "extension": _image_extension_from_media_type(media_type)
    }


Base64ImageSaver = Callable[[Dict[str, Any], Optional[str], Optional[str]], Optional[str]]


def _save_decoded_base64_image_to_disk(
        decoded: Dict[str, Any],
        output_dir: Optional[str],
        public_url_prefix: Optional[str]
) -> Optional[str]:
    """默认兼容保存器：把解码后的图片写入磁盘，并返回公开 URL。"""
    if not output_dir or not public_url_prefix:
        return None
    os.makedirs(output_dir, exist_ok=True)
    filename = f"img_{int(time.time())}_{uuid.uuid4().hex}{decoded['extension']}"
    file_path = os.path.join(output_dir, filename)
    with open(file_path, "wb") as f:
        f.write(decoded["bytes"])
    return f"{public_url_prefix.rstrip('/')}/{filename}"


def save_base64_image_as_url(
        value: str,
        output_dir: Optional[str],
        public_url_prefix: Optional[str],
        image_saver: Optional[Base64ImageSaver] = None
) -> Optional[str]:
    """把 b64_json 或 data:image base64 转为公开 URL；保存介质由 image_saver 决定，默认写磁盘。"""
    decoded = _decode_base64_image(value)
    if not decoded:
        return None
    saver = image_saver or _save_decoded_base64_image_to_disk
    return saver(decoded, output_dir, public_url_prefix)


_INLINE_DATA_IMAGE_RE = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)",
    re.MULTILINE
)


def replace_inline_base64_images_with_urls(
        text: str,
        output_dir: Optional[str],
        public_url_prefix: Optional[str],
        image_saver: Optional[Base64ImageSaver] = None
) -> str:
    """替换字符串中的 data:image base64 为公开 URL，兼容 markdown 图片和裸 data URL。"""
    if not isinstance(text, str) or "data:image/" not in text:
        return text

    def _replace(match: re.Match) -> str:
        data_url = f"data:{match.group(1)};base64,{match.group(2)}"
        saved_url = save_base64_image_as_url(data_url, output_dir, public_url_prefix, image_saver=image_saver)
        return saved_url or match.group(0)

    return _INLINE_DATA_IMAGE_RE.sub(_replace, text)


def convert_response_base64_images_to_urls(
        payload: Any,
        image_output_dir: Optional[str],
        image_public_url_prefix: Optional[str],
        image_saver: Optional[Base64ImageSaver] = None
) -> Any:
    """递归处理任意 JSON 响应中的 base64 图片，把 data URL / b64_json 转为公开 URL。

    该函数面向 Chat Completions、Images Generations 及第三方扩展响应格式：
    - 字符串中的 data:image/...;base64,... 会被替换为 URL；
    - dict 中的 b64_json 会被保存，并补充/覆盖同级 url；
    - dict 中的 url / image_url 等字符串若是 data URL，会被替换为 URL；
    - b64_json 保存成功后会从响应中移除，避免向下游和完整响应日志透传大块 base64。
    """
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            payload[index] = convert_response_base64_images_to_urls(item, image_output_dir, image_public_url_prefix, image_saver=image_saver)
        return payload

    if isinstance(payload, dict):
        b64_json = payload.get("b64_json")
        if isinstance(b64_json, str) and b64_json.strip():
            saved_url = save_base64_image_as_url(b64_json, image_output_dir, image_public_url_prefix, image_saver=image_saver)
            if saved_url:
                current_url = payload.get("url")
                if not isinstance(current_url, str) or not current_url.strip() or current_url.startswith("data:image/"):
                    payload["url"] = saved_url
                payload.pop("b64_json", None)

        for key, value in list(payload.items()):
            if isinstance(value, str):
                payload[key] = replace_inline_base64_images_with_urls(value, image_output_dir, image_public_url_prefix, image_saver=image_saver)
            elif isinstance(value, (dict, list)):
                payload[key] = convert_response_base64_images_to_urls(value, image_output_dir, image_public_url_prefix, image_saver=image_saver)
        return payload

    if isinstance(payload, str):
        return replace_inline_base64_images_with_urls(payload, image_output_dir, image_public_url_prefix, image_saver=image_saver)

    return payload


def build_anthropic_request_plan(raw_body: Dict[str, Any], config: Any, *, source_protocol: str = "openai") -> Tuple[str, Dict[str, Any]]:
    """构造 Anthropic /v1/messages 上游请求计划，返回 (path, payload)。

    - source_protocol="openai": raw_body 是 OpenAI Chat Completions 结构，由调用方先转换为 Anthropic 结构后传入也可；
      本函数只负责预设层通用字段覆盖与必填补齐。
    - source_protocol="anthropic": raw_body 是 Anthropic Messages 结构。为保证 prompt cache 命中，除 model/request_overrides/stream/max_tokens
      等必要字段外，必须最小改写，不能重排 messages/system/tools/cache_control。
    """
    if not isinstance(raw_body, dict):
        raise EndpointPresetError("anthropic_messages preset 请求体必须是 JSON 对象")

    payload = copy.deepcopy(raw_body)
    model = _config_get(config, "model") or payload.get("model")
    if is_empty_text(model):
        raise EndpointPresetError("anthropic_messages preset 缺少 model")
    payload["model"] = str(model).strip()

    if payload.get("max_tokens") is None:
        payload["max_tokens"] = 32768

    request_overrides = _config_get(config, "request_overrides", {})
    if isinstance(request_overrides, dict) and request_overrides:
        payload.update(copy.deepcopy(request_overrides))
        if payload.get("max_tokens") is None:
            payload["max_tokens"] = 32768

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise EndpointPresetError("anthropic_messages preset 缺少 messages 数组")

    return "/messages", payload


def wrap_image_response_as_chat_completion(
        response: Dict[str, Any],
        raw_body: Dict[str, Any],
        config: Any,
        image_output_dir: Optional[str] = None,
        image_public_url_prefix: Optional[str] = None,
        image_saver: Optional[Base64ImageSaver] = None
) -> Dict[str, Any]:
    """将 Images Generations 响应包装为 OpenAI chat.completion。base64 图片会优先转为 URL 返回。"""
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list) or not data:
        raise EndpointPresetError("上游 images_generations 响应缺少 data 数组")

    images: List[Dict[str, Any]] = []
    markdown_parts: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else None
        b64_json = item.get("b64_json") if isinstance(item.get("b64_json"), str) else None

        # 有些上游把 data URL 放在 url 字段；这种也转成普通 URL。
        if isinstance(url, str) and url.startswith("data:image/"):
            saved_url = save_base64_image_as_url(url, image_output_dir, image_public_url_prefix, image_saver=image_saver)
            if saved_url:
                url = saved_url

        if not url and b64_json:
            saved_url = save_base64_image_as_url(b64_json, image_output_dir, image_public_url_prefix, image_saver=image_saver)
            if saved_url:
                url = saved_url

        if not url:
            continue
        images.append({"url": url})
        markdown_parts.append(f"![image]({url})")

    if not images:
        raise EndpointPresetError("上游 images_generations 响应未包含 url 或 b64_json")

    created = response.get("created") if isinstance(response.get("created"), int) else int(time.time())
    return {
        "id": f"chatcmpl-img-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created,
        "model": raw_body.get("model") or _config_get(config, "model"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "\n\n".join(markdown_parts)
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        },
        "images": images
    }
