# -*- coding: utf-8 -*-
"""Responses API 状态校验、错误脱敏与 temperature 兼容性测试。"""

import json
import sys
import unittest
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

from astrbot_plugin_onepercent_generator.ai_client import (  # noqa: E402
    API_FORMAT_CHAT_COMPLETIONS,
    API_FORMAT_RESPONSES,
    AIClient,
    AIClientAPIError,
    AIClientEmptyResponse,
    AIClientError,
    AIClientIncomplete,
    AIClientResponseFailed,
)


class ResponsesStatusTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler, api_format=API_FORMAT_RESPONSES,
                send_temperature=True, api_key="test-key"):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key=api_key,
            model="test-model",
            api_format=api_format,
            send_temperature=send_temperature,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_completed_with_status_returns_content(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": "completed",
                "model": "resp-model",
                "output": [{
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "完整正文"}],
                }],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            })

        result = await self._client(handler).generate("生成")

        self.assertEqual(result["content"], "完整正文")
        self.assertEqual(result["model"], "resp-model")
        self.assertEqual(result["token_usage"]["total_tokens"], 3)

    async def test_incomplete_does_not_return_partial_text(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [{
                    "type": "message",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": "这是被截断的半截正文"}],
                }],
            })

        with self.assertRaises(AIClientIncomplete) as ctx:
            await self._client(handler).generate("生成")

        self.assertIn("输出长度", ctx.exception.user_message)
        self.assertNotIn("这是被截断的半截正文", ctx.exception.user_message)
        self.assertNotIn("这是被截断的半截正文", str(ctx.exception))

    async def test_incomplete_content_filter_has_clear_message(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            })

        with self.assertRaises(AIClientIncomplete) as ctx:
            await self._client(handler).generate("生成")

        self.assertIn("内容安全过滤", ctx.exception.user_message)

    async def test_failed_does_not_leak_provider_message_or_api_key(self):
        api_key = "sk-test-secret-123"

        async def handler(request):
            return httpx.Response(200, json={
                "status": "failed",
                "error": {
                    "code": "server_error",
                    "message": "upstream failed for " + api_key,
                },
                "output": [{
                    "type": "message",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": "半截"}],
                }],
            })

        with self.assertRaises(AIClientResponseFailed) as ctx:
            await self._client(handler, api_key=api_key).generate("生成")

        self.assertNotIn(api_key, ctx.exception.user_message)
        self.assertNotIn(api_key, str(ctx.exception))
        self.assertNotIn("upstream failed", ctx.exception.user_message)

    async def test_missing_status_remains_backward_compatible(self):
        async def handler(request):
            return httpx.Response(200, json={"output_text": "无状态兼容正文"})

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "无状态兼容正文")

    async def test_missing_status_with_output_items_remains_compatible(self):
        async def handler(request):
            return httpx.Response(200, json={
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "无状态输出正文"}],
                }],
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "无状态输出正文")

    async def test_missing_status_with_error_marker_is_rejected(self):
        async def handler(request):
            return httpx.Response(200, json={
                "error": {"code": "server_error", "message": "upstream"},
                "output_text": "无状态但失败",
            })

        with self.assertRaises(AIClientResponseFailed):
            await self._client(handler).generate("生成")

    async def test_missing_status_with_incomplete_marker_is_rejected(self):
        async def handler(request):
            return httpx.Response(200, json={
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "无状态但不完整",
            })

        with self.assertRaises(AIClientIncomplete):
            await self._client(handler).generate("生成")

    async def test_empty_completed_output_raises_empty_response(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": "completed",
                "output": [],
            })

        with self.assertRaises(AIClientEmptyResponse) as ctx:
            await self._client(handler).generate("生成")

        self.assertIn("有效内容", ctx.exception.user_message)

    async def test_empty_chat_completions_content_raises_empty_response(self):
        async def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "   "}}],
            })

        with self.assertRaises(AIClientEmptyResponse):
            await self._client(
                handler, api_format=API_FORMAT_CHAT_COMPLETIONS
            ).generate("生成")

    async def test_queued_and_cancelled_statuses_are_not_success(self):
        for status, expected in (("queued", "尚未完成"), ("cancelled", "取消")):
            with self.subTest(status=status):
                async def handler(request, _status=status):
                    return httpx.Response(200, json={"status": _status})

                with self.assertRaises(AIClientResponseFailed) as ctx:
                    await self._client(handler).generate("生成")
                self.assertIn(expected, ctx.exception.user_message)

    async def test_incomplete_is_not_retried(self):
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            })

        with self.assertRaises(AIClientIncomplete):
            await self._client(handler).generate("生成")

        self.assertEqual(calls["n"], 1)


class TemperatureOptionTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler, api_format=API_FORMAT_RESPONSES,
                send_temperature=True):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="test-model",
            api_format=api_format,
            send_temperature=send_temperature,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    @staticmethod
    def _ok_response(request):
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json={
                "status": "completed",
                "output_text": "ok",
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    async def test_temperature_included_by_default_for_responses(self):
        captured = {}

        async def handler(request):
            captured.update(json.loads(request.content))
            return self._ok_response(request)

        await self._client(handler).generate("生成")

        self.assertEqual(captured["temperature"], 0.8)
        self.assertEqual(captured["max_output_tokens"], 2048)
        self.assertFalse(captured["store"])

    async def test_temperature_omitted_for_responses_when_disabled(self):
        captured = {}

        async def handler(request):
            captured.update(json.loads(request.content))
            return self._ok_response(request)

        await self._client(handler, send_temperature=False).generate("生成")

        self.assertNotIn("temperature", captured)
        self.assertIn("max_output_tokens", captured)
        self.assertIn("instructions", captured)

    async def test_temperature_omitted_for_chat_when_disabled(self):
        captured = {}

        async def handler(request):
            captured.update(json.loads(request.content))
            return self._ok_response(request)

        await self._client(
            handler,
            api_format=API_FORMAT_CHAT_COMPLETIONS,
            send_temperature=False,
        ).generate("生成")

        self.assertNotIn("temperature", captured)
        self.assertIn("messages", captured)
        self.assertIn("max_tokens", captured)

    async def test_chat_completions_default_behavior_is_unchanged(self):
        captured = {}

        async def handler(request):
            captured.update(json.loads(request.content))
            return self._ok_response(request)

        result = await self._client(
            handler, api_format=API_FORMAT_CHAT_COMPLETIONS
        ).generate("生成")

        self.assertEqual(result["content"], "ok")
        self.assertEqual(captured["temperature"], 0.8)
        self.assertEqual(captured["max_tokens"], 2048)
        self.assertNotIn("max_output_tokens", captured)

    async def test_temperature_flag_applies_to_all_call_paths(self):
        for api_format in (API_FORMAT_RESPONSES, API_FORMAT_CHAT_COMPLETIONS):
            for send_temperature in (True, False):
                with self.subTest(
                    api_format=api_format,
                    send_temperature=send_temperature,
                ):
                    payloads = []

                    async def handler(request):
                        payloads.append(json.loads(request.content))
                        return self._ok_response(request)

                    client = self._client(
                        handler,
                        api_format=api_format,
                        send_temperature=send_temperature,
                    )
                    await client.generate("生成")
                    await client.summarize_search("游戏", "资料")
                    await client.test_connection()

                    self.assertEqual(len(payloads), 3)
                    for payload in payloads:
                        if send_temperature:
                            self.assertIn("temperature", payload)
                        else:
                            self.assertNotIn("temperature", payload)


class ErrorLeakTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_does_not_leak_api_key(self):
        api_key = "sk-http-secret-456"

        async def handler(request):
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        client = AIClient(
            base_url="https://example.com/v1",
            api_key=api_key,
            model="test-model",
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)

        with self.assertRaises(AIClientAPIError) as ctx:
            await client.generate("生成")

        self.assertNotIn(api_key, ctx.exception.user_message)
        self.assertNotIn(api_key, str(ctx.exception))


class UntrustedResponseFieldTests(unittest.IsolatedAsyncioTestCase):
    """上游响应字段必须脱敏，只有白名单枚举可进入日志/异常。"""

    FAKE = "sk-FAKESECRET"

    def _client(self, handler):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="test-model",
            api_format=API_FORMAT_RESPONSES,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def _run(self, body, exc_type):
        async def handler(request):
            return httpx.Response(200, json=body)

        client = self._client(handler)
        with self.assertLogs("astrbot", level="WARNING") as logs:
            with self.assertRaises(exc_type) as ctx:
                await client.generate("生成")
        return ctx.exception, "\n".join(logs.output)

    def _assert_clean(self, exc, log_text):
        self.assertNotIn(self.FAKE, str(exc))
        self.assertNotIn(self.FAKE, exc.user_message)
        self.assertNotIn(self.FAKE, log_text)

    async def test_unknown_top_level_status_not_echoed(self):
        exc, logs = await self._run(
            {"status": self.FAKE}, AIClientResponseFailed
        )
        self._assert_clean(exc, logs)
        self.assertIn("未知状态", logs)
        self.assertIn("未知状态", exc.user_message)

    async def test_unknown_incomplete_reason_not_echoed(self):
        exc, logs = await self._run(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": self.FAKE},
            },
            AIClientIncomplete,
        )
        self._assert_clean(exc, logs)
        self.assertIn("不完整", exc.user_message)

    async def test_unknown_error_code_not_echoed(self):
        exc, logs = await self._run(
            {
                "status": "failed",
                "error": {"code": self.FAKE, "message": "ignored"},
            },
            AIClientResponseFailed,
        )
        self._assert_clean(exc, logs)
        self.assertIn("错误码: unknown", logs)

    async def test_error_message_not_echoed(self):
        exc, logs = await self._run(
            {
                "status": "failed",
                "error": {"code": "server_error", "message": self.FAKE},
            },
            AIClientResponseFailed,
        )
        self._assert_clean(exc, logs)
        self.assertIn("server_error", logs)

    async def test_unknown_message_item_status_not_echoed(self):
        exc, logs = await self._run(
            {
                "output": [{
                    "type": "message",
                    "status": self.FAKE,
                    "content": [{"type": "output_text", "text": "部分正文"}],
                }],
            },
            AIClientResponseFailed,
        )
        self._assert_clean(exc, logs)

    async def test_non_string_status_is_rejected_without_echo(self):
        exc, logs = await self._run(
            {
                "status": {"leak": self.FAKE},
                "output_text": "部分正文",
            },
            AIClientResponseFailed,
        )
        self._assert_clean(exc, logs)

    async def test_null_status_remains_compatible(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": None,
                "output_text": "空状态兼容正文",
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "空状态兼容正文")

    async def test_known_incomplete_reasons_have_specific_messages(self):
        expectations = {
            "max_output_tokens": "输出长度",
            "max_messages": "消息数",
            "content_filter": "内容安全过滤",
            "steered": "中途调整",
        }
        for reason, keyword in expectations.items():
            with self.subTest(reason=reason):
                async def handler(request, _reason=reason):
                    return httpx.Response(200, json={
                        "status": "incomplete",
                        "incomplete_details": {"reason": _reason},
                    })

                with self.assertRaises(AIClientIncomplete) as ctx:
                    await self._client(handler).generate("生成")
                self.assertIn(keyword, ctx.exception.user_message)

    async def test_unknown_reason_without_top_level_status_not_echoed(self):
        exc, logs = await self._run(
            {
                "incomplete_details": {"reason": self.FAKE},
                "output_text": "部分正文",
            },
            AIClientIncomplete,
        )
        self._assert_clean(exc, logs)


class MessageItemStatusTests(unittest.IsolatedAsyncioTestCase):
    """顶层无 status 时，message 子项状态决定整条响应是否可用。"""

    def _client(self, handler):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key="test-key",
            model="test-model",
            api_format=API_FORMAT_RESPONSES,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_unusable_item_status_rejects_partial_text(self):
        cases = (
            ("failed", AIClientResponseFailed),
            ("incomplete", AIClientIncomplete),
            ("queued", AIClientResponseFailed),
            ("in_progress", AIClientResponseFailed),
            ("cancelled", AIClientResponseFailed),
            ("canceled", AIClientResponseFailed),
        )
        for status, exc_type in cases:
            with self.subTest(status=status):
                partial = "PARTIAL-ITEM-TEXT"

                async def handler(request, _status=status, _partial=partial):
                    return httpx.Response(200, json={
                        "output": [{
                            "type": "message",
                            "status": _status,
                            "content": [{
                                "type": "output_text",
                                "text": _partial,
                            }],
                        }],
                    })

                with self.assertRaises(exc_type) as ctx:
                    await self._client(handler).generate("生成")

                self.assertNotIn(partial, str(ctx.exception))
                self.assertNotIn(partial, ctx.exception.user_message)

    async def test_unknown_item_status_is_rejected_without_echo(self):
        fake = "sk-FAKESECRET"

        async def handler(request):
            return httpx.Response(200, json={
                "output": [{
                    "type": "message",
                    "status": fake,
                    "content": [{"type": "output_text", "text": "部分正文"}],
                }],
            })

        with self.assertRaises(AIClientResponseFailed) as ctx:
            await self._client(handler).generate("生成")

        self.assertNotIn(fake, str(ctx.exception))
        self.assertNotIn(fake, ctx.exception.user_message)

    async def test_non_string_item_status_rejected(self):
        cases = (
            ("object", {"bad": True}),
            ("array", ["bad"]),
            ("number", 42),
            ("boolean", True),
        )
        for name, value in cases:
            with self.subTest(kind=name):
                partial = "PARTIAL-NONSTRING-TEXT"

                async def handler(request, _value=value, _partial=partial):
                    return httpx.Response(200, json={
                        "output": [{
                            "type": "message",
                            "status": _value,
                            "content": [{
                                "type": "output_text",
                                "text": _partial,
                            }],
                        }],
                    })

                with self.assertRaises(AIClientResponseFailed) as ctx:
                    await self._client(handler).generate("生成")

                self.assertNotIn(partial, str(ctx.exception))
                self.assertNotIn(partial, ctx.exception.user_message)

    async def test_non_string_item_status_sensitive_value_not_echoed(self):
        fake = "sk-FAKESECRET"
        for name, value in (("object", {"bad": fake}), ("array", [fake])):
            with self.subTest(kind=name):
                async def handler(request, _value=value):
                    return httpx.Response(200, json={
                        "output": [{
                            "type": "message",
                            "status": _value,
                            "content": [{
                                "type": "output_text",
                                "text": "部分正文",
                            }],
                        }],
                    })

                with self.assertLogs("astrbot", level="WARNING") as logs:
                    with self.assertRaises(AIClientResponseFailed) as ctx:
                        await self._client(handler).generate("生成")

                log_text = "\n".join(logs.output)
                self.assertNotIn(fake, str(ctx.exception))
                self.assertNotIn(fake, ctx.exception.user_message)
                self.assertNotIn(fake, log_text)

    async def test_null_item_status_is_treated_as_absent(self):
        async def handler(request):
            return httpx.Response(200, json={
                "output": [{
                    "type": "message",
                    "status": None,
                    "content": [{
                        "type": "output_text",
                        "text": "null 状态兼容正文",
                    }],
                }],
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "null 状态兼容正文")

    async def test_missing_item_status_remains_compatible(self):
        async def handler(request):
            return httpx.Response(200, json={
                "output": [{
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": "缺少状态兼容正文",
                    }],
                }],
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "缺少状态兼容正文")

    async def test_blank_item_status_is_treated_as_absent(self):
        async def handler(request):
            return httpx.Response(200, json={
                "output": [{
                    "type": "message",
                    "status": "   ",
                    "content": [{
                        "type": "output_text",
                        "text": "空白状态兼容正文",
                    }],
                }],
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "空白状态兼容正文")

    async def test_mixed_completed_and_incomplete_rejects_whole_response(self):
        completed_text = "COMPLETED-TEXT"
        partial_text = "PARTIAL-TEXT"

        async def handler(request):
            return httpx.Response(200, json={
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{
                            "type": "output_text",
                            "text": completed_text,
                        }],
                    },
                    {
                        "type": "message",
                        "status": "incomplete",
                        "content": [{
                            "type": "output_text",
                            "text": partial_text,
                        }],
                    },
                ],
            })

        with self.assertRaises(AIClientIncomplete) as ctx:
            await self._client(handler).generate("生成")

        self.assertNotIn(completed_text, str(ctx.exception))
        self.assertNotIn(partial_text, str(ctx.exception))
        self.assertNotIn(completed_text, ctx.exception.user_message)
        self.assertNotIn(partial_text, ctx.exception.user_message)

    async def test_mixed_statusless_and_failed_rejects_whole_response(self):
        ok_text = "OK-TEXT"

        async def handler(request):
            return httpx.Response(200, json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": ok_text}],
                    },
                    {
                        "type": "message",
                        "status": "failed",
                        "content": [{
                            "type": "output_text",
                            "text": "FAILED-TEXT",
                        }],
                    },
                ],
            })

        with self.assertRaises(AIClientResponseFailed):
            await self._client(handler).generate("生成")

    async def test_all_completed_items_are_concatenated(self):
        async def handler(request):
            return httpx.Response(200, json={
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "第一段"}],
                    },
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "第二段"}],
                    },
                ],
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "第一段\n第二段")

    async def test_mixed_completed_and_statusless_succeeds(self):
        async def handler(request):
            return httpx.Response(200, json={
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "A"}],
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "B"}],
                    },
                ],
            })

        result = await self._client(handler).generate("生成")
        self.assertEqual(result["content"], "A\nB")


class AdminConnectionTests(unittest.IsolatedAsyncioTestCase):
    """test_connection 的 success 与提示信息必须真实反映状态。"""

    def _client(self, handler, api_key="test-key"):
        client = AIClient(
            base_url="https://example.com/v1",
            api_key=api_key,
            model="test-model",
            api_format=API_FORMAT_RESPONSES,
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_connection_success(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": "completed",
                "output_text": "连接正常",
            })

        result = await self._client(handler).test_connection()

        self.assertTrue(result["success"])
        self.assertIn("模型服务正常", result["message"])

    async def test_connection_incomplete_returns_failure(self):
        async def handler(request):
            return httpx.Response(200, json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            })

        result = await self._client(handler).test_connection()

        self.assertFalse(result["success"])
        self.assertIn("输出长度", result["message"])

    async def test_connection_hides_untrusted_reason(self):
        fake = "sk-FAKESECRET"

        async def handler(request):
            return httpx.Response(200, json={
                "status": "incomplete",
                "incomplete_details": {"reason": fake},
            })

        result = await self._client(handler).test_connection()

        self.assertFalse(result["success"])
        self.assertNotIn(fake, result["message"])
        self.assertIn("不完整", result["message"])

    async def test_connection_http_error_returns_failure_without_key(self):
        api_key = "sk-http-secret-999"

        async def handler(request):
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        result = await self._client(handler, api_key=api_key).test_connection()

        self.assertFalse(result["success"])
        self.assertIn("API Key 无效", result["message"])
        self.assertNotIn(api_key, result["message"])


if __name__ == "__main__":
    unittest.main()
