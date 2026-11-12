# -*- coding: utf-8 -*-
"""多后端搜索基础测试（离线，无真实 API 请求）。"""

import asyncio
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

from astrbot_plugin_onepercent_generator.prompt import build_main_prompt  # noqa: E402
from astrbot_plugin_onepercent_generator.search_service import (  # noqa: E402
    AstrBotSearchBackend,
    BackendResult,
    SearchBackend,
    SearchService,
    TavilySearchBackend,
    WebSearchBackend,
    build_search_summary,
    format_search_diagnostic,
    prepare_search_digest,
    resolve_search_mode,
)


def item(title="T", url="https://example.com/a", snippet="S", source="test"):
    return {"title": title, "url": url, "snippet": snippet, "source": source}


class FakeBackend(SearchBackend):
    def __init__(
        self,
        name,
        status="success",
        results=None,
        reason="",
        delay=0.0,
        available=True,
        availability_reason="",
        on_search=None,
        partial=False,
        failures=None,
    ):
        self.name = name
        self.status = status
        self.results = list(results or [])
        self.reason = reason
        self.delay = delay
        self.available = available
        self.availability_reason = availability_reason
        self.on_search = on_search
        self.partial = partial
        self.failures = list(failures or [])
        self.calls = []
        self.cancelled = False

    async def availability(self, event=None):
        return self.available, self.availability_reason

    async def search(self, query, max_results, event=None, timeout_s=None):
        self.calls.append({
            "query": query,
            "max_results": max_results,
            "timeout_s": timeout_s,
        })
        if self.on_search is not None:
            return await self.on_search(query, max_results, event, timeout_s)
        if self.delay:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return BackendResult(
            self.name,
            self.status,
            self.results[:max_results],
            self.reason,
            int(self.delay * 1000),
            partial=self.partial,
            failures=self.failures,
        )


def fake_service(
    backends,
    mode="auto",
    fallback_enabled=False,
    timeout_ms=2000,
    per_backend_timeout_ms=0,
    summary_input_max_chars=6000,
):
    return SearchService(
        mode=mode,
        fallback_enabled=fallback_enabled,
        timeout_ms=timeout_ms,
        per_backend_timeout_ms=per_backend_timeout_ms,
        summary_input_max_chars=summary_input_max_chars,
        backends=backends,
    )


class ModeMigrationTests(unittest.TestCase):
    def test_legacy_disabled_forces_off(self):
        self.assertEqual(resolve_search_mode(False, "auto"), "off")
        self.assertEqual(resolve_search_mode(False, "api"), "off")
        self.assertEqual(resolve_search_mode("false", "web"), "off")

    def test_missing_mode_defaults_to_auto(self):
        self.assertEqual(resolve_search_mode(True, None), "auto")
        self.assertEqual(resolve_search_mode(True, ""), "auto")

    def test_known_modes_and_invalid_fallback(self):
        for mode in ("auto", "astrbot", "api", "web", "off"):
            self.assertEqual(resolve_search_mode(True, mode), mode)
        self.assertEqual(resolve_search_mode(True, "unknown"), "auto")


class AutoOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_prefers_astrbot(self):
        astrbot = FakeBackend("astrbot", results=[item(source="astrbot")])
        api = FakeBackend("api", results=[item(source="tavily")])
        web = FakeBackend("web", results=[item(source="bing")])
        svc = fake_service({
            "astrbot": astrbot,
            "api": api,
            "web": web,
        })
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "astrbot")
        self.assertFalse(result["degraded"])
        self.assertEqual(len(astrbot.calls), 1)
        self.assertEqual(len(api.calls), 0)
        self.assertEqual(len(web.calls), 0)
        await svc.close()

    async def test_auto_skips_unavailable_astrbot_to_api(self):
        astrbot = FakeBackend(
            "astrbot", available=False,
            availability_reason="astrbot_contract_unavailable",
        )
        api = FakeBackend("api", results=[item(source="tavily")])
        web = FakeBackend("web", results=[item(source="bing")])
        svc = fake_service({
            "astrbot": astrbot,
            "api": api,
            "web": web,
        })
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "api")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["skipped"][0]["backend"], "astrbot")
        self.assertEqual(result["skipped"][0]["status"], "unavailable")
        self.assertEqual(len(web.calls), 0)
        await svc.close()

    async def test_auto_does_not_call_api_without_key(self):
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"results": []})

        api_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        api = TavilySearchBackend(api_key="", client=api_client)
        astrbot = FakeBackend(
            "astrbot", available=False,
            availability_reason="astrbot_contract_unavailable",
        )
        web = FakeBackend("web", results=[item(source="bing")])
        svc = SearchService(
            mode="auto",
            timeout_ms=2000,
            backends={"astrbot": astrbot, "api": api, "web": web},
        )
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "web")
        self.assertEqual(calls["n"], 0)
        self.assertEqual(result["skipped"][1]["reason"], "missing_api_key")
        await svc.close()

    async def test_auto_all_unavailable_is_failed(self):
        svc = fake_service({
            "astrbot": FakeBackend("astrbot", available=False,
                                   availability_reason="x"),
            "api": FakeBackend("api", available=False,
                               availability_reason="missing_api_key"),
            "web": FakeBackend("web", status="failed", reason="verification_page"),
        })
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "verification_page")
        self.assertEqual(len(result["attempted"]), 1)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertTrue(result["degraded"])
        await svc.close()

    async def test_manual_strict_mode_does_not_fallback(self):
        api = FakeBackend("api", status="failed", reason="http_500")
        web = FakeBackend("web", results=[item(source="bing")])
        svc = fake_service(
            {"api": api, "web": web}, mode="api", fallback_enabled=False
        )
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "http_500")
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(len(web.calls), 0)
        self.assertFalse(result["degraded"])
        await svc.close()

    async def test_manual_explicit_fallback_to_web(self):
        api = FakeBackend("api", status="failed", reason="http_429")
        web = FakeBackend("web", results=[item(source="bing")])
        svc = fake_service(
            {"api": api, "web": web}, mode="api", fallback_enabled=True
        )
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "web")
        self.assertTrue(result["degraded"])
        self.assertEqual(len(web.calls), 1)
        await svc.close()

    async def test_manual_astrbot_fallback_order(self):
        astrbot = FakeBackend("astrbot", status="failed",
                              reason="astrbot_search_error")
        api = FakeBackend("api", results=[item(source="tavily")])
        web = FakeBackend("web", results=[item(source="bing")])
        svc = fake_service(
            {"astrbot": astrbot, "api": api, "web": web},
            mode="astrbot",
            fallback_enabled=True,
        )
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "api")
        self.assertEqual([a["backend"] for a in result["attempted"]],
                         ["astrbot", "api"])
        self.assertEqual(len(web.calls), 0)
        await svc.close()

    async def test_off_mode_never_calls_backends(self):
        web = FakeBackend("web", results=[item()])
        svc = fake_service({"web": web}, mode="off")
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["results"], [])
        self.assertEqual(len(web.calls), 0)
        await svc.close()

    async def test_legacy_enabled_false_overrides_mode(self):
        web = FakeBackend("web", results=[item()])
        svc = SearchService(
            enabled=False,
            mode="auto",
            timeout_ms=2000,
            backends={"web": web},
        )
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(len(web.calls), 0)
        await svc.close()


class TwoPhaseBackend(SearchBackend):
    def __init__(
        self,
        name,
        availability_delay=0.0,
        search_delay=0.0,
        results=None,
    ):
        self.name = name
        self.availability_delay = availability_delay
        self.search_delay = search_delay
        self.results = list(results or [])
        self.search_timeout_s = None
        self.availability_cancelled = False

    async def availability(self, event=None):
        try:
            if self.availability_delay:
                await asyncio.sleep(self.availability_delay)
        except asyncio.CancelledError:
            self.availability_cancelled = True
            raise
        return True, ""

    async def search(self, query, max_results, event=None, timeout_s=None):
        self.search_timeout_s = timeout_s
        if self.search_delay:
            await asyncio.sleep(self.search_delay)
        return BackendResult(
            self.name, "success", self.results[:max_results], "", 0
        )


class TimeoutAndCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_total_budget_not_per_backend(self):
        first = FakeBackend("api", status="no_results",
                            reason="empty_results", delay=0.08)
        second = FakeBackend("web", results=[item()], delay=0.08)
        svc = fake_service(
            {"api": first, "web": second},
            mode="api",
            fallback_enabled=True,
            timeout_ms=150,
            per_backend_timeout_ms=100,
        )
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "failed")
        self.assertEqual([a["status"] for a in result["attempted"]],
                         ["no_results", "timeout"])
        self.assertLess(elapsed, 0.5)
        await svc.close()

    async def test_previous_timeout_leaves_time_for_next(self):
        first = FakeBackend("api", results=[item()], delay=1.0)
        second = FakeBackend("web", results=[item(source="bing")], delay=0.05)
        svc = fake_service(
            {"api": first, "web": second},
            mode="api",
            fallback_enabled=True,
            timeout_ms=500,
            per_backend_timeout_ms=100,
        )
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "web")
        self.assertEqual(
            [a["backend"] for a in result["attempted"]],
            ["api", "web"],
        )
        self.assertEqual(
            [a["status"] for a in result["attempted"]],
            ["timeout", "success"],
        )
        self.assertTrue(result["degraded"])
        self.assertLess(elapsed, 0.5)
        await svc.close()

    async def test_two_phases_share_one_backend_budget(self):
        first = TwoPhaseBackend(
            "api", availability_delay=0.12, search_delay=1.0,
            results=[item()],
        )
        second = TwoPhaseBackend(
            "web", availability_delay=0.0, search_delay=0.01,
            results=[item(source="bing")],
        )
        svc = fake_service(
            {"api": first, "web": second},
            mode="api",
            fallback_enabled=True,
            timeout_ms=800,
            per_backend_timeout_ms=300,
        )
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "web")
        self.assertEqual(
            [a["status"] for a in result["attempted"]],
            ["timeout", "success"],
        )
        # availability 已消耗约 120ms，search 只应拿到剩余约 180ms，
        # 而不是重新获得完整的 300ms。阈值留足调度余量。
        self.assertIsNotNone(first.search_timeout_s)
        self.assertLess(first.search_timeout_s, 0.26)
        self.assertLess(elapsed, 0.7)
        await svc.close()

    async def test_availability_cancel_propagates(self):
        backend = TwoPhaseBackend("web", availability_delay=10)
        svc = fake_service({"web": backend}, mode="web", timeout_ms=5000)
        task = asyncio.create_task(svc.search_game_info("游戏"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(backend.availability_cancelled)
        await svc.close()

    async def test_timeout_reported(self):
        slow = FakeBackend("web", results=[item()], delay=1.0)
        svc = fake_service({"web": slow}, mode="web", timeout_ms=100)
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "timeout")
        self.assertEqual(result["attempted"][0]["status"], "timeout")
        self.assertLess(elapsed, 0.5)
        await svc.close()

    async def test_slow_availability_respects_total_budget(self):
        class SlowAvailability(SearchBackend):
            name = "web"

            async def availability(self, event=None):
                await asyncio.sleep(1)
                return True, ""

            async def search(self, query, max_results, event=None, timeout_s=None):
                return BackendResult("web", "success", [item()], "", 0)

        svc = fake_service(
            {"web": SlowAvailability()}, mode="web", timeout_ms=100
        )
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["skipped"][0]["reason"], "backend_budget_exhausted"
        )
        self.assertLess(elapsed, 0.5)
        await svc.close()

    async def test_availability_timeout_leaves_time_for_next(self):
        class SlowAvailability(SearchBackend):
            name = "api"

            async def availability(self, event=None):
                await asyncio.sleep(1)
                return True, ""

            async def search(self, query, max_results, event=None, timeout_s=None):
                return BackendResult("api", "success", [item()], "", 0)

        web = FakeBackend("web", results=[item(source="bing")], delay=0.01)
        svc = fake_service(
            {"api": SlowAvailability(), "web": web},
            mode="api",
            fallback_enabled=True,
            timeout_ms=500,
            per_backend_timeout_ms=100,
        )
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "web")
        self.assertEqual(result["skipped"][0]["backend"], "api")
        self.assertEqual(
            result["skipped"][0]["reason"], "backend_budget_exhausted"
        )
        self.assertLess(elapsed, 0.5)
        await svc.close()

    async def test_outer_cancellation_propagates(self):
        started = asyncio.Event()
        backend = FakeBackend("web")

        async def on_search(query, max_results, event, timeout_s):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                backend.cancelled = True
                raise
            return BackendResult("web", "success", [item()], "", 0)

        backend.on_search = on_search
        svc = fake_service({"web": backend}, mode="web", timeout_ms=5000)
        task = asyncio.create_task(svc.search_game_info("游戏"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(backend.cancelled)
        await svc.close()


class TavilyBackendTests(unittest.IsolatedAsyncioTestCase):
    def _backend(
        self,
        handler,
        api_key="tvly-TEST-ONLY",
        base_url="https://api.tavily.com",
    ):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return TavilySearchBackend(
            api_key=api_key, base_url=base_url, client=client
        )

    async def test_success_request_contract(self):
        captured = {}

        async def handler(request):
            captured["path"] = request.url.path
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "results": [{
                    "title": "Tavily Title",
                    "url": "https://example.com/tavily",
                    "content": "Tavily Snippet",
                }],
                "usage": {"credits": 1},
            })

        backend = self._backend(handler)
        result = await backend.search("游戏", 3, timeout_s=2)

        self.assertEqual(result.status, "success")
        self.assertEqual(captured["path"], "/search")
        self.assertTrue(captured["auth"].startswith("Bearer "))
        self.assertEqual(captured["body"]["search_depth"], "basic")
        self.assertEqual(captured["body"]["max_results"], 3)
        self.assertFalse(captured["body"]["include_answer"])
        self.assertEqual(result.results[0]["source"], "tavily")
        self.assertEqual(result.results[0]["snippet"], "Tavily Snippet")

    async def test_missing_key_does_not_request(self):
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"results": []})

        backend = self._backend(handler, api_key="")
        available, reason = await backend.availability()
        self.assertFalse(available)
        self.assertEqual(reason, "missing_api_key")
        self.assertEqual(calls["n"], 0)

    async def test_non_loopback_http_api_key_rejected(self):
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"results": []})

        for base_url in (
            "http://example.com",
            "http://localhost:8765",
            "http://192.168.1.10:8765",
        ):
            with self.subTest(base_url=base_url):
                backend = self._backend(handler, base_url=base_url)
                available, reason = await backend.availability()
                self.assertFalse(available)
                self.assertEqual(reason, "insecure_api_base_url")
                result = await backend.search("游戏", 3, timeout_s=2)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.reason, "insecure_api_base_url")
        self.assertEqual(calls["n"], 0)

    async def test_loopback_http_api_key_allowed_for_local_mock(self):
        captured = {}

        async def handler(request):
            captured["host"] = request.url.host
            return httpx.Response(200, json={
                "results": [{
                    "title": "Local",
                    "url": "https://example.com/local",
                    "content": "Local Snippet",
                }]
            })

        backend = self._backend(
            handler, base_url="http://127.0.0.1:8765"
        )
        available, reason = await backend.availability()
        self.assertTrue(available, reason)
        result = await backend.search("游戏", 3, timeout_s=2)
        self.assertEqual(result.status, "success")
        self.assertEqual(captured["host"], "127.0.0.1")

    async def test_loopback_http_client_ignores_proxy_env(self):
        proxy_env = {
            "HTTP_PROXY": "http://203.0.113.10:8080",
            "HTTPS_PROXY": "http://203.0.113.10:8080",
            "ALL_PROXY": "http://203.0.113.10:8080",
        }
        with patch.dict(os.environ, proxy_env, clear=True):
            backend = TavilySearchBackend(
                api_key="tvly-FAKE",
                base_url="http://127.0.0.1:8765",
            )
            client = await backend._get_client()
            try:
                self.assertFalse(getattr(client, "_trust_env", True))
            finally:
                await backend.close()

    async def test_https_client_keeps_proxy_support(self):
        proxy_env = {
            "HTTP_PROXY": "http://203.0.113.10:8080",
            "HTTPS_PROXY": "http://203.0.113.10:8080",
            "ALL_PROXY": "http://203.0.113.10:8080",
        }
        with patch.dict(os.environ, proxy_env, clear=True):
            backend = TavilySearchBackend(
                api_key="tvly-FAKE",
                base_url="https://api.tavily.com",
            )
            client = await backend._get_client()
            try:
                self.assertTrue(getattr(client, "_trust_env", False))
            finally:
                await backend.close()

    async def test_client_init_error_is_sanitized(self):
        fake_key = "tvly-FAKESECRET"
        with patch(
            "httpx.AsyncClient",
            side_effect=httpx.InvalidURL("bad proxy config"),
        ):
            backend = TavilySearchBackend(
                api_key=fake_key,
                base_url="https://api.tavily.com",
            )
            with self.assertLogs("astrbot", level="WARNING") as logs:
                result = await backend.search("游戏", 3, timeout_s=1)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "client_init_error")
        self.assertNotIn(fake_key, str(result))
        self.assertNotIn(fake_key, "\n".join(logs.output))

    async def test_request_invalid_url_is_sanitized(self):
        fake_key = "tvly-FAKESECRET"

        async def handler(request):
            raise httpx.InvalidURL("bad proxy config")

        backend = self._backend(handler, api_key=fake_key)
        with self.assertLogs("astrbot", level="WARNING") as logs:
            result = await backend.search("游戏", 3, timeout_s=1)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "client_init_error")
        self.assertNotIn(fake_key, str(result))
        self.assertNotIn(fake_key, "\n".join(logs.output))

    async def test_http_error_does_not_leak_key(self):
        fake_key = "tvly-FAKESECRET"

        async def handler(request):
            return httpx.Response(
                401,
                json={"detail": {"error": f"invalid key {fake_key}"}},
            )

        backend = self._backend(handler, api_key=fake_key)
        with self.assertLogs("astrbot", level="WARNING") as logs:
            result = await backend.search("游戏", 3, timeout_s=2)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "http_401")
        self.assertNotIn(fake_key, str(result))
        self.assertNotIn(fake_key, "\n".join(logs.output))

    async def test_no_retry_on_server_error(self):
        calls = {"n": 0}

        async def handler(request):
            calls["n"] += 1
            return httpx.Response(500, text="server error")

        backend = self._backend(handler)
        result = await backend.search("游戏", 3, timeout_s=2)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "http_5xx")
        self.assertEqual(calls["n"], 1)

    async def test_empty_results(self):
        async def handler(request):
            return httpx.Response(200, json={"results": []})

        result = await self._backend(handler).search("游戏", 3, timeout_s=2)
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "empty_results")

    async def test_invalid_json(self):
        async def handler(request):
            return httpx.Response(200, text="not json")

        result = await self._backend(handler).search("游戏", 3, timeout_s=2)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "invalid_response")

    async def test_timeout(self):
        async def handler(request):
            raise httpx.TimeoutException("timeout", request=request)

        result = await self._backend(handler).search(
            "游戏", 3, timeout_s=0.05
        )
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.reason, "timeout")


class WebBackendTests(unittest.IsolatedAsyncioTestCase):
    BING_OK = (
        '<html><body><ol>'
        '<li class="b_algo"><h2><a href="https://example.com/bing">'
        'Bing Title</a></h2><p>Bing Snippet</p></li>'
        '</ol></body></html>'
    )
    BAIDU_OK = (
        '<html><body>'
        '<h3><a href="https://example.com/baidu">Baidu Title</a></h3>'
        '<div class="c-abstract">Baidu Snippet</div>'
        '</body></html>'
    )
    BING_NESTED = (
        '<html><body><ol>'
        '<li class="b_algo"><h2><a href="https://example.com/nested">'
        'Nested Title</a></h2>'
        '<ul><li>nested child</li></ul>'
        '<p>Main Snippet after nested list</p></li>'
        '</ol></body></html>'
    )
    BING_MULTI = (
        '<html><body><ol>'
        '<li class="b_algo"><h2>'
        '<a href="https://example.com/one">One Title</a></h2>'
        '<p>One Snippet</p></li>'
        '<li>普通列表项，不是搜索结果</li>'
        '<li class="b_algo"><h2>'
        '<a href="https://example.com/two">Two Title</a></h2>'
        '<ul><li>Two nested child</li></ul>'
        '<p>Two Snippet</p></li>'
        '</ol></body></html>'
    )
    BING_ENTITIES = (
        '<html><body><ol>'
        '<li class="b_algo"><h2>'
        '<a href="https://example.com/entities">Entity &amp; Title</a></h2>'
        '<p>Temperature &lt; 10 &gt; 5; '
        '&amp;lt;literal&amp;gt; 中文 42 &amp; done</p></li>'
        '<li>无关普通列表项</li>'
        '<li class="b_algo"><h2>'
        '<a href="https://example.com/more?a=1&amp;b=2">'
        'Second &amp; More</a></h2>'
        '<p>中文<span>与</span>数字 42 &lt; 10</p></li>'
        '</ol></body></html>'
    )
    BING_NO_SNIPPET = (
        '<html><body><ol>'
        '<li class="b_algo"><h2>'
        '<a href="https://example.com/no-snippet">No Snippet</a>'
        '</h2></li>'
        '<li class="b_algo"><h2>'
        '<a href="https://example.com/next">Next Title</a></h2>'
        '<p>Next Snippet</p></li>'
        '</ol></body></html>'
    )

    def _service(self, handler, mode="web", timeout_ms=2000):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        svc = SearchService(
            mode=mode,
            timeout_ms=timeout_ms,
            result_count=3,
            web_client=client,
        )
        self.addAsyncCleanup(svc.close)
        return svc

    def _backend(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        return WebSearchBackend(client=client)

    async def test_valid_web_results(self):
        paths = []

        async def handler(request):
            paths.append(request.url.path)
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            return httpx.Response(200, text=self.BAIDU_OK)

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "web")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual({i["source"] for i in result["results"]},
                         {"bing", "baidu"})
        # 只请求搜索页，不抓取结果链接正文。
        self.assertTrue(all(p in ("/search", "/s") for p in paths))

    async def test_verification_page_is_not_success(self):
        async def handler(request):
            return httpx.Response(
                200,
                text="<html><title>安全验证</title><body>请完成验证码后继续</body></html>",
            )

        result = await self._service(handler).search_game_info("游戏")

        self.assertNotEqual(result["status"], "success")
        self.assertEqual(result["failure_reason"], "verification_page")

    async def test_empty_page_is_not_success(self):
        async def handler(request):
            return httpx.Response(200, text="<html><body></body></html>")

        result = await self._service(handler).search_game_info("游戏")

        self.assertNotEqual(result["status"], "success")
        self.assertIn(result["failure_reason"],
                      {"empty_results", "no_results"})

    async def test_partial_source_failure_still_success(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            return httpx.Response(
                200, text="<html><body>安全验证</body></html>"
            )

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["source"], "bing")
        self.assertTrue(result["partial"])
        self.assertEqual(result["partial_failures"][0]["source"], "baidu")
        self.assertEqual(
            result["partial_failures"][0]["reason"], "verification_page"
        )

    async def test_bing_ok_baidu_timeout_is_partial(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            await asyncio.sleep(0.02)
            raise httpx.TimeoutException("timeout", request=request)

        svc = self._service(handler)
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["partial"])
        self.assertTrue(result["degraded"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["partial_failures"], [{
            "source": "baidu",
            "status": "timeout",
            "reason": "timeout",
        }])
        diag = await svc.test_search("游戏")
        self.assertIn("部分可用：是", diag["message"])
        self.assertIn("百度", diag["message"])
        self.assertIn("超时", diag["message"])

    async def test_bing_ok_baidu_429_is_partial(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            return httpx.Response(429, text="rate limited")

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["partial"])
        self.assertEqual(
            result["partial_failures"][0]["reason"], "http_429"
        )
        self.assertEqual(result["partial_failures"][0]["source"], "baidu")

    async def test_normal_result_with_verification_word_is_success(self):
        page = (
            '<html><body><ol><li class="b_algo">'
            '<h2><a href="https://example.com/normal">验证码攻略</a></h2>'
            '<p>正常搜索结果摘要，提到验证码但不影响阅读</p>'
            '</li></ol></body></html>'
        )

        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=page)
            return httpx.Response(429, text="rate limited")

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"][0]["title"], "验证码攻略")
        self.assertNotEqual(result["failure_reason"], "verification_page")

    async def test_bing_multiple_results_keep_own_title_url_snippet(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_MULTI)
            return httpx.Response(429, text="rate limited")

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 2)
        first, second = result["results"]
        self.assertEqual(first["title"], "One Title")
        self.assertEqual(first["url"], "https://example.com/one")
        self.assertEqual(first["snippet"], "One Snippet")
        self.assertEqual(second["title"], "Two Title")
        self.assertEqual(second["url"], "https://example.com/two")
        self.assertEqual(second["snippet"], "Two Snippet")
        self.assertNotIn("普通列表项", first["snippet"])
        self.assertNotIn("普通列表项", second["snippet"])

    async def test_bing_entities_are_decoded_once_and_not_stripped(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_ENTITIES)
            return httpx.Response(429, text="rate limited")

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 2)
        first, second = result["results"]
        self.assertEqual(first["title"], "Entity & Title")
        self.assertEqual(
            first["snippet"],
            "Temperature < 10 > 5; &lt;literal&gt; 中文 42 & done",
        )
        self.assertEqual(second["title"], "Second & More")
        self.assertEqual(
            second["url"], "https://example.com/more?a=1&b=2"
        )
        self.assertEqual(second["snippet"], "中文与数字 42 < 10")
        self.assertNotIn("无关普通列表项", first["snippet"])
        self.assertNotIn("无关普通列表项", second["snippet"])

    async def test_bing_missing_snippet_does_not_take_neighbor_text(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_NO_SNIPPET)
            return httpx.Response(429, text="rate limited")

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["snippet"], "")
        self.assertEqual(result["results"][1]["snippet"], "Next Snippet")

    async def test_bing_nested_list_keeps_snippet_after_nested_li(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_NESTED)
            return httpx.Response(429, text="rate limited")

        result = await self._service(handler).search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"][0]["title"], "Nested Title")
        self.assertIn(
            "Main Snippet after nested list",
            result["results"][0]["snippet"],
        )

    async def test_fast_source_survives_slow_source_async_cleanup(self):
        cleanup_done = []

        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            try:
                await asyncio.sleep(10)
            finally:
                await asyncio.sleep(0.05)
                cleanup_done.append("baidu")

        svc = self._service(handler, timeout_ms=100)
        start = time.monotonic()
        result = await svc.search_game_info("游戏")
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["partial_failures"][0]["source"], "baidu")
        self.assertEqual(result["partial_failures"][0]["reason"], "timeout")
        self.assertFalse(result["cleanup_incomplete"])
        self.assertTrue(cleanup_done)
        self.assertLess(elapsed, 0.15)

    async def test_bing_success_baidu_slow_is_partial(self):
        cancelled = []

        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append("baidu")
                raise
            return httpx.Response(200, text=self.BAIDU_OK)

        backend = self._backend(handler)
        start = time.monotonic()
        result = await backend.search("游戏", 3, timeout_s=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(result.status, "success")
        self.assertTrue(result.partial)
        self.assertEqual(result.failures, [{
            "source": "baidu",
            "status": "timeout",
            "reason": "timeout",
        }])
        self.assertEqual(len(result.results), 1)
        self.assertLess(elapsed, 0.5)
        self.assertIn("baidu", cancelled)

    async def test_baidu_success_bing_slow_is_partial(self):
        cancelled = []

        async def handler(request):
            if request.url.host == "www.baidu.com":
                return httpx.Response(200, text=self.BAIDU_OK)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append("bing")
                raise
            return httpx.Response(200, text=self.BING_OK)

        backend = self._backend(handler)
        start = time.monotonic()
        result = await backend.search("游戏", 3, timeout_s=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(result.status, "success")
        self.assertTrue(result.partial)
        self.assertEqual(result.failures[0]["source"], "bing")
        self.assertEqual(result.failures[0]["reason"], "timeout")
        self.assertEqual(len(result.results), 1)
        self.assertLess(elapsed, 0.5)
        self.assertIn("bing", cancelled)

    async def test_one_source_429_is_partial(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            return httpx.Response(429, text="rate limited")

        result = await self._backend(handler).search(
            "游戏", 3, timeout_s=0.5
        )

        self.assertEqual(result.status, "success")
        self.assertTrue(result.partial)
        self.assertEqual(result.failures, [{
            "source": "baidu",
            "status": "failed",
            "reason": "http_429",
        }])

    async def test_both_sources_timeout(self):
        cancelled = []

        async def handler(request):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(request.url.host)
                raise
            return httpx.Response(200)

        backend = self._backend(handler)
        start = time.monotonic()
        result = await backend.search("游戏", 3, timeout_s=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "timeout")
        self.assertEqual(len(result.failures), 2)
        self.assertEqual(
            {item["reason"] for item in result.failures}, {"timeout"}
        )
        self.assertLess(elapsed, 0.5)
        self.assertEqual(
            set(cancelled), {"www.bing.com", "www.baidu.com"}
        )

    async def test_external_cancel_cleans_all_sources(self):
        started = {
            "www.bing.com": asyncio.Event(),
            "www.baidu.com": asyncio.Event(),
        }
        cancelled = []

        async def handler(request):
            started[request.url.host].set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(request.url.host)
                raise
            return httpx.Response(200)

        backend = self._backend(handler)
        task = asyncio.create_task(backend.search("游戏", 3, timeout_s=5))
        await asyncio.gather(*(event.wait() for event in started.values()))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            set(cancelled), {"www.bing.com", "www.baidu.com"}
        )

    async def test_web_backend_direct(self):
        async def handler(request):
            if request.url.host == "www.bing.com":
                return httpx.Response(200, text=self.BING_OK)
            return httpx.Response(200, text=self.BAIDU_OK)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        backend = WebSearchBackend(client=client)
        result = await backend.search("游戏", 3, timeout_s=1)
        self.assertEqual(result.status, "success")
        self.assertTrue(all(i["url"] for i in result.results))


class DiagnosticAndBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_digest_prefers_ai_then_raw_and_truncates(self):
        self.assertEqual(
            prepare_search_digest("raw" * 100, "", 20),
            ("raw" * 100)[:20],
        )
        digest = prepare_search_digest("raw" * 100, "ai" * 100, 50)
        self.assertEqual(len(digest), 50)
        self.assertTrue(digest.startswith("ai"))
        self.assertEqual(prepare_search_digest("", "", 10), "")

    def test_summary_uses_structured_fields(self):
        text = build_search_summary([{
            "title": "标题",
            "url": "https://example.com/a",
            "snippet": "摘要",
            "source": "tavily",
        }])
        self.assertIn("[tavily] 标题", text)
        self.assertIn("摘要", text)
        self.assertIn("https://example.com/a", text)

    def test_long_snippet_does_not_crowd_out_later_items(self):
        text = build_search_summary([
            item(title="First", snippet="长" * 10000),
            item(title="Second", snippet="后面的有效摘要"),
        ], max_total_chars=400)

        self.assertLessEqual(len(text), 400)
        self.assertIn("First", text)
        self.assertIn("Second", text)
        self.assertIn("后面的有效摘要", text)
        self.assertNotIn("长" * 1000, text)

    def test_long_url_is_omitted_but_title_is_kept(self):
        long_url = "https://example.com/" + "a" * 4000
        text = build_search_summary([
            item(title="Long", url=long_url),
            item(title="OK", url="https://example.com/ok"),
        ], max_total_chars=400)

        self.assertIn("Long", text)
        self.assertIn("OK", text)
        self.assertNotIn(long_url, text)
        self.assertNotIn(long_url[:80], text)
        self.assertLessEqual(len(text), 400)

    def test_total_budget_200_five_long_results_keep_headers_and_snippets(self):
        results = [
            item(
                title=f"Five-{index}-" + "T" * 300,
                url=f"https://example.com/{index}/" + "u" * 1000,
                snippet=f"Snippet-{index}-" + "S" * 3000,
                source=f"s{index}",
            )
            for index in range(5)
        ]
        text = build_search_summary(results, max_total_chars=200)

        self.assertLessEqual(len(text), 200)
        for index in range(5):
            self.assertIn(f"Five-{index}", text)
            self.assertIn(f"[s{index}]", text)
            self.assertIn(f"Snippet-{index}", text)
        self.assertNotIn("https://", text)

    def test_total_budget_200_three_long_results_keep_headers(self):
        results = [
            item(
                title=f"Title-{index}-" + "T" * 300,
                url=f"https://example.com/{index}/" + "u" * 1000,
                snippet="S" * 3000,
                source=f"s{index}",
            )
            for index in range(3)
        ]
        text = build_search_summary(results, max_total_chars=200)

        self.assertLessEqual(len(text), 200)
        for index in range(3):
            self.assertIn(f"Title-{index}", text)
            self.assertIn(f"[s{index}]", text)
        self.assertNotIn("https://", text)

    def test_default_budget_keeps_multiple_results_and_respects_total(self):
        results = [
            item(
                title=f"Default-{index}-" + "T" * 300,
                url=f"https://example.com/{index}",
                snippet="S" * 3000,
                source=f"s{index}",
            )
            for index in range(4)
        ]
        text = build_search_summary(results, max_total_chars=6000)

        self.assertLessEqual(len(text), 6000)
        for index in range(4):
            self.assertIn(f"Default-{index}", text)
            self.assertIn(f"[s{index}]", text)

    def test_empty_fields_are_not_fabricated(self):
        text = build_search_summary([{
            "title": "Only Title",
            "url": "",
            "snippet": "",
            "source": "",
        }], max_total_chars=200)

        self.assertEqual(text, "Only Title")

    def test_tiny_budget_is_deterministic_and_bounded(self):
        results = [
            item(title="First", source="s0"),
            item(title="Second", source="s1"),
            item(title="Third", source="s2"),
        ]
        text = build_search_summary(results, max_total_chars=3)

        self.assertLessEqual(len(text), 3)
        self.assertEqual(text, "Fir")

    def test_extremely_long_title_and_url_are_bounded(self):
        text = build_search_summary([{
            "title": "Title-" + "T" * 2000,
            "url": "https://example.com/" + "u" * 5000,
            "snippet": "S" * 5000,
            "source": "tavily",
        }], max_total_chars=300)

        self.assertLessEqual(len(text), 300)
        self.assertTrue(text.startswith("[tavily] Title-"))
        self.assertNotIn("https://example.com/", text)

    async def test_search_service_summary_input_budget(self):
        backend = FakeBackend("web", results=[
            item(title="A", snippet="x" * 3000),
            item(title="B", snippet="y" * 3000),
        ])
        svc = fake_service(
            {"web": backend}, mode="web", summary_input_max_chars=300
        )
        result = await svc.search_game_info("游戏")

        self.assertEqual(result["status"], "success")
        self.assertLessEqual(len(result["summary"]), 300)
        self.assertIn("A", result["summary"])
        self.assertIn("B", result["summary"])
        await svc.close()

    async def test_admin_diagnostic_marks_partial_source(self):
        backend = FakeBackend(
            "web",
            results=[item(source="bing")],
            partial=True,
            failures=[{
                "source": "baidu",
                "status": "timeout",
                "reason": "timeout",
            }],
        )
        svc = fake_service({"web": backend}, mode="web")
        diag = await svc.test_search("游戏")

        self.assertTrue(diag["success"])
        self.assertTrue(diag["partial"])
        self.assertIn("部分可用：是", diag["message"])
        self.assertIn("百度（超时）", diag["message"])
        await svc.close()

    def test_prompt_receives_budgeted_digest(self):
        raw = "长" * 500
        digest = prepare_search_digest(raw, "", 30)
        prompt = build_main_prompt("游戏", digest)
        self.assertIn(digest, prompt)
        self.assertNotIn(raw, prompt)

    async def test_admin_diagnostic_success(self):
        api = FakeBackend("api", results=[item(source="tavily")])
        svc = fake_service({"api": api}, mode="api")
        diag = await svc.test_search("游戏")

        self.assertTrue(diag["success"])
        self.assertIn("后端：Tavily API", diag["message"])
        self.assertIn("有效结果：1", diag["message"])
        self.assertIn("降级：否", diag["message"])
        self.assertEqual(diag["result_count"], 1)
        await svc.close()

    async def test_admin_diagnostic_shows_skipped_backends(self):
        svc = fake_service({
            "astrbot": FakeBackend(
                "astrbot", available=False,
                availability_reason="astrbot_contract_unavailable",
            ),
            "api": FakeBackend(
                "api", available=False,
                availability_reason="missing_api_key",
            ),
            "web": FakeBackend(
                "web", status="failed", reason="verification_page"
            ),
        }, mode="auto")
        diag = await svc.test_search("游戏")

        self.assertFalse(diag["success"])
        self.assertIn("AstrBot", diag["message"])
        self.assertIn("Tavily API", diag["message"])
        self.assertIn("跳过", diag["message"])
        self.assertEqual(len(diag["skipped"]), 2)
        await svc.close()

    async def test_admin_diagnostic_failure_includes_reason(self):
        web = FakeBackend("web", status="failed", reason="verification_page")
        svc = fake_service({"web": web}, mode="web")
        diag = await svc.test_search("游戏")

        self.assertFalse(diag["success"])
        self.assertIn("搜索失败", diag["message"])
        self.assertIn("后端：无", diag["message"])
        self.assertIn("有效结果：0", diag["message"])
        self.assertIn("降级：否", diag["message"])
        self.assertIn("验证页", diag["message"])
        self.assertIn("网页搜索", diag["message"])
        self.assertEqual(diag["failure_reason"], "verification_page")
        await svc.close()

    def test_disabled_diagnostic(self):
        result = {
            "status": "disabled",
            "results": [],
            "attempted": [],
            "failure_reason": "disabled",
        }
        message = format_search_diagnostic(result)
        self.assertIn("搜索未启用", message)
        self.assertIn("后端：无", message)
        self.assertIn("有效结果：0", message)


class FakeManagedTool:
    def __init__(self, active=True):
        self.active = active
        self.calls = []

    async def call(self, context, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({
            "results": [{
                "title": "Managed",
                "url": "https://example.com/managed",
                "snippet": "Managed Snippet",
            }]
        })


class FakeToolManager:
    def __init__(self, registered=True, tool=None):
        self.registered = registered
        self.tool = tool or FakeManagedTool()
        self.requested = []

    def is_builtin_tool(self, name):
        return self.registered

    def get_builtin_tool(self, name):
        self.requested.append(name)
        if not self.registered:
            raise KeyError(name)
        return self.tool


class ManagedContext:
    def __init__(self, cfg, manager):
        self.cfg = cfg
        self.manager = manager

    def get_config(self, umo=None):
        return self.cfg

    def get_llm_tool_manager(self):
        return self.manager


class ManagedEvent:
    unified_msg_origin = "webchat:managed-test"


class AstrBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _fake_astrbot_modules():
        astrbot = types.ModuleType("astrbot")
        astrbot.__version__ = "4.29.4"
        core = types.ModuleType("astrbot.core")
        agent = types.ModuleType("astrbot.core.agent")
        run_context = types.ModuleType("astrbot.core.agent.run_context")

        class FakeWrapper:
            def __init__(self, context):
                self.context = context

        class FakeAgentContext:
            def __init__(self, context=None, event=None):
                self.context = context
                self.event = event

        run_context.ContextWrapper = FakeWrapper
        astr_agent_context = types.ModuleType(
            "astrbot.core.astr_agent_context"
        )
        astr_agent_context.AstrAgentContext = FakeAgentContext
        return patch.dict(sys.modules, {
            "astrbot": astrbot,
            "astrbot.core": core,
            "astrbot.core.agent": agent,
            "astrbot.core.agent.run_context": run_context,
            "astrbot.core.astr_agent_context": astr_agent_context,
        })

    @staticmethod
    def _cfg(**overrides):
        settings = {
            "web_search": True,
            "websearch_provider": "tavily",
            "websearch_tavily_key": ["tvly-FAKE"],
        }
        settings.update(overrides)
        return {"provider_settings": settings}

    async def test_managed_tool_unregistered_is_unavailable(self):
        context = ManagedContext(
            self._cfg(), FakeToolManager(registered=False)
        )
        with self._fake_astrbot_modules():
            backend = AstrBotSearchBackend(context=context)
            available, reason = await backend.availability(ManagedEvent())

        self.assertFalse(available)
        self.assertEqual(reason, "astrbot_tool_unregistered")

    async def test_managed_tool_disabled_is_unavailable(self):
        tool = FakeManagedTool(active=False)
        context = ManagedContext(
            self._cfg(), FakeToolManager(tool=tool)
        )
        with self._fake_astrbot_modules():
            backend = AstrBotSearchBackend(context=context)
            available, reason = await backend.availability(ManagedEvent())
            result = await backend.search(
                "游戏", 3, event=ManagedEvent(), timeout_s=1
            )

        self.assertFalse(available)
        self.assertEqual(reason, "astrbot_tool_disabled")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "astrbot_search_error")
        self.assertEqual(len(tool.calls), 0)

    async def test_managed_tool_enabled_is_available(self):
        tool = FakeManagedTool(active=True)
        context = ManagedContext(
            self._cfg(), FakeToolManager(tool=tool)
        )
        with self._fake_astrbot_modules():
            backend = AstrBotSearchBackend(context=context)
            available, reason = await backend.availability(ManagedEvent())

        self.assertTrue(available, reason)
        self.assertEqual(reason, "")

    async def test_search_uses_managed_tool_instance(self):
        tool = FakeManagedTool(active=True)
        manager = FakeToolManager(tool=tool)
        context = ManagedContext(self._cfg(), manager)
        with self._fake_astrbot_modules():
            backend = AstrBotSearchBackend(context=context)
            result = await backend.search(
                "游戏", 3, event=ManagedEvent(), timeout_s=1
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.results[0]["source"], "astrbot")
        self.assertEqual(manager.requested, ["web_search_tavily"])
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(tool.calls[0]["max_results"], 5)

    async def test_config_profile_mismatch_is_unavailable(self):
        cases = [
            (self._cfg(web_search=False), "astrbot_search_disabled"),
            (self._cfg(websearch_provider="serpapi"),
             "astrbot_provider_unverified"),
            (self._cfg(websearch_tavily_key=[]), "astrbot_key_missing"),
            (self._cfg(websearch_tavily_key=[""]), "astrbot_key_missing"),
            (self._cfg(websearch_tavily_key=["k1", "k2"]),
             "astrbot_retry_risk"),
        ]
        with self._fake_astrbot_modules():
            for cfg, expected in cases:
                with self.subTest(reason=expected):
                    backend = AstrBotSearchBackend(
                        context=ManagedContext(cfg, FakeToolManager())
                    )
                    available, reason = await backend.availability(
                        ManagedEvent()
                    )
                    self.assertFalse(available)
                    self.assertEqual(reason, expected)

    async def test_injected_adapter_returns_structured_results(self):
        async def adapter(query, max_results, event):
            return json.dumps({
                "results": [{
                    "title": "AstrBot Title",
                    "url": "https://example.com/astrbot",
                    "snippet": "AstrBot Snippet",
                }]
            })

        backend = AstrBotSearchBackend(adapter=adapter)
        available, reason = await backend.availability()
        self.assertTrue(available)
        result = await backend.search("游戏", 3)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.results[0]["source"], "astrbot")

    async def test_adapter_error_is_sanitized(self):
        async def adapter(query, max_results, event):
            return "Error: secret details"

        backend = AstrBotSearchBackend(adapter=adapter)
        result = await backend.search("游戏", 3)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "astrbot_search_error")
        self.assertNotIn("secret details", str(result))

    async def test_missing_context_is_unavailable(self):
        backend = AstrBotSearchBackend(context=None)
        available, reason = await backend.availability(None)
        self.assertFalse(available)
        self.assertEqual(reason, "astrbot_context_missing")

    async def test_unverified_environment_is_unavailable(self):
        try:
            import astrbot  # type: ignore
            if getattr(astrbot, "__version__", "") in {"4.29.4"}:
                self.skipTest("AstrBot 4.29.4 已安装，契约适配器可用")
        except Exception:
            pass

        class Cfg(dict):
            pass

        class Context:
            def get_config(self, umo=None):
                return Cfg({
                    "provider_settings": {
                        "web_search": True,
                        "websearch_provider": "tavily",
                        "websearch_tavily_key": ["tvly-FAKE"],
                    }
                })

        class Event:
            unified_msg_origin = "webchat:test"

        backend = AstrBotSearchBackend(context=Context())
        available, reason = await backend.availability(Event())
        self.assertFalse(available)
        self.assertIn(reason, {
            "astrbot_contract_unavailable",
            "astrbot_version_unverified",
        })


if __name__ == "__main__":
    unittest.main()
