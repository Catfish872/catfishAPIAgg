import unittest
from types import SimpleNamespace

from chat_image_content_compat import (
    fill_chat_completion_image_content,
    install_chat_image_content_compat,
)


class FillChatCompletionImageContentTests(unittest.TestCase):
    def test_fills_empty_content_from_message_images(self):
        payload = {
            "object": "chat.completion",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "kept",
                    "images": [{
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/generated.png"},
                        "index": 0,
                    }],
                }
            }],
        }

        result = fill_chat_completion_image_content(payload)

        self.assertIs(result, payload)
        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "![image](https://example.test/generated.png)",
        )
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], "kept")

    def test_preserves_existing_content(self):
        payload = {
            "choices": [{
                "message": {
                    "content": "Existing answer",
                    "images": [{"url": "https://example.test/generated.png"}],
                }
            }]
        }

        fill_chat_completion_image_content(payload)

        self.assertEqual(payload["choices"][0]["message"]["content"], "Existing answer")

    def test_deduplicates_multiple_compatible_image_shapes(self):
        payload = {
            "choices": [{
                "message": {
                    "content": "   ",
                    "images": [
                        {"url": "https://example.test/a.png"},
                        {"image_url": "https://example.test/a.png"},
                        "https://example.test/b.png",
                    ],
                }
            }]
        }

        fill_chat_completion_image_content(payload)

        self.assertEqual(
            payload["choices"][0]["message"]["content"],
            "![image](https://example.test/a.png)\n\n![image](https://example.test/b.png)",
        )

    def test_uses_single_choice_top_level_images_as_legacy_fallback(self):
        payload = {
            "choices": [{"message": {"content": None}}],
            "images": [{"url": "/generated-images/example.png"}],
        }

        fill_chat_completion_image_content(payload)

        self.assertEqual(
            payload["choices"][0]["message"]["content"],
            "![image](/generated-images/example.png)",
        )

    def test_ignores_unrelated_payload(self):
        payload = {"data": [{"url": "https://example.test/generated.png"}]}
        self.assertIs(fill_chat_completion_image_content(payload), payload)
        self.assertNotIn("choices", payload)


class InstallCompatTests(unittest.TestCase):
    def test_installer_is_idempotent_and_preserves_original_arguments(self):
        calls = []

        def original(payload, output_dir, public_prefix, image_saver=None):
            calls.append((output_dir, public_prefix, image_saver))
            return payload

        module = SimpleNamespace(convert_response_base64_images_to_urls=original)
        wrapped = install_chat_image_content_compat(module)
        second = install_chat_image_content_compat(module)
        marker = object()
        payload = {
            "choices": [{
                "message": {
                    "content": None,
                    "images": [{"url": "https://example.test/generated.png"}],
                }
            }]
        }

        result = module.convert_response_base64_images_to_urls(
            payload,
            "dir",
            "https://public.test/generated-images",
            image_saver=marker,
        )

        self.assertIs(wrapped, second)
        self.assertEqual(calls, [("dir", "https://public.test/generated-images", marker)])
        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "![image](https://example.test/generated.png)",
        )


if __name__ == "__main__":
    unittest.main()
