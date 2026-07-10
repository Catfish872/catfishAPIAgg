"""Compatibility bridge for image-bearing Chat Completions responses.

Some OpenAI-compatible upstreams return generated images in
``choices[*].message.images`` while leaving ``message.content`` empty. A number
of clients only inspect ``message.content`` and therefore report an empty
response even though the image URL is valid. This module fills Markdown image
links only in that narrow case and leaves every existing non-empty content
value untouched.
"""

from functools import wraps
from typing import Any, Callable, Iterable, List, Optional


_COMPAT_MARKER = "__catfish_chat_image_content_compat__"


def _is_empty_content(value: Any) -> bool:
    """Return True only for content shapes that are semantically empty."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


def _candidate_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    if url.startswith(("http://", "https://", "data:image/", "/")):
        return url
    return None


def _extract_image_url(item: Any) -> Optional[str]:
    """Extract a URL from common Chat Completions image item shapes."""
    direct = _candidate_url(item)
    if direct:
        return direct
    if not isinstance(item, dict):
        return None

    direct = _candidate_url(item.get("url"))
    if direct:
        return direct

    image_url = item.get("image_url")
    direct = _candidate_url(image_url)
    if direct:
        return direct
    if isinstance(image_url, dict):
        direct = _candidate_url(image_url.get("url"))
        if direct:
            return direct

    source = item.get("source")
    if isinstance(source, dict):
        return _candidate_url(source.get("url"))
    return None


def _collect_image_urls(image_groups: Iterable[Any]) -> List[str]:
    urls: List[str] = []
    seen = set()
    for group in image_groups:
        if not isinstance(group, (list, tuple)):
            continue
        for item in group:
            url = _extract_image_url(item)
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def fill_chat_completion_image_content(payload: Any) -> Any:
    """Fill empty assistant content from image fields without changing other replies.

    Supported sources are ``message.images``, ``choice.images``, and the legacy
    top-level ``images`` field. Existing non-empty string or structured content
    always wins. The payload is mutated in place, matching the existing response
    normalization helpers in APIAgg.
    """
    if not isinstance(payload, dict):
        return payload

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return payload

    top_level_images = payload.get("images")
    use_top_level_fallback = len(choices) == 1 and isinstance(top_level_images, (list, tuple))

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict) or not _is_empty_content(message.get("content")):
            continue

        image_groups: List[Any] = [message.get("images"), choice.get("images")]
        if use_top_level_fallback:
            image_groups.append(top_level_images)
        urls = _collect_image_urls(image_groups)
        if not urls:
            continue

        message["content"] = "\n\n".join(f"![image]({url})" for url in urls)

    return payload


def install_chat_image_content_compat(app_core_module: Any) -> Callable[..., Any]:
    """Wrap APIAgg's existing image URL normalizer with the content bridge.

    The wrapper is installed on ``app_core`` rather than replacing request
    routing. This keeps all existing base64-to-URL handling, retry logic,
    streaming strategy selection, and response metadata unchanged. Installation
    is idempotent to remain safe under development reloaders.
    """
    original = getattr(app_core_module, "convert_response_base64_images_to_urls")
    if getattr(original, _COMPAT_MARKER, False):
        return original

    @wraps(original)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        converted = original(*args, **kwargs)
        return fill_chat_completion_image_content(converted)

    setattr(wrapped, _COMPAT_MARKER, True)
    setattr(app_core_module, "convert_response_base64_images_to_urls", wrapped)
    return wrapped
