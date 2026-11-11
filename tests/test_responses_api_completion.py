# -*- coding: utf-8 -*-
"""Responses API 状态校验与 temperature 兼容性测试。"""

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


if __name__ == "__main__":
    unittest.main()
