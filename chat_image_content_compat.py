"""Fill empty Chat Completions content from generated image fields."""

from functools import wraps
from typing import Any, Callable, Optional


_MARKER = "__catfish_chat_image_content_compat__"


def _empty_content(value: Any) -> bool:
    return value is None or value == [] or isinstance(value, str) and not value.strip()


def _image_url(item: Any) -> Optional[str]:
    if isinstance(item, str):
        value = item
    elif isinstance(item, dict):
        value = item.get("url") or item.get("image_url")
        if isinstance(value, dict):
            value = value.get("url")
    else:
        return None

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.startswith(("http://", "https://", "data:image/", "/")) else None


def fill_chat_completion_image_content(payload: Any) -> Any:
    """Add Markdown links only when a chat message has images and empty content."""
    if not isinstance(payload, dict):
        return payload

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload

    top_images = payload.get("images") if len(choices) == 1 else None
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict) or not _empty_content(message.get("content")):
            continue

        urls = []
        for group in (message.get("images"), choice.get("images"), top_images):
            if not isinstance(group, list):
                continue
            for item in group:
                url = _image_url(item)
                if url and url not in urls:
                    urls.append(url)

        if urls:
            message["content"] = "\n\n".join(f"![image]({url})" for url in urls)

    return payload


def install_chat_image_content_compat(app_core_module: Any) -> Callable[..., Any]:
    """Wrap the existing image normalizer without changing routing or metadata."""
    original = app_core_module.convert_response_base64_images_to_urls
    if getattr(original, _MARKER, False):
        return original

    @wraps(original)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return fill_chat_completion_image_content(original(*args, **kwargs))

    setattr(wrapped, _MARKER, True)
    app_core_module.convert_response_base64_images_to_urls = wrapped
    return wrapped
