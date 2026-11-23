# -*- coding: utf-8 -*-
"""Anysearch 插件适配的单元测试（工具为轻量模拟，不发起网络请求）。"""

import asyncio
import json
import unittest

from astrbot_plugin_onepercent_generator.search_service import (
    AnysearchSearchBackend,
    SearchService,
    _anysearch_structured_results,
)


class FakeTool:
    active = True
    name = "anysearch_search"
    __module__ = "astrbot_plugin_anysearch.tools"

    def __init__(self, result=None, delay=0.0, error=None):
        self.result = result
        self.delay = delay
        self.error = error
        self.calls = []

    async def run(self, event, query, max_results=5):
        self.calls.append((event, query, max_results))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class FakeManager:
    def __init__(self, tool):
        self.func_list = [tool] if tool is not None else []


class FakeContext:
    def __init__(self, tool):
        self.tool = tool

    def get_llm_tool_manager(self):
        return FakeManager(self.tool)


class FakeEvent:
    unified_msg_origin = "qq_official:group:1"


class FakeBackend:
    def __init__(self, name, available=False, reason="unavailable"):
        self.name = name
        self.available = available
        self.reason = reason
        self.calls = 0

    async def availability(self, event=None):
        return self.available, self.reason

    async def search(self, query, max_results, event=None, timeout_s=None):
        self.calls += 1
        raise AssertionError("should not be searched")

    async def close(self):
        pass


def anysearch_json(count=2):
    return json.dumps({
        "results": [
            {
                "title": f"结果{i}",
                "url": f"https://example.com/{i}",
                "snippet": f"摘要{i}",
            }
            for i in range(count)
        ]
    }, ensure_ascii=False)


class AnysearchBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_real_tool_contract_and_parses_json(self):
        tool = FakeTool(anysearch_json())
        backend = AnysearchSearchBackend(context=FakeContext(tool))

        available, reason = await backend.availability(FakeEvent())
        result = await backend.search("原神", 2, event=FakeEvent())

        self.assertTrue(available, reason)
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.results), 2)
        self.assertEqual(tool.calls[0][1], "原神")
        self.assertEqual(tool.calls[0][2], 2)

    async def test_missing_plugin_is_unavailable(self):
        backend = AnysearchSearchBackend(context=FakeContext(None))
        available, reason = await backend.availability(FakeEvent())
        self.assertFalse(available)
        self.assertEqual(reason, "anysearch_plugin_not_installed")

    async def test_disabled_tool_is_unavailable(self):
        tool = FakeTool(anysearch_json())
        tool.active = False
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        available, reason = await backend.availability(FakeEvent())
        self.assertFalse(available)
        self.assertEqual(reason, "anysearch_tool_disabled")

    async def test_error_text_is_failure_not_success(self):
        tool = FakeTool("错误：Unauthorized")
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 2, event=FakeEvent())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "anysearch_error")

    async def test_empty_result_is_no_results(self):
        tool = FakeTool("")
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 2, event=FakeEvent())
        self.assertEqual(result.status, "no_results")

    async def test_markdown_links_are_normalized(self):
        tool = FakeTool(
            "[标题](https://example.com/a)\n摘要 A\n\n"
            "[标题2](https://example.com/b)\n摘要 B"
        )
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 2, event=FakeEvent())
        self.assertEqual(result.status, "success")
        self.assertEqual(result.results[0]["url"], "https://example.com/a")

    async def test_section_markdown_from_real_tool_is_parsed(self):
        # 审查回归：真实 AnySearch v0.3 返回分节 Markdown（标题 +
        # “- **URL**:” + 摘要），不是 JSON 也不是 [标题](链接)。
        # 样本按公开响应结构脱敏改写，保留原有排版。
        tool = FakeTool(
            "## 星露谷物语 玩法与平台\n"
            "- **URL**: https://example.com/stardew\n"
            "- **摘要**: 星露谷物语是一款农场经营游戏，支持 PC、Switch。\n"
            "\n"
            "## 星露谷物语 玩家评价\n"
            "- **URL**: https://example.com/review\n"
            "玩家普遍认为节奏舒缓，适合喜欢经营和收集的人。\n"
        )
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("星露谷物语 玩法 评测", 2, event=FakeEvent())

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.results[0]["url"], "https://example.com/stardew")
        self.assertEqual(result.results[0]["title"], "星露谷物语 玩法与平台")
        self.assertIn("农场经营", result.results[0]["snippet"])
        self.assertEqual(result.results[1]["title"], "星露谷物语 玩家评价")
        self.assertEqual(result.results[1]["source"], "anysearch")

    async def test_section_markdown_dedupes_and_skips_url_less_sections(self):
        text = (
            "1. 星露谷物语 介绍\n"
            "- **URL**: https://example.com/a\n"
            "介绍段落。\n"
            "2. 重复结果\n"
            "**URL**：https://example.com/a\n"
            "重复摘要。\n"
            "3. 没有链接的分节\n"
            "这段没有 URL，不能产出结果。\n"
        )
        results = _anysearch_structured_results(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/a")

    async def test_legal_empty_json_is_no_results_not_unparsed(self):
        for payload in ('{"results": []}', "[]"):
            tool = FakeTool(payload)
            backend = AnysearchSearchBackend(context=FakeContext(tool))
            result = await backend.search("原神", 3, event=FakeEvent())
            self.assertEqual(result.status, "no_results", payload)
            self.assertEqual(result.reason, "empty_results", payload)

    async def test_markdown_declaring_zero_results_is_no_results(self):
        tool = FakeTool("未找到相关结果，换个关键词试试。")
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "empty_results")

    async def test_markdown_with_content_but_no_link_is_unparsed(self):
        tool = FakeTool("## 标题\n有正文内容，但没有任何链接。")
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "unparsed_results")

    async def test_zero_results_header_is_empty(self):
        tool = FakeTool("## Search Results (0 results, 100ms)")
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "empty_results")

    async def test_native_empty_list_is_empty(self):
        # 审查回归：插件直接返回原生空数组（不是字符串 "[]"）。
        tool = FakeTool([])
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "empty_results")

    async def test_mcp_wrapped_empty_list_is_empty(self):
        tool = FakeTool({
            "content": [{"type": "text", "text": "[]"}],
        })
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "empty_results")

    async def test_mcp_wrapped_zero_results_header_is_empty(self):
        tool = FakeTool({
            "content": [{
                "type": "text",
                "text": "## Search Results (0 results, 88ms)",
            }],
        })
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "empty_results")

    async def test_native_empty_list_with_declared_items_stays_unparsed(self):
        # 有条目但没有链接/标题时仍是解析失败，不能被空数组规则吞掉。
        tool = FakeTool([
            {"summary": "没有链接的条目"},
        ])
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "unparsed_results")

    async def test_declared_results_without_links_is_unparsed_even_with_phrase(self):
        tool = FakeTool(
            "## Search Results (2 results, 120ms)\n"
            "1. 玩家讨论存档问题\n"
            "摘要里提到没有找到存档，但这条结果本身没有链接。"
        )
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 3, event=FakeEvent())
        self.assertEqual(result.status, "no_results")
        self.assertEqual(result.reason, "unparsed_results")

    async def test_error_tool_exception_is_sanitized(self):
        tool = FakeTool(error="secret sk-123")
        backend = AnysearchSearchBackend(context=FakeContext(tool))
        result = await backend.search("原神", 2, event=FakeEvent())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "anysearch_search_error")
        self.assertNotIn("sk-123", result.reason)


class AnysearchCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_astrbot_group_tries_anysearch_when_official_disabled(self):
        tool = FakeTool(anysearch_json(2))
        service = SearchService(
            enabled=True,
            mode="astrbot",
            timeout_ms=2000,
            result_count=2,
            backends={
                "astrbot": FakeBackend("astrbot", False, "astrbot_search_disabled"),
                "anysearch": AnysearchSearchBackend(
                    context=FakeContext(tool)
                ),
                "api": FakeBackend("api"),
                "web": FakeBackend("web"),
            },
        )

        result = await service.search_game_info("原神", event=FakeEvent())

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["effective_backend"], "anysearch")
        self.assertEqual(result["skipped"][0]["backend"], "astrbot")
        self.assertEqual(result["skipped"][0]["reason"], "astrbot_search_disabled")
        await service.close()

    async def test_anysearch_off_never_called(self):
        tool = FakeTool(anysearch_json())
        service = SearchService(
            enabled=False,
            mode="off",
            result_count=2,
            backends={
                "anysearch": AnysearchSearchBackend(
                    context=FakeContext(tool)
                ),
            },
        )
        result = await service.search_game_info("原神", event=FakeEvent())
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(tool.calls, [])
        await service.close()

    async def test_anysearch_timeout(self):
        tool = FakeTool(anysearch_json(), delay=0.3)
        service = SearchService(
            enabled=True,
            mode="astrbot",
            timeout_ms=80,
            per_backend_timeout_ms=30,
            result_count=2,
            backends={
                "astrbot": FakeBackend(
                    "astrbot", False, "astrbot_search_disabled"
                ),
                "anysearch": AnysearchSearchBackend(
                    context=FakeContext(tool)
                ),
            },
        )
        result = await service.search_game_info("原神", event=FakeEvent())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "timeout")
        await service.close()


class AnysearchParserTests(unittest.TestCase):
    def test_jsonrpc_content_text(self):
        payload = {
            "content": [{
                "type": "text",
                "text": anysearch_json(1),
            }]
        }
        results = _anysearch_structured_results(payload)
        self.assertEqual(results[0]["title"], "结果0")

    def test_invalid_text_is_empty_not_fake(self):
        with self.assertRaises(ValueError):
            _anysearch_structured_results("错误：denied")
        # 错误文本由 backend 单独识别为失败；解析器本身不伪造结果。
