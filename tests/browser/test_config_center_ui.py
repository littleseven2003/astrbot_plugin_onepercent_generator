# -*- coding: utf-8 -*-
"""配置中心真实浏览器回归（隔离 AstrBotPluginView 模拟桥）。

用例通过本地 HTTP 服务加载真实的 index.html/app.js/style.css，桥只提供
确定性的 state/save 响应；不访问 AstrBot、真实搜索或付费模型。

需要 Playwright for Python；未安装时跳过。运行方式：
    PYTHONPATH=<playwright 包目录> python3 -m unittest tests.browser.test_config_center_ui
"""

import copy
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

PROJECT_DIR = Path(__file__).resolve().parents[2]
VIEW_DIR = PROJECT_DIR / "views" / "config-center"


def _initial_state():
    return {
        "model": {
            "source": "manual",
            "providers": [{"id": "local/mock-chat", "label": "Mock Chat"}],
            "astrbot_provider_id": "",
            "presets": [
                {
                    "id": "custom",
                    "label": "自定义",
                    "base_url": "",
                    "api_format": "chat_completions",
                }
            ],
            "manual": {
                "base_url": "https://api.example.com/v1",
                "model": "saved-model",
                "api_format": "chat_completions",
                "send_temperature": True,
                "provider_preset": "custom",
                "key_configured": False,
                "key_masked": "",
                "key_usable": True,
            },
        },
        "search": {
            "enabled": True,
            "mode": "astrbot",
            "mode_order": ["astrbot", "api", "web"],
            "mode_enabled": ["astrbot", "api", "web"],
            "result_count": 3,
            "timeout_ms": 8000,
            "per_backend_timeout_ms": 0,
            "summary_input_max_chars": 6000,
            "summarize_enabled": False,
            "summarize_max_chars": 150,
            "material_max_chars": 3000,
            "material_max_items": 6,
            "material_snippet_chars": 400,
            "api_provider": "tavily",
            "api_base_url": "https://api.tavily.com",
            "api_key_configured": False,
            "api_key_masked": "",
            "api_key_usable": False,
            "modes": ["astrbot", "api", "web"],
            "last_result": {},
        },
        "rate": {
            "window_minutes": 10,
            "max_requests_per_window": 3,
            "daily_max": 20,
            "reset_timezone": "",
            "timezone_label": "CST",
            "admin_exception": True,
        },
        "session": {
            "allow_private": True,
            "allow_group": True,
            "sessions": [
                {
                    "umo": "qq:FriendMessage:u1",
                    "connector": "qq",
                    "type": "private",
                    "enabled": True,
                }
            ],
            "admin_users": [
                {"connector": "qq", "uid": "admin1", "legacy": False}
            ],
            "legacy_admin_present": False,
            "legacy_admin_pending": [],
            "legacy_umo_pending": [],
            "legacy_restrictions_pending": False,
            "migration_required": False,
            "config_error": "",
            "identity_links": [],
            "known_identities": [],
            "connectors": ["qq"],
        },
        "advanced": {"preset_games": ["原神"], "show_generation_stats": True},
        "stats_started_at": 1.0,
    }


def _stats_payload(chats=None, totals=None, buckets=None):
    payload = {
        "totals": {
            "requests": 0,
            "searches": 0,
            "tokens": 0,
            "token_status": "unavailable",
            "tokens_by_kind": {"generate": 0, "summarize": 0, "test": 0},
            "failures": 0,
            "fallback_attempts": 0,
            "fallback_incomplete": False,
            "fallback_unknown_events": 0,
        },
        "range_start": 0,
        "range_end": 60,
        "retained_events": 0,
        "matched_events": 0,
        "buckets": buckets or [],
        "chats": chats or [],
    }
    payload["totals"].update(totals or {})
    payload["retained_events"] = payload["totals"].get(
        "retained_events", payload["retained_events"]
    )
    payload["matched_events"] = payload["totals"].get(
        "matched_events", payload["retained_events"]
    )
    return payload


class MockBridgeServer:
    """真实前端 + 最小持久化 state 的测试服务器。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = _initial_state()
        self.stats_chats = []
        self.stats_totals = {}
        self.stats_buckets = []
        self.stats_fail = False
        self.requests = []
        self.fail_next = {}
        self.holds = {}
        self.link_seq = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return None

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def _json(self, value, status=200):
                raw = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/sandbox.html":
                    raw = (
                        '<!doctype html><meta charset="utf-8"><title>sandbox</title>'
                        '<iframe id="frame" src="/index.html" sandbox="allow-scripts allow-forms allow-downloads" '
                        'style="width:1240px;height:880px;border:0"></iframe>'
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path in ("/", "/index.html"):
                    path = "/index.html"
                target = (VIEW_DIR / path.lstrip("/")).resolve()
                if not str(target).startswith(str(VIEW_DIR.resolve())) or not target.is_file():
                    self.send_error(404)
                    return
                body = target.read_bytes()
                self.send_response(200)
                if target.suffix == ".html":
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                elif target.suffix == ".js":
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                elif target.suffix == ".css":
                    self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/__api":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                method = payload.get("method")
                path = payload.get("path")
                data = payload.get("data") or {}
                section = data.get("section") if path == "config-center/save" else None
                with owner.lock:
                    owner.requests.append({"method": method, "path": path, "data": data})
                # 测试用可控闸门：请求已到达但响应/落盘被阻塞，用于制造过期响应。
                hold = owner._take_hold(path, section)
                stale_state = None
                if hold is not None:
                    if path == "config-center/state":
                        # 读取响应在请求到达时就已生成；阻塞期间发生的写入不能进入这份快照。
                        with owner.lock:
                            stale_state = copy.deepcopy(owner.state)
                    hold["entered"].set()
                    if not hold["release"].wait(15):
                        self._json({"status": "error", "message": "mock hold timeout"}, 504)
                        return
                with owner.lock:
                    if path == "config-center/state":
                        if hold and hold.get("fail"):
                            self._json({"status": "error", "message": hold["fail"]})
                            return
                        self._json(stale_state if stale_state is not None else copy.deepcopy(owner.state))
                        return
                    if path == "config-center/stats":
                        if owner.stats_fail:
                            self._json(
                                {"status": "error", "message": "统计读取失败（模拟）"},
                                500,
                            )
                            return
                        self._json(_stats_payload(
                            owner.stats_chats,
                            owner.stats_totals.get(data.get("chats", "")),
                            owner.stats_buckets,
                        ))
                        return
                    if path == "config-center/save":
                        reason = hold.get("fail") if hold else owner.fail_next.pop(section, None)
                        if reason:
                            self._json({"status": "error", "message": reason})
                            return
                        base_revision = data.get("base_revision")
                        if base_revision is not None:
                            try:
                                base_revision = int(base_revision)
                            except (TypeError, ValueError):
                                self._json({"status": "error", "message": "base_revision 格式错误"}, 400)
                                return
                            if base_revision != owner.revision:
                                self._json({
                                    "saved": False,
                                    "conflict": True,
                                    "revision": owner.revision,
                                    "message": "配置已被其他页面更新，已阻止本次保存以避免覆盖，请刷新后再保存（当前草稿已保留）。",
                                })
                                return
                        owner._apply_save(section, data.get("data") or {})
                        owner.revision += 1
                        owner.state["revision"] = owner.revision
                        if owner.save_state_none:
                            self._json({
                                "saved": True,
                                "section": section,
                                "state": None,
                                "revision": owner.revision,
                            })
                            return
                        self._json({
                            "saved": True,
                            "section": section,
                            "state": copy.deepcopy(owner.state),
                            "revision": owner.revision,
                        })
                        return
                    if path == "config-center/model/test":
                        self._json({"success": True, "message": "mock ok"})
                        return
                    if path == "config-center/model/models":
                        self._json({"models": ["saved-model", "other-model"]})
                        return
                    if path == "config-center/search/test":
                        self._json({
                            "success": True,
                            "message": "mock search ok",
                            "mode": "网页搜索",
                            "backend": "mock",
                            "result_count": 1,
                            "attempted": [{
                                "backend": "api",
                                "status": "success",
                                "reason": "",
                                "result_count": 1,
                            }],
                            "skipped": [{
                                "backend": "astrbot",
                                "status": "unavailable",
                                "reason": "anysearch_plugin_not_installed",
                            }],
                        })
                        return
                    self._json({"status": "error", "message": "unknown api"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def reset(self):
        with self.lock:
            self.state = _initial_state()
            self.state["revision"] = 0
            self.revision = 0
            self.save_state_none = False
            self.stats_chats = []
            self.stats_totals = {}
            self.stats_buckets = []
            self.stats_fail = False
            self.requests = []
            self.fail_next = {}
            self.holds = {}
            self.link_seq = 0

    def hold_next(self, path, section=None, fail=None):
        """阻塞下一条匹配请求，直到测试释放；用于稳定制造过期响应。"""
        gate = {
            "entered": threading.Event(),
            "release": threading.Event(),
            "fail": fail,
        }
        with self.lock:
            self.holds[(path, section)] = gate
        return gate

    def _take_hold(self, path, section):
        with self.lock:
            if (path, section) in self.holds:
                return self.holds.pop((path, section))
            if section is not None and (path, None) in self.holds:
                return self.holds.pop((path, None))
        return None

    def _apply_save(self, section, data):
        state = self.state
        if section == "admins":
            admins = []
            for item in data.get("admin_users", []):
                admins.append({
                    "connector": item.get("connector", ""),
                    "uid": item.get("uid", ""),
                    "legacy": bool(item.get("legacy", False)),
                })
            state["session"]["admin_users"] = admins
            pending = state["session"].get("legacy_admin_pending") or []
            if data.get("clear_legacy_admin_pending"):
                pending = []
            state["session"]["legacy_admin_pending"] = [
                item for item in pending if item.get("uid") not in {a["uid"] for a in admins}
            ]
        elif section == "identity":
            links = copy.deepcopy(state["session"].get("identity_links") or [])
            action = data.get("action")
            if action == "link":
                connector = data.get("connector") or ""
                uids = sorted({str(item).strip() for item in (data.get("uids") or []) if str(item).strip()})
                related = [
                    link for link in links
                    if link.get("connector") == connector
                    and set(link.get("uids") or []) & set(uids)
                ]
                merged = set(uids)
                for link in related:
                    merged.update(link.get("uids") or [])
                links = [link for link in links if link not in related]
                self.link_seq += 1
                links.append({
                    "id": "link-mock-%d" % self.link_seq,
                    "connector": connector,
                    "uids": sorted(merged),
                })
                for uid in uids:
                    known = state["session"].setdefault("known_identities", [])
                    token = connector + ":" + uid
                    if token not in known:
                        known.append(token)
            elif action == "unlink":
                link_id = data.get("id")
                links = [link for link in links if link.get("id") != link_id]
            state["session"]["identity_links"] = links
        elif section == "sessions":
            # 与真实后端一致：只有显式提交 sessions 键才改会话列表；
            # 普通开关保存只更新放行策略，保留 null/无效列表及其诊断。
            state["session"].update({
                "allow_private": bool(data.get("allow_private")),
                "allow_group": bool(data.get("allow_group")),
            })
            if "sessions" in data:
                sessions = copy.deepcopy(data.get("sessions") or [])
                state["session"].update({
                    "sessions": sessions,
                    "config_error": "",
                    "sessions_raw_type": "list",
                    "invalid_sessions": [],
                })
                if sessions:
                    state["session"]["migration_required"] = False
                    state["session"]["legacy_restrictions_pending"] = False
        elif section == "model":
            manual = data.get("manual") or {}
            saved_manual = state["model"]["manual"]
            base_changed = manual.get("base_url", "") != saved_manual.get(
                "base_url", ""
            )
            key = str(manual.get("api_key") or "")
            state["model"]["source"] = data.get("source", "manual")
            state["model"]["astrbot_provider_id"] = data.get(
                "astrbot_provider_id", ""
            )
            saved_manual.update({
                "base_url": manual.get("base_url", ""),
                "model": manual.get("model", ""),
                "api_format": manual.get("api_format", "chat_completions"),
                "send_temperature": bool(manual.get("send_temperature", True)),
                "provider_preset": manual.get("provider_preset", "custom"),
            })
            # 与真实后端一致：填入新 Key 覆盖；留空保留旧 Key（地址变了则
            # 保留但标记不可用，需要重新输入）。
            if key:
                saved_manual["key_configured"] = True
                saved_manual["key_usable"] = True
                saved_manual["key_masked"] = "••••" + key[-4:]
            elif base_changed:
                saved_manual["key_usable"] = False
        elif section == "search":
            key = data.get("api_key")
            state["search"].update({
                "enabled": bool(data.get("enabled")),
                "mode": data.get("mode", "auto"),
                "mode_order": list(data.get("mode_order") or []),
                "mode_enabled": list(data.get("mode_enabled") or []),
                "result_count": data.get("result_count", 3),
                "timeout_ms": data.get("timeout_ms", 8000),
                "per_backend_timeout_ms": data.get("per_backend_timeout_ms", 0),
                "summary_input_max_chars": data.get("summary_input_max_chars", 6000),
                "summarize_enabled": bool(data.get("summarize_enabled")),
                "summarize_max_chars": data.get("summarize_max_chars", 150),
                "api_provider": data.get("api_provider", "tavily"),
                "api_base_url": data.get("api_base_url", ""),
            })
            if data.get("clear_api_key"):
                state["search"]["api_key_configured"] = False
            elif key:
                state["search"]["api_key_configured"] = True
        elif section == "rate":
            state["rate"].update({
                "window_minutes": data.get("window_minutes", 0),
                "max_requests_per_window": data.get("max_requests_per_window", 1),
                "daily_max": data.get("daily_max", 0),
                "reset_timezone": data.get("reset_timezone", ""),
            })
        elif section == "advanced":
            state["advanced"].update({
                "preset_games": copy.deepcopy(data.get("preset_games") or []),
                "show_generation_stats": bool(data.get("show_generation_stats")),
            })

    def saves(self, section=None):
        with self.lock:
            return [
                item for item in self.requests
                if item["path"] == "config-center/save"
                and (section is None or item["data"].get("section") == section)
            ]


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "需要 Playwright for Python")
class ConfigCenterBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockBridgeServer()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.close()

    def setUp(self):
        self.server.reset()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.context.add_init_script(
            """
            window.AstrBotPluginView = {
              ready: async () => {},
              getContext: () => ({ isDark: false }),
              onContext: () => {},
              apiGet: async (path, params) => {
                const response = await fetch('/__api', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ method: 'GET', path, data: params || {} }),
                });
                return await response.json();
              },
              apiPost: async (path, data) => {
                const response = await fetch('/__api', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ method: 'POST', path, data: data || {} }),
                });
                return await response.json();
              },
            };
            """
        )
        self.page = self.context.new_page()
        self.console_errors = []
        self.page.on(
            "console",
            lambda message: self.console_errors.append(message.text)
            if message.type == "error" else None,
        )
        self.page.on("pageerror", lambda error: self.console_errors.append(str(error)))
        self.page.goto(self.server.url)
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )

    def tearDown(self):
        self.context.close()

    def go(self, page_name):
        self.page.click('nav button[data-page="%s"]' % page_name)

    def badge(self):
        return self.page.locator("#dirtyBadge").inner_text()

    def clear_message(self, element_id):
        self.page.evaluate(
            "(id) => { const el = document.getElementById(id); if (el) el.textContent = ''; }",
            element_id,
        )

    def wait_saves(self, section, count, timeout=5):
        """等待服务端收到至少 count 次该分区保存，避免误匹配上一条成功消息。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.server.saves(section)) >= count:
                return
            time.sleep(0.02)
        self.fail(
            "等待 %s 分区第 %d 次保存超时（已收到 %d 次）"
            % (section, count, len(self.server.saves(section)))
        )

    def trigger_save(self, section):
        """自动保存没有按钮：标记该分区为已修改并强制提交一次（跳过同内容去重）。"""
        self.page.evaluate(
            """(sec) => {
              const id = {model: 'modelInput', search: 'searchResults',
                          rate: 'windowNumber', advanced: 'presetGames'}[sec];
              const el = document.getElementById(id);
              if (el) el.dispatchEvent(new Event('input', { bubbles: true }));
              window.__dshConfig.resetSignature(sec);
              return window.__dshConfig.saveSectionNow(sec);
            }""",
            section,
        )

    def refresh_all(self):
        """刷新全部：若有未保存草稿，先确认丢弃再等待读取。"""
        self.page.click("#refreshAll")
        self.page.wait_for_timeout(50)
        visible = self.page.locator("#confirmLayer").evaluate(
            "el => !el.classList.contains('hidden')"
        )
        if visible:
            self.page.click("#confirmOk")

    def pause_autosave(self, frame=None):
        """部分旧用例专注「草稿/刷新/冲突」语义：暂停自动保存以保持草稿可观察。"""
        (frame or self.page).evaluate(
            "() => window.__dshConfig.setAutosavePaused(true)"
        )

    def mode_state(self):
        """总开关关闭或没有勾选组时返回「关闭」，否则返回首个已勾选组。"""
        return self.page.evaluate(
            """() => {
              const master = document.getElementById('searchEnabled');
              if (master && !master.checked) return '关闭';
              const rows = [...document.querySelectorAll('#modeList .mode-row')];
              const on = rows.find(r => r.querySelector('.status-badge.ok'));
              return on
                ? on.querySelector('.candidate-main strong').textContent.trim()
                : '关闭';
            }"""
        )

    def test_unavailable_tokens_are_not_shown_as_zero(self):
        self.go("status")
        self.page.wait_for_selector("#statCards:has-text('Tokens')")
        cards = self.page.locator("#statCards").inner_text()
        self.assertIn("暂无数据", cards)
        self.assertNotIn("Tokens\n0", cards)
        self.assertIn("未按 0 补计", self.page.locator("#tokenNote").inner_text())
        self.assertIn(
            "当前范围内没有实际记录",
            self.page.locator("#chartEmpty").inner_text(),
        )
        self.assertIn(
            "当前范围没有实际记录，未按 0 补计",
            self.page.locator("#chartTable").text_content(),
        )
        self.assertIn(
            "当前范围记录 0 条；插件共保留 0 条",
            self.page.locator("#rangeInfo").inner_text(),
        )

    def test_stats_report_search_modes_and_real_retained_range(self):
        self.server.stats_totals = {
            "": {
                "requests": 4,
                "searches": 3,
                "matched_events": 4,
                "retained_events": 6,
                "searches_by_mode": {"auto": 2, "unknown": 1},
            },
        }
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("status")
        self.page.wait_for_selector("#rangeInfo:has-text('插件共保留 6 条')")
        cards = self.page.locator("#statCards").inner_text()
        self.assertIn("请求数\n4", cards)
        self.assertIn("实际搜索\n3", cards)
        note = self.page.locator("#tokenNote").inner_text()
        self.assertIn("搜索模式：旧版自动 2 · 未记录模式（旧记录） 1", note)
        self.assertNotIn("未按 0 补计", note)
        self.assertIn(
            "当前范围记录 4 条；插件共保留 6 条",
            self.page.locator("#rangeInfo").inner_text(),
        )

    def test_stats_failure_clears_previous_range_instead_of_showing_stale_data(self):
        self.go("status")
        self.page.wait_for_selector("#statCards:has-text('暂无数据')")
        self.server.stats_fail = True
        self.page.click('[data-period="1d"]')
        self.page.wait_for_selector("#rangeInfo:has-text('统计加载失败')")
        self.assertIn("统计读取失败（模拟）", self.page.locator("#rangeInfo").inner_text())
        cards = self.page.locator("#statCards").inner_text()
        self.assertEqual(cards.count("加载失败"), 3)
        self.assertNotIn("暂无数据", cards)
        self.assertIn(
            "统计加载失败，未显示旧数据",
            self.page.locator("#chartEmpty").inner_text(),
        )
        self.assertTrue(self.page.locator("#trendChart").is_hidden())
        self.assertIn(
            "统计加载失败：未显示上一次范围的数据。",
            self.page.locator("#chartTable").text_content(),
        )
        self.assertIn("统计加载失败：", self.page.locator("#tokenNote").inner_text())

        self.server.stats_fail = False
        self.page.click('[data-period="30d"]')
        self.page.wait_for_selector("#statCards:has-text('暂无数据')")
        self.assertIn(
            "当前范围内没有实际记录",
            self.page.locator("#chartEmpty").inner_text(),
        )

    def test_zero_trend_with_records_is_not_reported_as_no_data(self):
        self.server.stats_totals = {
            "": {
                "requests": 0,
                "searches": 0,
                "matched_events": 3,
                "retained_events": 3,
                "tokens": 0,
                "token_status": "unavailable",
            },
        }
        self.server.stats_buckets = [
            {"label": "10:00", "requests": 0, "searches": 0, "tokens": 0},
            {"label": "11:00", "requests": 0, "searches": 0, "tokens": 0},
            {"label": "12:00", "requests": 0, "searches": 0, "tokens": 0},
        ]
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("status")
        self.page.wait_for_selector("#rangeInfo:has-text('当前范围记录 3 条')")
        self.assertNotIn("暂无数据", self.page.locator("#statCards").inner_text())
        self.assertIn(
            "当前范围有记录，但所选指标均为 0",
            self.page.locator("#chartEmpty").inner_text(),
        )
        self.assertTrue(self.page.locator("#trendChart").is_hidden())
        table = self.page.locator("#chartTable").text_content()
        self.assertIn("10:00", table)
        self.assertIn("12:00", table)
        self.assertNotIn("暂无数据", table)

    def test_zero_hit_chat_search_does_not_change_all_chats_selection(self):
        self.server.stats_chats = [
            {"key": "qq:FriendMessage:a", "label": "聊天 A", "umo": "qq:FriendMessage:a", "connector": "qq"},
            {"key": "qq:GroupMessage:b", "label": "聊天 B", "umo": "qq:GroupMessage:b_group", "connector": "qq"},
        ]
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("status")
        self.page.wait_for_selector("#statCards:has-text('请求数')")
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")

        def latest_stats_count():
            return len([
                r for r in self.server.requests if r["path"] == "config-center/stats"
            ])

        def apply_and_check():
            before = latest_stats_count()
            self.page.click("#chatApply")
            deadline = time.time() + 5
            while time.time() < deadline:
                stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
                if len(stats) > before:
                    break
                time.sleep(0.02)
            self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")
            self.assertNotIn("chats", stats[-1]["data"])

        # 搜索无命中时全选/全不选没有作用对象，不能把“全部聊天（含以后新增）”
        # 悄悄改成显式空集合或当前聊天集合。
        self.page.click("#chatToggle")
        self.page.fill("#chatSearch", "不存在的聊天")
        self.assertIn("没有匹配的聊天记录", self.page.locator("#chatList").inner_text())
        self.page.click("#chatAll")
        apply_and_check()

        self.page.click("#chatToggle")
        self.page.fill("#chatSearch", "不存在的聊天")
        self.page.click("#chatNone")
        apply_and_check()

    def test_search_key_must_be_reentered_when_base_url_changes(self):
        search = self.server.state["search"]
        search.update({
            "api_key_configured": True,
            "api_key_masked": "sk-old...",
            "api_key_usable": True,
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        self.assertIn(
            "已保存搜索 Key sk-old...",
            self.page.locator("#searchMessage").inner_text(),
        )
        before = len(self.server.saves("search"))
        self.page.fill("#searchBase", "https://api.tavily.com/search")
        self.page.press("#searchBase", "Tab")
        self.assertIn(
            "旧 Key 不会复用",
            self.page.locator("#searchMessage").inner_text(),
        )
        self.trigger_save("search")
        self.page.wait_for_selector(
            "#searchSaveMessage:has-text('请重新输入 API Key')"
        )
        self.assertEqual(len(self.server.saves("search")), before)
        self.assertEqual(search["api_base_url"], "https://api.tavily.com")
        self.assertTrue(search["api_key_configured"])

        self.page.fill("#searchKey", "sk-new")
        self.trigger_save("search")
        self.page.wait_for_selector("#searchSaveMessage:has-text('搜索配置已保存')")
        self.wait_saves("search", before + 1)
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["api_key"], "sk-new")
        self.assertEqual(payload["api_base_url"], "https://api.tavily.com/search")
        self.assertEqual(search["api_base_url"], "https://api.tavily.com/search")
        self.assertTrue(search["api_key_configured"])

    def test_provider_selection_feedback_is_immediate(self):
        self.go("model")
        self.page.click("#sourceAstrbot")
        self.assertEqual(
            self.page.locator("#providerSelectionState").inner_text(), "未选择模型"
        )
        self.page.select_option("#providerSelect", "local/mock-chat")
        self.page.wait_for_selector(
            "#providerSelectionState:has-text('已保存：Mock Chat')"
        )
        self.trigger_save("model")
        self.page.wait_for_selector("#modelMessage:has-text('已保存')")
        self.assertEqual(
            self.page.locator("#providerSelectionState").inner_text(),
            "已保存：Mock Chat",
        )
        self.assertEqual(
            self.server.state["model"]["astrbot_provider_id"], "local/mock-chat"
        )

    def test_saved_group_order_and_effective_group_round_trip(self):
        # 服务端保存的顺序与勾选是事实来源：页面按列表显示，首行已勾选组生效。
        self.server.state["search"].update({
            "mode": "web",
            "mode_order": ["web", "astrbot", "api"],
            "mode_enabled": ["web"],
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        self.assertIn(
            "网页搜索", self.mode_state()
        )
        rows = self.page.locator("[data-mode-row]").evaluate_all(
            "els => els.map(el => el.dataset.modeRow)"
        )
        self.assertEqual(rows, ["web", "astrbot", "api"])
        self.assertTrue(self.page.is_checked('[data-mode-check="web"]'))
        self.trigger_save("search")
        self.page.wait_for_selector("#searchSaveMessage:has-text('搜索配置已保存')")
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["mode"], "web")
        self.assertEqual(payload["mode_order"], ["web", "astrbot", "api"])
        self.assertEqual(payload["mode_enabled"], ["web"])

        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        self.assertIn(
            "网页搜索", self.mode_state()
        )

    def test_search_page_only_has_three_groups_and_no_fallback_switch(self):
        self.go("search")
        rows = self.page.locator("[data-mode-row]").evaluate_all(
            "els => els.map(el => el.dataset.modeRow)"
        )
        self.assertEqual(rows, ["astrbot", "api", "web"])
        labels = self.page.locator("[data-mode-row] .candidate-main strong").evaluate_all(
            "els => els.map(el => el.textContent.trim())"
        )
        self.assertEqual(labels, ["Astrbot与搜索插件", "搜索API", "网页搜索"])
        self.assertEqual(self.page.locator("#fallbackEnabled").count(), 0)
        self.assertIn(
            "Astrbot与搜索插件", self.mode_state()
        )

    def test_master_switch_off_shows_no_effective_concept(self):
        # 已移除「当前生效」概念：页面不再出现该文案，总开关关闭后行徽标全部未启用。
        self.go("search")
        self.assertEqual(self.mode_state(), "Astrbot与搜索插件")
        self.assertNotIn("当前生效", self.page.locator("#page-search").inner_text())
        self.page.uncheck("#searchEnabled")
        self.wait_saves("search", 1)
        self.assertEqual(self.mode_state(), "关闭")
        self.assertNotIn("当前生效", self.page.locator("#page-search").inner_text())
        self.page.check("#searchEnabled")
        self.wait_saves("search", 2)
        self.assertEqual(self.mode_state(), "Astrbot与搜索插件")

    def test_reorder_and_toggle_survive_save_and_reload(self):
        self.server.state["search"].update({
            "mode": "api",
            "mode_order": ["web", "api", "astrbot"],
            "mode_enabled": ["api"],
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        self.assertIn(
            "搜索API", self.mode_state()
        )
        rows = self.page.locator("[data-mode-row]").evaluate_all(
            "els => els.map(el => el.dataset.modeRow)"
        )
        self.assertEqual(rows, ["web", "api", "astrbot"])
        self.trigger_save("search")
        self.page.wait_for_selector("#searchSaveMessage:has-text('搜索配置已保存')")
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["mode"], "api")
        self.assertEqual(payload["mode_order"], ["web", "api", "astrbot"])
        self.assertEqual(payload["mode_enabled"], ["api"])
        self.assertNotIn("fallback_enabled", payload)

        # 勾选网页组后它排在首位，成为生效组；保存后顺序与勾选都保留。
        self.page.check('[data-mode-check="web"]')
        self.trigger_save("search")
        self.wait_saves("search", 2)
        self.page.wait_for_selector("#searchSaveMessage:has-text('搜索配置已保存')")
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["mode"], "web")
        self.assertEqual(payload["mode_enabled"], ["api", "web"])

        # 下移网页组：顺序变为 API → 网页，生效组随之变为搜索API。
        self.page.click('[data-mode-move="web"][data-direction="1"]')
        self.page.wait_for_function(
            "document.querySelectorAll('[data-mode-row]')[0].dataset.modeRow === 'api'"
        )
        self.trigger_save("search")
        self.wait_saves("search", 3)
        self.page.wait_for_selector("#searchSaveMessage:has-text('搜索配置已保存')")
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["mode_order"], ["api", "web", "astrbot"])
        self.assertEqual(payload["mode"], "api")
        self.assertEqual(payload["mode_enabled"], ["api", "web"])

        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        rows = self.page.locator("[data-mode-row]").evaluate_all(
            "els => els.map(el => el.dataset.modeRow)"
        )
        self.assertEqual(rows, ["api", "web", "astrbot"])
        self.assertIn(
            "搜索API", self.mode_state()
        )
        self.assertTrue(self.page.is_checked('[data-mode-check="web"]'))
        self.assertTrue(self.page.is_checked('[data-mode-check="api"]'))

    def test_chat_filter_draft_does_not_apply_until_confirmed(self):
        self.server.stats_chats = [
            {"key": "qq:FriendMessage:a", "label": "聊天 A", "umo": "qq:FriendMessage:a", "connector": "qq"},
            {"key": "qq:GroupMessage:b", "label": "聊天 B", "umo": "qq:GroupMessage:b_group", "connector": "qq"},
        ]
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")
        self.page.click("#chatToggle")
        self.page.uncheck('#chatList input[value="qq:FriendMessage:a"]')
        # 只改草稿、不应用：切换范围仍按“全部聊天”统计，标签也不变。
        self.page.click('[data-period="1d"]')
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats:
                break
            time.sleep(0.02)
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")
        self.assertNotIn("chats", stats[-1]["data"])

        # 重新打开时草稿回到已应用条件，不会偷偷保留未确认的勾选。
        self.page.click("#chatToggle")
        self.assertTrue(self.page.is_checked('#chatList input[value="qq:FriendMessage:a"]'))
        self.page.uncheck('#chatList input[value="qq:FriendMessage:a"]')
        self.page.click("#chatApply")
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "1 个聊天")
        self.page.click('[data-period="30d"]')
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats and stats[-1]["data"].get("chats") == "qq:GroupMessage:b":
                break
            time.sleep(0.02)
        self.assertEqual(stats[-1]["data"].get("chats"), "qq:GroupMessage:b")

        # Esc 丢弃草稿，不改变已应用筛选。
        self.page.click("#chatToggle")
        self.page.uncheck('#chatList input[value="qq:GroupMessage:b"]')
        self.page.keyboard.press("Escape")
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "1 个聊天")

        self.page.click("#chatToggle")
        self.page.click("#chatNone")
        self.page.click("#chatApply")
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "未选择聊天")
        self.page.click('[data-period="7d"]')
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats and stats[-1]["data"].get("chats") == "__none__":
                break
            time.sleep(0.02)
        self.assertEqual(stats[-1]["data"].get("chats"), "__none__")

        self.page.click("#chatToggle")
        self.page.fill("#chatSearch", "聊天 B")
        self.assertEqual(self.page.locator("#chatList input").count(), 1)
        self.assertIn(
            "搜索命中 1 个聊天；全选/全不选只作用于当前筛选结果",
            self.page.locator("#chatBulkScope").inner_text(),
        )
        self.assertEqual(self.page.locator("#chatAll").inner_text(), "全选结果")
        self.page.click("#chatAll")
        self.page.click("#chatApply")
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "1 个聊天")
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats and stats[-1]["data"].get("chats") == "qq:GroupMessage:b":
                break
            time.sleep(0.02)
        self.assertEqual(stats[-1]["data"].get("chats"), "qq:GroupMessage:b")

    def test_empty_chat_filter_distinguishes_all_and_explicit_none(self):
        self.server.stats_chats = []
        self.server.stats_totals = {
            "": {
                "requests": 3,
                "searches": 2,
                "matched_events": 3,
                "retained_events": 3,
            },
            "__none__": {
                "requests": 0,
                "searches": 0,
                "matched_events": 0,
                "retained_events": 3,
            },
        }
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")

        # 没有聊天时，“全选”草稿也是空；应用后仍必须表示全部记录。
        self.page.click("#chatToggle")
        self.page.click("#chatAll")
        self.page.click("#chatApply")
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats and "chats" not in stats[-1]["data"]:
                break
            time.sleep(0.02)
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")
        self.assertEqual(
            self.page.locator("#statCards .stat strong").nth(0).inner_text(), "3"
        )

        # 随后新增聊天：全部聊天继续包含新记录，新聊天也出现在草稿里。
        self.server.stats_chats = [
            {"key": "qq:FriendMessage:new", "label": "新聊天", "umo": "qq:FriendMessage:new", "connector": "qq"}
        ]
        self.server.stats_totals[""] = {
            "requests": 4,
            "searches": 3,
            "matched_events": 4,
            "retained_events": 4,
        }
        before = len([
            r for r in self.server.requests if r["path"] == "config-center/stats"
        ])
        self.page.click('[data-period="30d"]')
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if (
                len(stats) > before
                and stats[-1]["data"].get("period") == "30d"
                and "chats" not in stats[-1]["data"]
            ):
                break
            time.sleep(0.02)
        self.page.wait_for_function(
            "document.querySelector('#statCards .stat strong').textContent === '4'",
            timeout=5000,
        )
        self.assertEqual(
            self.page.locator("#statCards .stat strong").nth(0).inner_text(), "4"
        )
        self.page.click("#chatToggle")
        self.assertTrue(
            self.page.is_checked('#chatList input[value="qq:FriendMessage:new"]')
        )

        # 明确“未选择聊天”才发送空筛选。
        self.page.click("#chatNone")
        self.page.click("#chatApply")
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats and stats[-1]["data"].get("chats") == "__none__":
                break
            time.sleep(0.02)
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "未选择聊天")
        self.page.wait_for_function(
            "document.querySelector('#statCards .stat strong').textContent === '暂无数据'",
            timeout=5000,
        )
        self.assertEqual(
            self.page.locator("#statCards .stat strong").nth(0).inner_text(),
            "暂无数据",
        )

        # 再全选一次仍恢复为“全部聊天”，而不是空筛选。
        self.page.click("#chatToggle")
        self.page.click("#chatAll")
        self.page.click("#chatApply")
        deadline = time.time() + 5
        while time.time() < deadline:
            stats = [r for r in self.server.requests if r["path"] == "config-center/stats"]
            if stats and "chats" not in stats[-1]["data"]:
                break
            time.sleep(0.02)
        self.assertEqual(self.page.locator("#chatToggleLabel").inner_text(), "全部聊天")
        self.page.wait_for_function(
            "document.querySelector('#statCards .stat strong').textContent === '4'",
            timeout=5000,
        )
        self.assertEqual(
            self.page.locator("#statCards .stat strong").nth(0).inner_text(), "4"
        )

    def test_clear_legacy_admin_pending_without_granting_admin(self):
        self.server.state["session"]["legacy_admin_pending"] = [
            {"uid": "old-admin", "connector": "", "reason": "no_connector"}
        ]
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector("#clearAdminPending")
        self.assertEqual(
            len(self.server.state["session"]["admin_users"]), 1
        )
        self.page.click("#clearAdminPending")
        self.wait_saves("admins", 1)
        self.page.wait_for_selector("#adminPending.hidden", state="attached")
        payload = self.server.saves("admins")[-1]["data"]["data"]
        self.assertTrue(payload.get("clear_legacy_admin_pending"))
        self.assertEqual(
            [item["uid"] for item in payload["admin_users"]], ["admin1"]
        )
        self.assertFalse(
            self.page.is_visible("#adminPending")
        )
        self.assertEqual(
            self.server.state["session"]["legacy_admin_pending"], []
        )
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.assertFalse(self.page.is_visible("#clearAdminPending"))

    def test_admin_removal_is_immediate_and_survives_reload(self):
        self.go("session")
        self.page.click("[data-admin-remove='0']")
        self.page.wait_for_selector("#adminMessage:has-text('管理员已保存')")
        self.assertNotIn("管理员", self.badge())
        self.assertEqual(
            self.page.locator("#adminList .list-row").count(), 0
        )
        self.assertEqual(self.server.state["session"]["admin_users"], [])
        self.assertEqual(
            self.server.saves("admins")[-1]["data"]["data"]["admin_users"], []
        )
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.assertEqual(self.page.locator("#adminList .list-row").count(), 0)
        self.page.wait_for_function(
            "document.getElementById('dirtyBadge').classList.contains('hidden')"
        )

    def test_admin_addition_is_immediate_and_persists(self):
        self.go("session")
        self.page.fill("#adminUid", "admin2")
        self.page.click("#addAdmin")
        self.page.wait_for_selector("#adminMessage:has-text('管理员已保存')")
        self.assertEqual(self.page.locator("#adminList .list-row").count(), 2)
        self.assertNotIn("管理员", self.badge())
        self.assertEqual(
            [item["uid"] for item in self.server.state["session"]["admin_users"]],
            ["admin1", "admin2"],
        )
        self.assertEqual(
            self.server.saves("admins")[-1]["data"]["data"]["admin_users"],
            [
                {"connector": "qq", "uid": "admin1", "legacy": False},
                {"connector": "qq", "uid": "admin2", "legacy": False},
            ],
        )

        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.assertEqual(self.page.locator("#adminList .list-row").count(), 2)

    def test_refresh_providers_prompts_before_discarding_drafts(self):
        self.pause_autosave()
        self.go("model")
        self.page.click("#sourceAstrbot")
        self.assertTrue(self.page.is_visible("#astrbotPanel"))
        self.assertIn("模型", self.badge())

        self.page.click("#refreshProviders")
        self.page.wait_for_selector("#confirmLayer:not(.hidden)")
        self.assertIn("丢弃这些草稿", self.page.inner_text("#confirmText"))
        self.page.click("#confirmCancel")
        expect(self.page.locator("#confirmLayer")).to_be_hidden()
        self.assertTrue(self.page.is_visible("#astrbotPanel"))
        self.assertIn("模型", self.badge())

    def test_discard_confirmation_is_in_page_in_sandboxed_iframe(self):
        native_dialogs = []

        def on_dialog(dialog):
            native_dialogs.append(dialog.type)
            dialog.dismiss()

        self.page.on("dialog", on_dialog)
        self.page.goto(self.server.url + "sandbox.html")
        frame = self.page.frame_locator("#frame")
        expect(frame.locator("#sumModel")).not_to_have_text("加载中…", timeout=5000)
        frame.locator("body").evaluate(
            "() => window.__dshConfig.setAutosavePaused(true)"
        )
        frame.locator('nav button[data-page="model"]').click()
        frame.locator("#modelInput").fill("draft-model")
        frame.locator('nav button[data-page="status"]').click()

        frame.locator("#refreshAll").click()
        expect(frame.locator("#confirmLayer")).to_be_visible()
        self.assertEqual(native_dialogs, [])
        frame.locator("#confirmCancel").click()
        expect(frame.locator("#confirmLayer")).to_be_hidden()
        frame.locator('nav button[data-page="model"]').click()
        self.assertEqual(frame.locator("#modelInput").input_value(), "draft-model")

        frame.locator('nav button[data-page="status"]').click()
        frame.locator("#refreshAll").click()
        expect(frame.locator("#confirmLayer")).to_be_visible()
        frame.locator("#confirmOk").click()
        expect(frame.locator("#dirtyBadge")).to_be_hidden()

    def test_session_switch_saves_and_survives_filter_and_redraw(self):
        self.go("session")
        self.page.uncheck("#allowPrivate")
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        self.assertFalse(self.server.state["session"]["allow_private"])
        self.assertNotIn("会话", self.badge())

        # 停用列表项也会触发自动保存和 renderSession；不能把总开关恢复成旧值。
        self.page.uncheck("[data-session-check='0']")
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertIn("停用", self.page.locator("#sessionList").inner_text())

        self.page.select_option("#sessionTypeFilter", "group")
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.page.wait_for_timeout(20)
        self.assertFalse(self.page.is_checked("#allowPrivate"))

        saved = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertFalse(saved["allow_private"])
        self.assertFalse(saved["sessions"][0]["enabled"])
        self.assertFalse(self.server.state["session"]["sessions"][0]["enabled"])

        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertIn("停用", self.page.locator("#sessionList").inner_text())

    def test_cross_section_autosave_saves_each_value(self):
        self.go("model")
        self.page.click("#sourceManual")
        self.page.fill("#modelInput", "draft-model")
        self.page.press("#modelInput", "Tab")
        self.wait_saves("model", 1)
        models = [
            item["data"]["data"]["manual"]["model"]
            for item in self.server.saves("model")
        ]
        self.assertIn("draft-model", models)

        self.go("search")
        self.page.fill("#searchKey", "sk-draft")
        self.page.press("#searchKey", "Tab")
        self.wait_saves("search", 1)
        self.assertEqual(
            self.server.saves("search")[-1]["data"]["data"]["api_key"],
            "sk-draft",
        )

        self.go("rate")
        self.page.fill("#windowNumber", "15")
        self.page.press("#windowNumber", "Tab")
        self.wait_saves("rate", 1)
        self.assertEqual(self.server.state["rate"]["window_minutes"], 15)

    def test_admin_save_failure_keeps_draft_and_error(self):
        self.go("session")
        self.server.fail_next["admins"] = "磁盘写入失败"
        self.page.click("[data-admin-remove='0']")
        self.page.wait_for_selector("#adminMessage:has-text('保存管理员失败')")
        self.assertIn("管理员", self.badge())
        self.assertEqual(self.page.locator("#adminList .list-row").count(), 0)
        self.assertEqual(
            self.server.state["session"]["admin_users"][0]["uid"], "admin1"
        )

        # 失败后草稿仍在，下一次自动保存应提交移除后的列表。
        self.page.fill("#adminConnector", "qq")
        self.page.fill("#adminUid", "admin2")
        self.page.click("#addAdmin")
        self.page.wait_for_selector("#adminMessage:has-text('管理员已保存')")
        self.assertEqual(
            [item["uid"] for item in self.server.state["session"]["admin_users"]],
            ["admin2"],
        )

    def test_stale_tab_save_conflicts_without_silent_overwrite(self):
        self.pause_autosave()
        # 第二个页面在 A 保存前加载，两份页面都基于 revision 0 的快照。
        page_b = self.context.new_page()
        page_b.on("pageerror", lambda error: self.console_errors.append(str(error)))
        page_b.goto(self.server.url)
        page_b.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        page_b.evaluate("() => window.__dshConfig.setAutosavePaused(true)")
        # A 先保存频率=15：服务端 revision 升到 1。
        self.go("rate")
        self.page.fill("#windowNumber", "15")
        self.trigger_save("rate")
        self.page.wait_for_selector("#rateMessage:has-text('频率配置已保存')")
        self.assertEqual(self.server.revision, 1)
        self.assertEqual(self.server.state["rate"]["window_minutes"], 15)
        # B 仍以旧 revision 0 保存频率=20：必须被冲突拦截，不能静默覆盖 A。
        page_b.click('nav button[data-page="rate"]')
        page_b.fill("#windowNumber", "20")
        page_b.press("#windowNumber", "Tab")
        page_b.wait_for_selector("#rateMessage:has-text('其他页面更新')")
        self.assertEqual(self.server.state["rate"]["window_minutes"], 15)
        # B 的草稿与未保存标记保留，可刷新后重试。
        self.assertEqual(page_b.input_value("#windowNumber"), "20")
        self.assertIn("频率", page_b.locator("#dirtyBadge").inner_text())
        page_b.close()

    def test_save_success_with_state_readback_failure_warns_pending(self):
        self.server.save_state_none = True
        self.go("rate")
        self.page.fill("#windowNumber", "15")
        self.trigger_save("rate")
        self.page.wait_for_selector("#rateMessage:has-text('状态回读失败')")
        # 已真实写入服务端；草稿按已提交清理，不把旧状态当成功展示。
        self.assertEqual(self.server.state["rate"]["window_minutes"], 15)
        self.assertNotIn("频率", self.badge())
        # 刷新后恢复真实状态。
        self.server.save_state_none = False
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("rate")
        self.assertEqual(self.page.input_value("#windowNumber"), "15")

    def test_summary_reports_real_effective_rules(self):
        session = self.server.state["session"]
        session["sessions"] = []
        session["allow_private"] = True
        session["allow_group"] = True
        session["migration_required"] = False
        session["config_error"] = ""
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('不限制会话') >= 0"
        )

        session = self.server.state["session"]
        session["sessions"] = [
            {"umo": "qq:FriendMessage:u1", "connector": "qq", "type": "private", "enabled": False}
        ]
        session["migration_required"] = False
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('0 个启用会话') >= 0"
        )
        self.assertIn(
            "不会按空列表放开", self.page.locator("#sumSessionMeta").inner_text()
        )

        session = self.server.state["session"]
        session["sessions"] = []
        session["migration_required"] = True
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('迁移未完成') >= 0"
        )
        meta = self.page.locator("#sumSessionMeta").inner_text()
        self.assertIn("需添加明确的 UMO 会话", meta)
        self.assertNotIn("确认不限制", meta)

        session = self.server.state["session"]
        session["config_error"] = "UMO 配置损坏"
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('配置无效') >= 0"
        )
        self.assertIn(
            "UMO 配置损坏", self.page.locator("#sumSessionMeta").inner_text()
        )

        session = self.server.state["session"]
        session["config_error"] = ""
        session["sessions"] = []
        session["allow_private"] = False
        session["allow_group"] = False
        session["migration_required"] = False
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('全部会话类型关闭') >= 0"
        )

        session = self.server.state["session"]
        session["sessions"] = [
            {"umo": "qq:FriendMessage:u1", "connector": "qq", "type": "private", "enabled": True}
        ]
        session["allow_private"] = False
        session["allow_group"] = True
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('0 个生效会话') >= 0"
        )
        self.assertIn(
            "总开关关闭", self.page.locator("#sumSessionMeta").inner_text()
        )

    def test_refresh_keeps_autosaved_values(self):
        # 自动保存后刷新直接用已写入的值。
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "draft-model")
        self.trigger_save("model")
        self.wait_saves("model", 1)
        self.go("status")
        self.page.click("#refreshAll")
        self.page.wait_for_timeout(300)
        # 刷新后被确认框拦住时先取消，避免遮罩影响后续导航。
        if not self.page.locator("#confirmLayer").evaluate(
            "el => el.classList.contains('hidden')"
        ):
            self.page.click("#confirmCancel")
        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")

    def test_migration_requires_explicit_session_without_confirmation(self):
        self.server.state["session"].update({
            "allow_private": True,
            "allow_group": True,
            "sessions": [],
            "migration_required": True,
            "legacy_restrictions_pending": True,
            "config_error": "",
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.assertTrue(self.page.is_visible("#migrationBanner"))
        self.assertIn("添加明确的 UMO 会话", self.page.inner_text("#migrationBanner"))
        self.assertEqual(
            self.page.locator("#unrestrictedConfirmed").count(), 0
        )

        # 只能通过明确的 UMO 会话完成迁移，而不是勾选“不限制”。
        self.page.fill("#newUmo", "webchat:FriendMessage:new-user")
        self.page.click("#addSession")
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        saved = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(
            [item["umo"] for item in saved["sessions"]],
            ["webchat:FriendMessage:new-user"],
        )
        self.assertNotIn("umos_unrestricted_confirmed", saved)
        self.assertFalse(self.server.state["session"]["migration_required"])
        self.assertFalse(self.page.is_visible("#migrationBanner"))

    def test_add_session_accepts_colon_session_id_and_rejects_empty(self):
        """页面添加入口与宿主三段解析一致：qq:FriendMessage::b 的完整
        session_id 是 ":b"，应原样保存并可重载；只有完整 session_id 为空
        （qq:FriendMessage:）才拒绝，且不发出保存请求。"""
        self.go("session")
        self.page.fill("#newUmo", "qq:FriendMessage::b")
        self.page.click("#addSession")
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        saved = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(
            [item["umo"] for item in saved["sessions"]],
            ["qq:FriendMessage:u1", "qq:FriendMessage::b"],
        )
        self.assertEqual(saved["sessions"][1]["connector"], "qq")
        self.assertEqual(saved["sessions"][1]["type"], "private")

        # 重载后仍原样保留在会话列表和页面里。
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_function(
            "document.querySelectorAll('#sessionList .list-row').length === 2"
        )
        self.assertIn(
            "qq:FriendMessage::b", self.page.locator("#sessionList").inner_text()
        )
        self.assertEqual(
            self.server.state["session"]["sessions"][1]["umo"],
            "qq:FriendMessage::b",
        )

        # 完整会话 ID 为空仍被页面校验拒绝，且不产生保存请求。
        before = len(self.server.saves("sessions"))
        self.page.fill("#newUmo", "qq:FriendMessage:")
        self.page.click("#addSession")
        self.page.wait_for_selector(
            "#sessionMessage:has-text('完整会话 ID 不能为空')"
        )
        self.assertEqual(len(self.server.saves("sessions")), before)
        self.assertEqual(self.page.input_value("#newUmo"), "qq:FriendMessage:")

    def test_each_section_autosaves_only_its_own_changes(self):
        self.go("model")
        self.page.click("#sourceManual")
        self.page.fill("#modelInput", "draft-model")
        self.page.press("#modelInput", "Tab")
        self.wait_saves("model", 1)
        self.assertGreaterEqual(len(self.server.saves("model")), 1)
        self.assertEqual(self.server.saves("search"), [])
        self.assertEqual(self.server.saves("rate"), [])
        self.assertEqual(self.server.saves("advanced"), [])

        self.go("search")
        self.page.fill("#searchResults", "5")
        self.wait_saves("search", 1)
        self.assertGreaterEqual(len(self.server.saves("model")), 1)
        self.assertEqual(self.server.saves("advanced"), [])

        self.go("rate")
        self.page.fill("#windowNumber", "15")
        self.page.press("#windowNumber", "Tab")
        self.wait_saves("rate", 1)
        self.assertEqual(self.server.saves("advanced"), [])

        self.go("session")
        self.page.fill("#presetGames", "原神\n星穹铁道")
        self.page.press("#presetGames", "Tab")
        self.wait_saves("advanced", 1)
        self.assertEqual(
            self.server.saves("advanced")[-1]["data"]["data"]["preset_games"],
            ["原神", "星穹铁道"],
        )
        self.assertGreaterEqual(len(self.server.saves("model")), 1)
        self.assertGreaterEqual(len(self.server.saves("rate")), 1)

    def test_model_key_blank_keeps_and_new_value_overwrites(self):
        # 审查回归：LLM 页只保留简洁 Key 输入，不再有「Key 操作/清除」控件。
        self.go("model")
        self.page.click("#sourceManual")
        self.assertEqual(self.page.locator("#clearKey").count(), 0)
        page_text = self.page.locator("#page-model").inner_text()
        self.assertNotIn("Key 操作", page_text)
        self.assertNotIn("清除已保存 Key", page_text)

        self.page.fill("#apiKey", "sk-model-draft")
        self.page.press("#apiKey", "Tab")
        self.wait_saves("model", 1)
        payload = self.server.saves("model")[-1]["data"]["data"]["manual"]
        self.assertEqual(payload["api_key"], "sk-model-draft")
        self.assertNotIn("clear_key", payload)
        self.assertTrue(self.server.state["model"]["manual"]["key_configured"])
        self.page.wait_for_selector("#keyState:has-text('已保存 Key')")

        # 重载后留空保存：保留旧 Key，不覆盖。
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("model")
        self.assertEqual(self.page.input_value("#apiKey"), "")
        self.assertIn("已保存 Key", self.page.inner_text("#keyState"))
        before = len(self.server.saves("model"))
        self.page.fill("#modelInput", "blank-keeps-key")
        self.page.press("#modelInput", "Tab")
        self.wait_saves("model", before + 1)
        payload = self.server.saves("model")[-1]["data"]["data"]["manual"]
        self.assertEqual(payload["api_key"], "")
        self.assertTrue(self.server.state["model"]["manual"]["key_configured"])

        # 填入新值自动覆盖。
        before = len(self.server.saves("model"))
        self.page.fill("#apiKey", "sk-model-new")
        self.page.press("#apiKey", "Tab")
        self.wait_saves("model", before + 1)
        payload = self.server.saves("model")[-1]["data"]["data"]["manual"]
        self.assertEqual(payload["api_key"], "sk-model-new")
        self.assertTrue(self.server.state["model"]["manual"]["key_configured"])

    def scroll_state(self):
        return self.page.evaluate(
            """() => ({
              side: document.querySelector('.sidebar').scrollTop,
              ws: document.querySelector('.workspace').scrollTop,
              win: window.scrollY,
            })"""
        )

    def reset_scroll(self):
        self.page.evaluate(
            """() => {
              document.querySelector('.sidebar').scrollTop = 0;
              document.querySelector('.workspace').scrollTop = 0;
              window.scrollTo(0, 0);
            }"""
        )

    def wheel_at(self, x, y, times=3):
        self.page.mouse.move(x, y)
        self.page.wait_for_timeout(80)
        for _ in range(times):
            self.page.mouse.wheel(0, 120)
            self.page.wait_for_timeout(80)

    def test_sidebar_stays_fixed_while_content_scrolls(self):
        # 桌面端：右栏自己滚，左栏固定；整页不跟着滚。
        self.page.set_viewport_size({"width": 1280, "height": 480})
        self.go("search")
        dims = self.page.evaluate(
            """() => {
              const ws = document.querySelector('.workspace');
              return { client: ws.clientHeight, scroll: ws.scrollHeight };
            }"""
        )
        self.assertGreater(dims["scroll"], dims["client"])
        self.page.evaluate("document.querySelector('.workspace').scrollTop = 600")
        self.page.wait_for_timeout(80)
        top = self.page.evaluate(
            "document.querySelector('.sidebar').getBoundingClientRect().top"
        )
        self.assertLessEqual(abs(top), 1)
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        # 整页不可滚：再滚窗口也不动。
        self.page.evaluate("window.scrollTo(0, 600)")
        self.assertEqual(self.page.evaluate("window.scrollY"), 0)
        style = self.page.evaluate(
            """() => {
              const el = document.querySelector('.sidebar');
              const cs = getComputedStyle(el);
              return { position: cs.position, height: cs.height,
                       overflowY: cs.overflowY };
            }"""
        )
        self.assertEqual(style["position"], "sticky")
        self.assertEqual(style["overflowY"], "auto")
        self.assertEqual(style["height"], "480px")

        # 短视口真实滚轮：左栏上半、下半与底部多个坐标都要只滚左栏。
        self.page.set_viewport_size({"width": 1280, "height": 220})
        self.go("search")
        sidebar_points = ((116, 110), (116, 187), (100, 180), (180, 190))
        for theme in ("light", "dark"):
            for x, y in sidebar_points:
                with self.subTest(theme=theme, point=(x, y)):
                    self.page.evaluate(
                        "(t) => document.documentElement.setAttribute('data-theme', t)",
                        theme,
                    )
                    self.reset_scroll()
                    self.wheel_at(x, y)
                    state = self.scroll_state()
                    self.assertGreater(state["side"], 0)
                    self.assertEqual(state["ws"], 0)
                    self.assertEqual(state["win"], 0)
        self.page.evaluate(
            "() => document.documentElement.setAttribute('data-theme', 'light')"
        )

        # 右栏也自己承担滚动：滚轮命中正文时只滚正文，左栏不动。
        for point in ((950, 160), (700, 200)):
            with self.subTest(point=point):
                self.reset_scroll()
                self.wheel_at(*point)
                state = self.scroll_state()
                self.assertGreater(state["ws"], 0)
                self.assertEqual(state["side"], 0)
                self.assertEqual(state["win"], 0)

        # 正常窗口：单次滚轮增量必须与输入一致，补偿不能叠加成两倍。
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.go("search")
        for delta in (60, 96, 120):
            with self.subTest(delta=delta):
                self.reset_scroll()
                self.page.mouse.move(950, 160)
                self.page.wait_for_timeout(80)
                self.page.mouse.wheel(0, delta)
                self.page.wait_for_timeout(200)
                state = self.scroll_state()
                self.assertEqual(state["ws"], delta)
                self.assertEqual(state["side"], 0)
                self.assertEqual(state["win"], 0)
        # 左栏可滚时同样只走一份增量。
        self.page.set_viewport_size({"width": 1280, "height": 220})
        self.go("search")
        self.reset_scroll()
        self.page.mouse.move(116, 110)
        self.page.wait_for_timeout(80)
        self.page.mouse.wheel(0, 40)
        self.page.wait_for_timeout(200)
        self.assertEqual(self.scroll_state()["side"], 40)

        # 栏内输入框这类内部滚动区自己消费滚轮，右栏不再跟着动。
        self.page.set_viewport_size({"width": 1280, "height": 800})
        self.go("session")
        self.reset_scroll()
        self.page.evaluate(
            """() => {
              const ta = document.getElementById('presetGames');
              ta.value = Array.from({length: 40}, (_, i) => '游戏' + i).join('\\n');
              ta.dispatchEvent(new Event('input', { bubbles: true }));
              ta.scrollIntoView({ block: 'center' });
              ta.scrollTop = 0;
            }"""
        )
        self.page.wait_for_timeout(400)
        ta = self.page.evaluate(
            """() => {
              const el = document.getElementById('presetGames');
              const rect = el.getBoundingClientRect();
              return { x: rect.x, y: rect.y, width: rect.width,
                       height: rect.height, scroll: el.scrollHeight,
                       client: el.clientHeight };
            }"""
        )
        self.assertGreater(ta["scroll"], ta["client"])
        before = self.scroll_state()
        self.page.mouse.move(
            ta["x"] + ta["width"] / 2, ta["y"] + ta["height"] / 2
        )
        self.page.wait_for_timeout(80)
        self.page.mouse.wheel(0, 60)
        self.page.wait_for_timeout(250)
        state = self.scroll_state()
        nested = self.page.evaluate(
            "() => document.getElementById('presetGames').scrollTop"
        )
        self.assertGreater(nested, 0)
        self.assertEqual(state["ws"], before["ws"])
        self.assertEqual(state["side"], 0)

    def test_sidebar_layout_on_narrow_viewports(self):
        # 窄屏改为横向/网格导航，不再粘住视口，也不能遮挡或横向溢出。
        for width, height, expected_position in (
            (360, 800, "static"),
            (390, 844, "static"),
            (768, 1024, "sticky"),
        ):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": height})
                self.go("search")
                position = self.page.evaluate(
                    "getComputedStyle(document.querySelector('.sidebar')).position"
                )
                self.assertEqual(position, expected_position)
                overflow = self.page.evaluate(
                    "document.documentElement.scrollWidth - window.innerWidth"
                )
                self.assertLessEqual(overflow, 1)
                self.assertTrue(
                    self.page.is_visible(".nav button[data-page='status']")
                )

    def test_toast_success_is_light_green_in_both_themes(self):
        # 自动保存成功提示：浅绿底、深绿字、细绿边框；警告/错误仍区分。
        self.go("search")
        self.page.fill("#searchResults", "5")
        self.wait_saves("search", 1)
        self.page.wait_for_selector("#toast:has-text('已自动保存')")

        def toast_style():
            return self.page.evaluate(
                """() => {
                  const cs = getComputedStyle(document.getElementById('toast'));
                  return { bg: cs.backgroundColor, color: cs.color,
                           border: cs.borderTopColor, width: cs.borderTopWidth };
                }"""
            )

        light = toast_style()
        self.assertEqual(light["bg"], "rgb(233, 247, 239)")
        self.assertEqual(light["color"], "rgb(18, 99, 63)")
        self.assertEqual(light["border"], "rgb(183, 226, 201)")
        self.assertEqual(light["width"], "1px")

        self.page.evaluate(
            "document.documentElement.setAttribute('data-theme', 'dark')"
        )
        self.page.fill("#searchResults", "6")
        self.wait_saves("search", 2)
        self.page.wait_for_selector("#toast:has-text('已自动保存')")
        dark = toast_style()
        self.assertEqual(dark["bg"], "rgb(18, 49, 38)")
        self.assertEqual(dark["color"], "rgb(143, 227, 180)")
        self.assertEqual(dark["border"], "rgb(31, 90, 61)")
        self.assertEqual(dark["width"], "1px")

        # 警告/错误使用各自的语义色，不与成功态混同。
        warn = self.page.evaluate(
            """() => {
              const el = document.getElementById('toast');
              el.classList.add('warn');
              const cs = getComputedStyle(el);
              const color = cs.color;
              el.classList.remove('warn');
              return color;
            }"""
        )
        self.assertNotEqual(warn, dark["color"])
        self.assertNotEqual(warn, light["color"])

    def test_beforeunload_warns_only_with_drafts(self):
        self.assertFalse(self.page.evaluate(
            "() => { const e = new Event('beforeunload', { cancelable: true }); "
            "window.dispatchEvent(e); return e.defaultPrevented; }"
        ))
        self.go("model")
        self.page.fill("#modelInput", "draft-model")
        self.assertTrue(self.page.evaluate(
            "() => { const e = new Event('beforeunload', { cancelable: true }); "
            "window.dispatchEvent(e); return e.defaultPrevented; }"
        ))
        self.clear_message("modelMessage")
        self.trigger_save("model")
        self.page.wait_for_selector("#modelMessage:has-text('已保存')")
        self.assertFalse(self.page.evaluate(
            "() => { const e = new Event('beforeunload', { cancelable: true }); "
            "window.dispatchEvent(e); return e.defaultPrevented; }"
        ))

    def test_test_actions_do_not_discard_other_drafts(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "draft-model")
        self.clear_message("modelMessage")
        self.page.click("#testModel")
        self.page.wait_for_selector("#modelMessage:has-text('mock ok')")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")
        self.assertIn("模型", self.badge())

        self.go("search")
        self.page.fill("#searchKey", "sk-draft")
        self.page.click("#searchTest")
        self.page.wait_for_selector("#searchTestResult:has-text('mock search ok')")
        self.assertIn("模型", self.badge())

        self.go("status")
        self.clear_message("sumModelTest")
        self.page.click("#testCurrentModel")
        self.page.wait_for_selector("#sumModelTest:has-text('mock ok')")
        self.assertIn("模型", self.badge())
        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")

    def test_model_save_does_not_overwrite_edits_during_response(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "model-A")
        gate = self.server.hold_next("config-center/save", "model")
        try:
            self.trigger_save("model")
            self.assertTrue(gate["entered"].wait(5), "模型保存请求未到达模拟桥")
            self.page.fill("#modelInput", "model-B")
        finally:
            gate["release"].set()
        self.assertTrue(
            self.page.locator("#dirtyBadge").evaluate(
                "el => !el.classList.contains('hidden')"
            )
        )
        self.assertEqual(self.page.input_value("#modelInput"), "model-B")
        self.assertEqual(
            self.server.saves("model")[0]["data"]["data"]["manual"]["model"],
            "model-A",
        )
        # 下一次保存必须提交新值 B，而不是把 A 再写一遍。
        self.clear_message("modelMessage")
        self.trigger_save("model")
        self.page.wait_for_function(
            "document.querySelector('#modelMessage').textContent.includes('已保存')"
        )
        self.assertEqual(
            self.server.saves("model")[-1]["data"]["data"]["manual"]["model"],
            "model-B",
        )

    def test_model_save_failure_keeps_edits_made_during_response(self):
        self.go("model")
        self.page.fill("#modelInput", "model-A")
        gate = self.server.hold_next(
            "config-center/save", "model", fail="模拟磁盘写入失败"
        )
        try:
            self.trigger_save("model")
            self.assertTrue(gate["entered"].wait(5), "模型保存请求未到达模拟桥")
            self.page.fill("#modelInput", "model-B")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#modelMessage:has-text('保存失败，草稿已保留')")
        self.assertEqual(self.page.input_value("#modelInput"), "model-B")
        self.assertIn("模型", self.badge())
        self.assertEqual(self.server.state["model"]["manual"]["model"], "saved-model")
        self.assertEqual(
            self.server.saves("model")[-1]["data"]["data"]["manual"]["model"],
            "model-A",
        )

        self.clear_message("modelMessage")
        self.trigger_save("model")
        self.page.wait_for_selector("#modelMessage:has-text('已保存')")
        self.assertEqual(
            self.server.saves("model")[-1]["data"]["data"]["manual"]["model"],
            "model-B",
        )

    def test_repeated_model_save_submits_snapshots_in_order(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "model-A")
        self.trigger_save("model")
        self.wait_saves("model", 1)
        self.page.fill("#modelInput", "model-B")
        self.trigger_save("model")
        self.wait_saves("model", 2)
        saved = [
            item["data"]["data"]["manual"]["model"]
            for item in self.server.saves("model")
        ]
        self.assertEqual(saved[:2], ["model-A", "model-B"])
        self.assertEqual(self.page.input_value("#modelInput"), "model-B")

    def test_cross_section_saves_run_serially_with_independent_snapshots(self):
        # 三个分区各自入队，串行写入且互不覆盖快照。
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "model-A")
        self.go("search")
        self.page.fill("#searchKey", "sk-concurrent")
        self.go("rate")
        self.page.fill("#windowNumber", "15")
        self.trigger_save("model")
        self.trigger_save("search")
        self.trigger_save("rate")
        self.wait_saves("model", 1)
        self.wait_saves("search", 1)
        self.wait_saves("rate", 1)
        raw = [item["data"]["section"] for item in self.server.saves()]
        sections = list(dict.fromkeys(raw))
        self.assertEqual(sections, ["model", "search", "rate"])
        self.assertEqual(self.server.state["model"]["manual"]["model"], "model-A")
        self.assertEqual(self.server.state["rate"]["window_minutes"], 15)

    def test_key_values_survive_cross_section_autosave(self):
        # 搜索 Key 已无「清除」控件；空白保留同地址旧 Key，新值整值覆盖。
        self.go("model")
        self.page.click("#sourceManual")
        self.page.fill("#modelInput", "draft-model")
        self.page.fill("#apiKey", "sk-model-draft")
        self.page.press("#apiKey", "Tab")
        self.wait_saves("model", 1)
        self.go("search")
        self.page.fill("#searchKey", "sk-search-draft")
        self.page.press("#searchKey", "Tab")
        self.wait_saves("search", 1)
        self.go("session")
        self.page.fill("#presetGames", "原神\n新游戏")
        self.page.press("#presetGames", "Tab")
        self.wait_saves("advanced", 1)
        self.assertEqual(self.page.locator("#clearSearchKey").count(), 0)

        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")
        self.assertEqual(
            self.server.saves("model")[-1]["data"]["data"]["manual"]["api_key"],
            "sk-model-draft",
        )
        self.go("search")
        self.assertEqual(self.page.input_value("#searchKey"), "")
        search_payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertFalse(search_payload["clear_api_key"])
        self.assertEqual(search_payload["api_key"], "sk-search-draft")

    def test_admin_edit_during_save_is_not_marked_saved(self):
        self.go("session")
        gate = self.server.hold_next("config-center/save", "admins")
        try:
            self.page.click("[data-admin-remove='0']")
            self.assertTrue(gate["entered"].wait(5), "管理员保存请求未到达模拟桥")
            self.page.fill("#adminUid", "admin2")
            self.page.click("#addAdmin")
        finally:
            gate["release"].set()
        # 第一次提交只包含移除操作；排队中的第二次提交新草稿。
        self.wait_saves("admins", 2)
        first = self.server.saves("admins")[0]["data"]["data"]
        second = self.server.saves("admins")[1]["data"]["data"]
        self.assertEqual(first["admin_users"], [])
        self.assertEqual(
            [item["uid"] for item in second["admin_users"]],
            ["admin2"],
        )
        self.page.wait_for_function(
            "document.getElementById('dirtyBadge').classList.contains('hidden')"
        )
        self.assertEqual(self.page.locator("#adminList .list-row").count(), 1)
        self.assertIn("admin2", self.page.locator("#adminList").inner_text())
        self.assertEqual(
            [item["uid"] for item in self.server.state["session"]["admin_users"]],
            ["admin2"],
        )
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.assertIn("admin2", self.page.locator("#adminList").inner_text())

    def test_admin_auto_save_failure_keeps_draft(self):
        self.go("session")
        gate = self.server.hold_next(
            "config-center/save", "admins", fail="模拟管理员写入失败"
        )
        try:
            self.page.click("[data-admin-remove='0']")
            self.assertTrue(gate["entered"].wait(5), "管理员保存请求未到达模拟桥")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#adminMessage:has-text('保存管理员失败')")
        self.assertEqual(
            [item["uid"] for item in self.server.state["session"]["admin_users"]],
            ["admin1"],
        )
        self.assertEqual(self.page.locator("#adminList .list-row").count(), 0)
        self.assertIn("管理员", self.badge())

        # 下一次自动保存提交草稿并清除未保存标记。
        self.page.fill("#adminUid", "admin2")
        self.page.click("#addAdmin")
        self.page.wait_for_selector("#adminMessage:has-text('管理员已保存')")
        self.assertEqual(
            [item["uid"] for item in self.server.state["session"]["admin_users"]],
            ["admin2"],
        )

    def test_session_switches_and_added_umo_survive_inflight_save(self):
        self.go("session")
        gate = self.server.hold_next("config-center/save", "sessions")
        try:
            self.page.uncheck("#allowPrivate")
            self.assertTrue(gate["entered"].wait(5), "会话保存请求未到达模拟桥")
            self.page.uncheck("[data-session-check='0']")
            self.page.fill("#newUmo", "qq:FriendMessage:u2")
            self.page.click("#addSession")
        finally:
            gate["release"].set()
        # 总开关保存只改变放行策略，不携带 sessions；后续自动保存才提交
        # 新增会话和勾选状态。
        first = self.server.saves("sessions")[0]["data"]["data"]
        self.assertFalse(first["allow_private"])
        self.assertNotIn("sessions", first)

        self.page.select_option("#sessionTypeFilter", "")
        self.page.wait_for_function(
            "document.querySelectorAll('#sessionList .list-row').length === 2"
        )
        self.page.wait_for_function(
            "document.getElementById('dirtyBadge').classList.contains('hidden')"
        )
        last = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(len(last["sessions"]), 2)
        self.assertFalse(last["sessions"][0]["enabled"])
        self.assertEqual(len(self.server.state["session"]["sessions"]), 2)
        self.assertFalse(self.server.state["session"]["sessions"][0]["enabled"])

    def test_identity_mutation_keeps_unrelated_drafts_and_reports_failure(self):
        self.pause_autosave()
        self.go("rate")
        self.page.evaluate("document.querySelector('.identity-details').open = true")
        self.page.fill("#identityUids", "u1, u2")
        gate = self.server.hold_next("config-center/save", "identity")
        try:
            self.page.click("#linkIdentity")
            self.assertTrue(gate["entered"].wait(5), "关联请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "draft-model")
        finally:
            gate["release"].set()
        self.page.wait_for_selector(
            "#rateMessage:has-text('共同历史只计一次')", state="attached"
        )
        self.go("rate")
        self.assertEqual(self.page.locator("#linkList .list-row").count(), 1)
        self.assertIn("u1、u2", self.page.locator("#linkList").inner_text())
        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")

        gate = self.server.hold_next(
            "config-center/save", "identity", fail="模拟关联服务失败"
        )
        try:
            self.go("rate")
            self.page.click("#linkList [data-unlink]")
            self.assertTrue(gate["entered"].wait(5), "解除请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "draft-model-2")
        finally:
            gate["release"].set()
        self.page.wait_for_selector(
            "#rateMessage:has-text('解除失败，草稿未受影响')", state="attached"
        )
        self.go("rate")
        self.assertEqual(self.page.locator("#linkList .list-row").count(), 1)
        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model-2")
        self.assertIn("模型", self.badge())

    def test_identity_link_failure_and_unlink_success_keep_drafts(self):
        self.pause_autosave()
        self.go("rate")
        self.page.evaluate("document.querySelector('.identity-details').open = true")
        self.page.fill("#identityUids", "u1, u2")
        gate = self.server.hold_next(
            "config-center/save", "identity", fail="模拟关联服务失败"
        )
        try:
            self.page.click("#linkIdentity")
            self.assertTrue(gate["entered"].wait(5), "关联请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "draft-model")
        finally:
            gate["release"].set()
        self.page.wait_for_selector(
            "#rateMessage:has-text('关联失败，草稿未受影响')", state="attached"
        )
        self.go("rate")
        self.assertEqual(self.page.locator("#linkList .list-row").count(), 0)
        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")

        self.go("rate")
        self.page.evaluate("document.querySelector('.identity-details').open = true")
        self.page.fill("#identityUids", "u1, u2")
        self.page.click("#linkIdentity")
        self.page.wait_for_selector(
            "#rateMessage:has-text('共同历史只计一次')", state="attached"
        )
        self.assertEqual(self.page.locator("#linkList .list-row").count(), 1)
        self.assertIn("u1、u2", self.page.locator("#linkList").inner_text())

        self.page.click("#linkList [data-unlink]")
        self.page.wait_for_selector(
            "#rateMessage:has-text('已解除关联')", state="attached"
        )
        self.assertEqual(self.page.locator("#linkList .list-row").count(), 0)
        self.go("model")
        self.assertEqual(self.page.input_value("#modelInput"), "draft-model")
        self.assertIn("模型", self.badge())

    def test_stale_model_list_and_test_do_not_replace_newer_draft(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "old-model")
        gate = self.server.hold_next("config-center/model/models")
        try:
            self.page.click("#fetchModels")
            self.assertTrue(gate["entered"].wait(5), "模型列表请求未到达模拟桥")
            self.page.fill("#modelInput", "new-model")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#modelMessage:has-text('列表基于点击时的表单')")
        self.assertEqual(self.page.input_value("#modelInput"), "new-model")
        self.assertEqual(self.page.locator("#modelList option").count(), 2)

        self.clear_message("modelMessage")
        gate = self.server.hold_next("config-center/model/test")
        try:
            self.page.click("#testModel")
            self.assertTrue(gate["entered"].wait(5), "模型测试请求未到达模拟桥")
            self.page.fill("#modelInput", "newer-model")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#modelMessage:has-text('测试针对点击时的表单')")
        self.assertEqual(self.page.input_value("#modelInput"), "newer-model")
        self.assertIn("模型", self.badge())

    def test_refresh_is_rejected_while_mutation_is_pending(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "pending-model")
        gate = self.server.hold_next("config-center/save", "model")
        try:
            self.trigger_save("model")
            self.assertTrue(gate["entered"].wait(5), "模型保存请求未到达模拟桥")
            self.go("status")
            before = len([
                item for item in self.server.requests
                if item["path"] == "config-center/state"
            ])
            self.page.click("#refreshAll")
            self.page.wait_for_timeout(150)
            after = len([
                item for item in self.server.requests
                if item["path"] == "config-center/state"
            ])
            # 写入进行中时刷新不会读取新状态。
            self.assertGreaterEqual(after, before)
        finally:
            gate["release"].set()
        self.wait_saves("model", 1)
        self.assertEqual(self.page.input_value("#modelInput"), "pending-model")
        self.assertEqual(
            self.server.state["model"]["manual"]["model"], "pending-model"
        )

    def test_partial_refresh_rebuilds_search_mode_cache(self):
        # 先在搜索页渲染一次，让客户端缓存旧的组顺序。
        self.go("search")
        self.assertIn(
            "Astrbot与搜索插件", self.mode_state()
        )
        # 服务端搜索顺序随后变化，但客户端仍保留上一次缓存。
        self.server.state["search"].update({
            "mode": "web",
            "mode_order": ["web", "astrbot", "api"],
            "mode_enabled": ["web"],
        })
        self.go("status")
        gate = self.server.hold_next("config-center/state")
        try:
            self.page.click("#refreshAll")
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
            # 刷新响应在途时只修改频率分区，搜索分区应整体采用服务端快照。
            self.go("rate")
            self.page.fill("#windowNumber", "15")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#globalNotice:has-text('刷新期间的新修改已保留')")
        self.go("search")
        self.assertIn(
            "网页搜索", self.mode_state()
        )
        self.trigger_save("search")
        self.page.wait_for_selector("#searchSaveMessage:has-text('搜索配置已保存')")
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["mode"], "web")
        self.assertEqual(payload["mode_order"], ["web", "astrbot", "api"])
        self.assertEqual(payload["mode_enabled"], ["web"])
        self.assertNotIn("auto", payload["mode_enabled"])

        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        self.assertIn(
            "网页搜索", self.mode_state()
        )

    def test_refresh_response_keeps_edits_made_while_loading(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "model-A")
        self.go("status")
        gate = self.server.hold_next("config-center/state")
        try:
            self.refresh_all()
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "model-B")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#globalNotice:has-text('刷新期间的新修改已保留')")
        self.assertEqual(self.page.input_value("#modelInput"), "model-B")
        self.assertIn("模型", self.badge())

    def test_session_save_refreshes_baseline_and_second_save_keeps_state(self):
        self.go("session")
        self.page.uncheck("#allowPrivate")
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")

        # 保存成功后必须立即采用服务端基线，而不是从旧 state.data 重建草稿。
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertIn("0 个生效会话", self.page.locator("#sumSession").inner_text())
        self.assertIn("私聊关 · 群聊开", self.page.locator("#sumSessionMeta").inner_text())
        self.assertNotIn("会话", self.badge())
        self.assertFalse(self.server.state["session"]["allow_private"])

        # 第二次自动保存不能把服务器值改回旧值。
        self.page.uncheck("#allowGroup")
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertFalse(payload["allow_private"])
        self.assertFalse(payload["allow_group"])
        self.assertFalse(self.server.state["session"]["allow_private"])

        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('0 个生效会话') >= 0"
        )
        self.go("session")
        self.assertFalse(self.page.is_checked("#allowPrivate"))

    def test_stale_refresh_cannot_overwrite_saves_that_complete_during_read(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "before-refresh")
        self.trigger_save("model")
        self.page.wait_for_selector("#modelMessage:has-text('已保存')")

        self.go("status")
        gate = self.server.hold_next("config-center/state")
        try:
            self.refresh_all()
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "after-refresh")
            self.trigger_save("model")
            self.wait_saves("model", 2)
            self.go("session")
            self.page.uncheck("#allowPrivate")
            self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        finally:
            gate["release"].set()

        self.page.wait_for_selector("#globalNotice:has-text('刷新结果已过期')")
        self.assertEqual(self.page.input_value("#modelInput"), "after-refresh")
        self.assertEqual(self.server.state["model"]["manual"]["model"], "after-refresh")
        self.assertFalse(self.server.state["session"]["allow_private"])

    def test_save_started_during_refresh_survives_refresh_response_first(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "before-refresh")
        self.trigger_save("model")
        self.wait_saves("model", 1)
        self.go("status")
        gate = self.server.hold_next("config-center/state")
        try:
            self.refresh_all()
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "model-during-refresh")
        finally:
            gate["release"].set()
        self.page.wait_for_timeout(200)
        self.trigger_save("model")
        self.wait_saves("model", 2)
        self.assertEqual(
            self.server.state["model"]["manual"]["model"], "model-during-refresh"
        )

    def test_refresh_response_before_save_keeps_submitted_session_draft(self):
        self.go("status")
        refresh_gate = self.server.hold_next("config-center/state")
        self.page.click("#refreshAll")
        self.assertTrue(refresh_gate["entered"].wait(5), "刷新请求未到达模拟桥")

        self.go("session")
        save_gate = self.server.hold_next("config-center/save", "sessions")
        self.page.uncheck("#allowPrivate")
        self.assertTrue(save_gate["entered"].wait(5), "刷新期间保存请求未到达模拟桥")
        try:
            refresh_gate["release"].set()
            self.page.wait_for_selector("#globalNotice:has-text('刷新结果已过期')")
            # 在途提交不能被旧刷新快照重建为服务器旧草稿。
            self.assertFalse(self.page.is_checked("#allowPrivate"))
            self.assertIn("会话", self.badge())
            self.assertTrue(self.server.state["session"]["allow_private"])
        finally:
            save_gate["release"].set()

        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存。')")
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertNotIn("会话", self.badge())
        self.assertIn("0 个生效会话", self.page.locator("#sumSession").inner_text())
        self.assertFalse(self.server.state["session"]["allow_private"])

        # 第二次自动保存必须沿用刚确认的服务端基线，而不是提交前旧草稿。
        self.page.uncheck("#allowGroup")
        self.wait_saves("sessions", 2)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertFalse(payload["allow_private"])
        self.assertFalse(self.server.state["session"]["allow_private"])

    def test_session_edits_during_inflight_save_are_queued(self):
        self.go("status")
        refresh_gate = self.server.hold_next("config-center/state")
        self.page.click("#refreshAll")
        self.assertTrue(refresh_gate["entered"].wait(5), "刷新请求未到达模拟桥")

        self.go("session")
        save_gate = self.server.hold_next("config-center/save", "sessions")
        self.page.uncheck("#allowPrivate")
        self.assertTrue(save_gate["entered"].wait(5), "刷新期间保存请求未到达模拟桥")
        try:
            refresh_gate["release"].set()
            self.page.wait_for_selector("#globalNotice:has-text('刷新结果已过期')")
            self.assertFalse(self.page.is_checked("#allowPrivate"))
            self.assertIn("会话", self.badge())

            # 刷新返回后、保存响应前再编辑：自动保存排队提交最新草稿。
            self.page.uncheck("#allowGroup")
            self.assertIn("会话", self.badge())
        finally:
            save_gate["release"].set()

        self.wait_saves("sessions", 2)
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存。')")
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertFalse(self.page.is_checked("#allowGroup"))
        self.assertNotIn("会话", self.badge())
        # 第一次提交只包含提交时的 allow_private=false / allow_group=true。
        first = self.server.saves("sessions")[0]["data"]["data"]
        self.assertFalse(first["allow_private"])
        self.assertTrue(first["allow_group"])
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertFalse(payload["allow_private"])
        self.assertFalse(payload["allow_group"])
        self.assertFalse(self.server.state["session"]["allow_group"])

    def test_refresh_response_before_failed_save_keeps_session_draft(self):
        self.go("status")
        refresh_gate = self.server.hold_next("config-center/state")
        self.page.click("#refreshAll")
        self.assertTrue(refresh_gate["entered"].wait(5), "刷新请求未到达模拟桥")

        self.go("session")
        save_gate = self.server.hold_next(
            "config-center/save", "sessions", fail="模拟会话保存失败"
        )
        self.page.uncheck("#allowPrivate")
        self.assertTrue(save_gate["entered"].wait(5), "刷新期间保存请求未到达模拟桥")
        try:
            refresh_gate["release"].set()
            self.page.wait_for_selector("#globalNotice:has-text('刷新结果已过期')")
            self.assertFalse(self.page.is_checked("#allowPrivate"))
            self.assertIn("会话", self.badge())
        finally:
            save_gate["release"].set()

        self.page.wait_for_selector(
            "#sessionMessage:has-text('保存失败，草稿已保留：模拟会话保存失败')"
        )
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertIn("会话", self.badge())
        self.assertTrue(self.server.state["session"]["allow_private"])

        # 失败后草稿仍可再次自动提交。
        self.page.uncheck("#allowGroup")
        self.wait_saves("sessions", 2)
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存。')")
        self.assertFalse(self.page.is_checked("#allowPrivate"))
        self.assertFalse(self.page.is_checked("#allowGroup"))
        self.assertNotIn("会话", self.badge())
        self.assertFalse(self.server.state["session"]["allow_private"])
        self.assertFalse(self.server.state["session"]["allow_group"])

    def test_refresh_response_before_save_keeps_new_model_edit(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "draft-before-refresh")
        self.go("status")
        gate = self.server.hold_next("config-center/state")
        try:
            self.refresh_all()
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
            self.go("model")
            self.page.fill("#modelInput", "edit-after-refresh")
        finally:
            gate["release"].set()
        self.page.wait_for_selector("#globalNotice:has-text('刷新期间的新修改已保留')")
        self.assertEqual(self.page.input_value("#modelInput"), "edit-after-refresh")
        self.trigger_save("model")
        self.wait_saves("model", 1)
        self.assertEqual(
            self.server.state["model"]["manual"]["model"], "edit-after-refresh"
        )

    def test_stale_refresh_cannot_restore_identity_changed_during_read(self):
        self.go("status")
        gate = self.server.hold_next("config-center/state")
        try:
            self.page.click("#refreshAll")
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
            self.go("rate")
            self.page.evaluate("document.querySelector('.identity-details').open = true")
            self.page.fill("#identityUids", "u1, u2")
            self.page.click("#linkIdentity")
            self.page.wait_for_selector(
                "#rateMessage:has-text('共同历史只计一次')", state="attached"
            )
            self.assertEqual(self.page.locator("#linkList .list-row").count(), 1)
        finally:
            gate["release"].set()

        self.page.wait_for_selector("#globalNotice:has-text('刷新结果已过期')")
        self.go("rate")
        self.assertEqual(self.page.locator("#linkList .list-row").count(), 1)
        self.assertIn("u1、u2", self.page.locator("#linkList").inner_text())

    def test_refresh_failure_keeps_draft_and_confirmed_state(self):
        self.pause_autosave()
        self.go("model")
        self.page.fill("#modelInput", "draft-before-failure")
        self.go("status")
        gate = self.server.hold_next("config-center/state", fail="模拟刷新失败")
        try:
            self.page.click("#refreshAll")
            self.page.wait_for_selector("#confirmLayer:not(.hidden)")
            self.page.click("#confirmOk")
            self.assertTrue(gate["entered"].wait(5), "刷新请求未到达模拟桥")
        finally:
            gate["release"].set()

        self.page.wait_for_selector(
            "#modelMessage:has-text('操作失败：模拟刷新失败')", state="attached"
        )
        self.assertIn(
            "模拟刷新失败", self.page.locator(".section.active .load-error").inner_text()
        )
        self.assertIn("模型", self.badge())
        self.assertEqual(self.page.input_value("#modelInput"), "draft-before-failure")

    def test_summary_partial_enabled_and_migration_with_sessions(self):
        session = self.server.state["session"]
        session.update({
            "sessions": [
                {"umo": "qq:FriendMessage:u1", "connector": "qq", "type": "private", "enabled": True},
                {"umo": "qq:GroupMessage:g1", "connector": "qq", "type": "group", "enabled": True},
            ],
            "allow_private": True,
            "allow_group": False,
            "migration_required": False,
            "config_error": "",
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('1 个生效会话') >= 0"
        )
        self.assertIn(
            "部分启用项被类型开关关闭",
            self.page.locator("#sumSessionMeta").inner_text(),
        )

        session["migration_required"] = True
        session["legacy_restrictions_pending"] = True
        session["sessions"] = [
            {"umo": "qq:FriendMessage:u1", "connector": "qq", "type": "private", "enabled": True},
        ]
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumSession').textContent.indexOf('1 个迁移期生效会话') >= 0"
        )
        self.assertIn(
            "仅列表内启用的精确 UMO 放行",
            self.page.locator("#sumSessionMeta").inner_text(),
        )

    def test_session_repair_unset_list_is_visible_and_clickable(self):
        """真实 4.29.4 把未配置的 sessions 保留为 null 时，管理员能看到
        “尚未配置”诊断，并一键明确保存空列表（不限制）。"""
        session = self.server.state["session"]
        session.update({
            "sessions": [],
            "config_error": "sessions_unset",
            "sessions_raw_type": "NoneType",
            "invalid_sessions": [],
            "migration_required": False,
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector(
            "#sessionRepair:has-text('null（尚未配置）')"
        )
        self.assertIn(
            "会话列表尚未设置",
            self.page.locator("#sessionMessage").inner_text(),
        )
        self.assertIn(
            "普通用户已拒绝",
            self.page.locator("#sessionMessage").inner_text(),
        )
        self.page.click("#clearSessionsUnrestricted")
        self.wait_saves("sessions", 1)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(self.server.state["session"]["sessions"], [])
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存。')")
        self.assertEqual(self.console_errors, [])

    def test_session_repair_lists_invalid_entries_and_clears_them(self):
        """读路径给出逐条诊断（与写路径同一套规则），管理员可看清哪一条
        为什么无效，并用同一个入口清空保存为明确不限制。"""
        session = self.server.state["session"]
        session.update({
            "sessions": [
                {"umo": "qq:FriendMessage:u1", "connector": "qq", "type": "private", "enabled": True},
            ],
            "config_error": "invalid_session_entries",
            "sessions_raw_type": "list",
            "invalid_sessions": [
                {"index": 1, "umo": "bad", "reason": "umo_malformed"},
                {"index": 2, "umo": "", "reason": "entry_not_object"},
            ],
            "migration_required": False,
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector("#sessionRepair:has-text('2 条无法识别的条目')")
        repair = self.page.locator("#sessionRepair").inner_text()
        self.assertIn("第 2 条（bad）：UMO 格式不完整", repair)
        self.assertIn("第 3 条：不是对象", repair)
        self.assertIn(
            "包含无效条目",
            self.page.locator("#sessionMessage").inner_text(),
        )
        # 无效条目不会进入可编辑草稿，列表只展示可用条目。
        self.assertEqual(
            self.page.locator("#sessionList .list-row").count(), 1
        )
        self.page.click("#clearSessionsUnrestricted")
        self.wait_saves("sessions", 1)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(self.server.state["session"]["sessions"], [])
        self.assertEqual(self.console_errors, [])

    def test_session_switch_save_preserves_unset_policy(self):
        """总开关保存只提交放行策略：未配置的 sessions 不能在刷新后
        被草稿里的 [] 洗成“明确不限制”，诊断和修复入口必须保留。"""
        session = self.server.state["session"]
        session.update({
            "sessions": [],
            "config_error": "sessions_unset",
            "sessions_raw_type": "NoneType",
            "invalid_sessions": [],
            "migration_required": False,
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector("#sessionRepair:has-text('null（尚未配置）')")

        self.page.uncheck("#allowPrivate")
        self.wait_saves("sessions", 1)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertNotIn("sessions", payload)
        self.assertFalse(payload["allow_private"])
        self.assertFalse(self.server.state["session"]["allow_private"])
        self.assertEqual(
            self.server.state["session"]["config_error"], "sessions_unset"
        )
        self.assertEqual(self.server.state["session"]["sessions"], [])
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        # 保存成功提示不能盖掉修复入口：诊断仍在修复卡片里。
        self.assertIn(
            "null（尚未配置）", self.page.locator("#sessionRepair").inner_text()
        )

        # 刷新后仍显示诊断，而不是被“已保存”覆盖成不限制。
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector("#sessionRepair:has-text('null（尚未配置）')")
        self.assertIn(
            "普通用户已拒绝",
            self.page.locator("#sessionMessage").inner_text(),
        )

        # 只有明确的“清空并保存（不限制）”才提交 sessions=[]。
        self.page.click("#clearSessionsUnrestricted")
        self.wait_saves("sessions", 2)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(payload["sessions"], [])
        self.assertFalse(payload["allow_private"])
        self.assertEqual(self.server.state["session"]["config_error"], "")
        self.assertEqual(self.server.state["session"]["sessions"], [])
        self.page.wait_for_function(
            "!document.getElementById('sessionMessage').textContent.includes('配置无效')"
        )
        self.assertNotIn("配置无效", self.page.locator("#sessionMessage").inner_text())
        self.assertEqual(self.console_errors, [])

    def test_session_switch_save_preserves_invalid_list_diagnostics(self):
        """无效列表下切换总开关：不得提交被过滤后的草稿（空/部分列表），
        也不能清掉逐条诊断；管理员仍能通过同一入口修复。"""
        session = self.server.state["session"]
        session.update({
            "sessions": [
                {"umo": "qq:FriendMessage:u1", "connector": "qq", "type": "private", "enabled": True},
            ],
            "config_error": "invalid_session_entries",
            "sessions_raw_type": "list",
            "invalid_sessions": [
                {"index": 1, "umo": "bad", "reason": "umo_malformed"},
                {"index": 2, "umo": "", "reason": "entry_not_object"},
            ],
            "migration_required": False,
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector("#sessionRepair:has-text('2 条无法识别的条目')")

        self.page.uncheck("#allowGroup")
        self.wait_saves("sessions", 1)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertNotIn("sessions", payload)
        self.assertFalse(payload["allow_group"])
        self.assertEqual(
            self.server.state["session"]["config_error"],
            "invalid_session_entries",
        )
        self.assertEqual(len(self.server.state["session"]["sessions"]), 1)
        self.page.wait_for_selector("#sessionMessage:has-text('会话配置已保存')")
        repair = self.page.locator("#sessionRepair").inner_text()
        self.assertIn("第 2 条（bad）：UMO 格式不完整", repair)
        self.assertIn("第 3 条：不是对象", repair)
        self.assertFalse(self.page.is_checked("#allowGroup"))
        self.assertEqual(self.console_errors, [])

    def test_session_repair_non_list_state_is_visible_and_repairable(self):
        """P1：sessions 为非数组时状态页不能崩，必须显示实际类型并允许
        管理员用“清空并保存（不限制）”恢复。"""
        session = self.server.state["session"]
        session.update({
            "sessions": [],
            "config_error": "invalid_session_list",
            "sessions_raw_type": "int",
            "invalid_sessions": [],
            "migration_required": False,
        })
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("session")
        self.page.wait_for_selector("#sessionRepair:has-text('实际值是 int')")
        self.assertIn(
            "不是数组", self.page.locator("#sessionRepair").inner_text()
        )

        self.page.click("#clearSessionsUnrestricted")
        self.wait_saves("sessions", 1)
        payload = self.server.saves("sessions")[-1]["data"]["data"]
        self.assertEqual(payload["sessions"], [])
        self.assertEqual(self.server.state["session"]["config_error"], "")
        self.assertEqual(self.server.state["session"]["sessions"], [])
        self.page.wait_for_function(
            "!document.getElementById('sessionMessage').textContent.includes('配置无效')"
        )
        self.assertEqual(self.console_errors, [])

    def test_narrow_viewport_and_console_clean(self):
        self.page.set_viewport_size({"width": 430, "height": 900})
        self.go("model")
        self.page.fill("#modelInput", "窄屏模型")
        self.trigger_save("model")
        self.page.wait_for_selector("#modelMessage:has-text('已保存')")
        overflow = self.page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        self.assertLessEqual(overflow, 1)
        self.go("session")
        self.assertTrue(self.page.is_visible("#allowPrivate"))
        self.assertEqual(self.console_errors, [])

    def test_search_page_sections_mobile_nav_and_touch_targets(self):
        # 审查回归：390px 下导航不能靠横滚藏内容，排序按钮至少 44px，
        # 搜索页按「策略、诊断、高级参数」组织，保存入口在首屏可见。
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.go("search")
        self.assertTrue(self.page.is_visible("#searchPolicyCard"))
        self.assertTrue(self.page.is_visible("#searchDiagCard"))
        self.assertTrue(self.page.is_visible("#searchAdvancedCard"))
        heads = self.page.eval_on_selector_all(
            "#searchPolicyCard h2, #searchDiagCard h2, #searchAdvancedCard h2",
            "els => els.map(e => e.textContent.trim())",
        )
        self.assertEqual(heads, ["搜索策略", "诊断", "高级参数"])

        nav = self.page.evaluate(
            """() => {
              const nav = document.querySelector('.nav');
              const rects = [...nav.querySelectorAll('button')].map(b => {
                const r = b.getBoundingClientRect();
                return {left: r.left, right: r.right, height: r.height};
              });
              return {
                overflow: nav.scrollWidth - nav.clientWidth,
                count: rects.length,
                minHeight: Math.min(...rects.map(r => r.height)),
                maxRight: Math.max(...rects.map(r => r.right)),
                innerWidth: window.innerWidth,
              };
            }"""
        )
        self.assertEqual(nav["count"], 5)
        self.assertLessEqual(nav["overflow"], 2)
        self.assertLessEqual(nav["maxRight"], nav["innerWidth"] + 1)
        self.assertGreaterEqual(nav["minHeight"], 44)

        sizes = self.page.eval_on_selector_all(
            ".order-btn",
            "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.width, r.height]; })",
        )
        self.assertTrue(sizes)
        for width, height in sizes:
            self.assertGreaterEqual(width, 44)
            self.assertGreaterEqual(height, 44)

        self.assertEqual(self.page.locator("#saveSearchTop, #saveSearch").count(), 0)
        overflow = self.page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        self.assertLessEqual(overflow, 1)
        self.assertEqual(self.console_errors, [])

    def test_search_page_autosaves_with_toast(self):
        # 修改后自动保存，顶部出现 role=status 的轻提示；不再有独立保存按钮。
        self.go("search")
        self.assertEqual(self.page.locator("#saveSearch, #saveSearchTop").count(), 0)
        self.page.fill("#searchResults", "5")
        self.wait_saves("search", 1)
        self.page.wait_for_selector("#toast:has-text('已自动保存')")
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["result_count"], 5)
        self.assertEqual(payload["material_max_chars"], 3000)
        self.assertEqual(payload["material_max_items"], 6)
        self.assertEqual(payload["material_snippet_chars"], 400)
        self.assertFalse(payload["summarize_enabled"])
        self.assertEqual(self.console_errors, [])

    def test_search_group_order_and_rate_slider_autosave(self):
        # 审查回归：只勾选/排序搜索组、只拖频率滑杆也必须触发自动保存。
        self.go("search")
        before = len(self.server.saves("search"))
        self.page.uncheck('[data-mode-check="api"]')
        self.wait_saves("search", before + 1)
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertNotIn("api", payload["mode_enabled"])

        self.page.click('[data-mode-move="web"][data-direction="-1"]')
        self.wait_saves("search", before + 2)
        payload = self.server.saves("search")[-1]["data"]["data"]
        order = payload["mode_order"]
        self.assertLess(order.index("web"), order.index("api"))

        self.go("rate")
        before_rate = len(self.server.saves("rate"))
        self.page.eval_on_selector(
            "#windowRange",
            """el => {
              el.value = "15";
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
        )
        self.wait_saves("rate", before_rate + 1)
        self.assertEqual(self.server.state["rate"]["window_minutes"], 15)

    def test_source_switch_and_noop_blur_autosave(self):
        # 切换模型来源要保存；未改动字段仅失焦不应保存或提示。
        self.go("search")
        before_search = len(self.server.saves("search"))
        self.page.press("#searchResults", "Tab")
        self.page.wait_for_timeout(400)
        self.assertEqual(len(self.server.saves("search")), before_search)

        self.go("model")
        before_model = len(self.server.saves("model"))
        self.page.click("#sourceManual")
        self.wait_saves("model", before_model + 1)
        self.assertEqual(
            self.server.saves("model")[-1]["data"]["data"]["source"], "manual"
        )

    def test_empty_rate_input_does_not_save_zero(self):
        self.go("rate")
        before = len(self.server.saves("rate"))
        self.page.fill("#windowNumber", "")
        self.page.press("#windowNumber", "Tab")
        self.page.wait_for_timeout(500)
        self.assertEqual(len(self.server.saves("rate")), before)
        self.assertNotEqual(self.server.state["rate"]["window_minutes"], 0)

    def test_queued_save_aborts_after_conflict_response(self):
        # 审查回归：冲突响应返回前已排队的写入，在真正发请求前必须再查基线，
        # 不能把另一页刚保存的每日上限 77 覆盖回旧快照的 20。
        page_b = self.context.new_page()
        page_b.on("pageerror", lambda error: self.console_errors.append(str(error)))
        page_b.goto(self.server.url)
        page_b.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.pause_autosave()
        self.pause_autosave(page_b)

        self.go("rate")
        self.page.fill("#dailyNumber", "77")
        self.page.evaluate(
            "() => document.getElementById('dailyNumber')"
            ".dispatchEvent(new Event('change', { bubbles: true }))"
        )
        self.wait_saves("rate", 1)
        self.assertEqual(self.server.state["rate"]["daily_max"], 77)

        # B 的搜索保存被闸门卡住，冲突响应尚未返回。
        gate = self.server.hold_next("config-center/save", "search")
        page_b.click('nav button[data-page="search"]')
        page_b.fill("#searchResults", "5")
        page_b.evaluate(
            """() => {
              window.__dshConfig.resetSignature('search');
              window.__dshConfig.saveSectionNow('search');
              return 'queued';
            }"""
        )
        self.assertTrue(gate["entered"].wait(5), "搜索保存未到达模拟桥")

        # 冲突返回前，频率写入已经排在队列里（每日上限仍是旧快照的 20）。
        page_b.click('nav button[data-page="rate"]')
        page_b.fill("#windowNumber", "11")
        page_b.evaluate(
            """() => {
              window.__dshConfig.resetSignature('rate');
              window.__dshConfig.saveSectionNow('rate');
              return 'queued';
            }"""
        )
        page_b.wait_for_timeout(150)
        rate_saves_before = len(self.server.saves("rate"))

        gate["release"].set()
        page_b.wait_for_function(
            "document.getElementById('searchSaveMessage').textContent"
            ".includes('其他页面更新')"
        )
        page_b.wait_for_selector("#rateMessage:has-text('取消这次排队保存')")
        page_b.wait_for_timeout(300)
        self.assertEqual(len(self.server.saves("rate")), rate_saves_before)
        self.assertEqual(self.server.state["rate"]["daily_max"], 77)
        self.assertEqual(self.server.state["rate"]["window_minutes"], 10)
        # 草稿保留，等刷新后由用户确认。
        self.assertEqual(page_b.input_value("#windowNumber"), "11")
        self.assertEqual(page_b.input_value("#dailyNumber"), "20")
        page_b.close()

    def test_conflict_pauses_all_sections_until_refresh(self):
        # 审查回归：A 页保存搜索 7 和每日上限 77；B 页在搜索分区冲突后
        # 再动频率，旧表单快照不能把每日上限覆盖回 20。
        page_b = self.context.new_page()
        page_b.on("pageerror", lambda error: self.console_errors.append(str(error)))
        page_b.goto(self.server.url)
        page_b.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.pause_autosave()

        self.go("search")
        self.page.fill("#searchResults", "7")
        self.trigger_save("search")
        self.wait_saves("search", 1)
        self.assertEqual(self.server.state["search"]["result_count"], 7)

        self.go("rate")
        self.page.fill("#dailyNumber", "77")
        self.page.evaluate(
            "() => document.getElementById('dailyNumber')"
            ".dispatchEvent(new Event('change', { bubbles: true }))"
        )
        self.wait_saves("rate", 1)
        self.assertEqual(self.server.state["rate"]["daily_max"], 77)

        # B 页仍基于旧 revision：只在搜索分区制造冲突。
        page_b.click('nav button[data-page="search"]')
        page_b.fill("#searchResults", "5")
        page_b.wait_for_selector("#searchSaveMessage:has-text('其他页面更新')")
        self.assertEqual(self.server.state["search"]["result_count"], 7)

        # 冲突后编辑另一个分区：自动保存必须整体暂停，不能覆盖 77。
        page_b.click('nav button[data-page="rate"]')
        rate_saves_before = len(self.server.saves("rate"))
        page_b.fill("#dailyNumber", "99")
        page_b.evaluate(
            "() => document.getElementById('dailyNumber')"
            ".dispatchEvent(new Event('change', { bubbles: true }))"
        )
        page_b.wait_for_timeout(900)
        self.assertEqual(len(self.server.saves("rate")), rate_saves_before)
        self.assertEqual(self.server.state["rate"]["daily_max"], 77)
        self.assertIn(
            "自动保存已暂停",
            page_b.inner_text("#globalNotice"),
        )

        # 刷新拿到新基线后才恢复自动保存，并保留服务端的 77。
        page_b.click('nav button[data-page="status"]')
        page_b.click("#refreshAll")
        page_b.wait_for_selector("#confirmLayer:not(.hidden)")
        page_b.click("#confirmOk")
        page_b.wait_for_function(
            "document.getElementById('dailyNumber').value === '77'"
        )
        page_b.click('nav button[data-page="rate"]')
        page_b.fill("#windowNumber", "12")
        page_b.evaluate(
            "() => document.getElementById('windowNumber')"
            ".dispatchEvent(new Event('change', { bubbles: true }))"
        )
        self.wait_saves("rate", rate_saves_before + 1)
        self.assertEqual(self.server.state["rate"]["daily_max"], 77)
        self.assertEqual(self.server.state["rate"]["window_minutes"], 12)
        page_b.close()

    def test_blank_per_backend_timeout_keeps_saved_value(self):
        # 已保存 4321 被临时清空时不能落盘成 0；主动输入 0 才写 0。
        self.server.state["search"]["per_backend_timeout_ms"] = 4321
        self.page.reload()
        self.page.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        self.go("search")
        self.assertEqual(self.page.input_value("#searchPerTimeout"), "4321")
        before = len(self.server.saves("search"))
        self.page.fill("#searchPerTimeout", "")
        self.page.wait_for_timeout(900)
        self.assertEqual(len(self.server.saves("search")), before)
        self.assertEqual(
            self.server.state["search"]["per_backend_timeout_ms"], 4321
        )
        # 失焦把空白恢复成保留值，而不是显示成已被清空。
        self.page.press("#searchPerTimeout", "Tab")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.input_value("#searchPerTimeout"), "4321")
        self.assertEqual(len(self.server.saves("search")), before)
        # 用户主动输入 0 时按 0 保存（0 表示按总超时，不限制单后端）。
        self.page.fill("#searchPerTimeout", "0")
        self.wait_saves("search", before + 1)
        self.assertEqual(
            self.server.state["search"]["per_backend_timeout_ms"], 0
        )

    def test_focus_survives_save_receipt_after_keyboard_reorder(self):
        # 键盘排序后焦点回到按钮；保存回执整块重渲染后也不能掉到 BODY。
        self.go("search")
        button = self.page.locator(
            ".mode-row .order-btn[data-direction='1']"
        ).first
        key = button.get_attribute("data-mode-move")
        button.focus()
        self.page.keyboard.press("Enter")
        self.wait_saves("search", 1)
        self.page.wait_for_function(
            """(key) => {
              const el = document.activeElement;
              return !!(el && el.dataset
                && el.dataset.modeMove === key
                && el.dataset.direction === "1");
            }""",
            arg=key,
        )
        self.assertEqual(self.console_errors, [])

    def test_conflict_blocks_stale_overwrite_until_refresh(self):
        # 审查回归：冲突后保留草稿但停止自动写入，不能把别的页面刚存的值覆盖回去。
        page_b = self.context.new_page()
        page_b.on("pageerror", lambda error: self.console_errors.append(str(error)))
        page_b.goto(self.server.url)
        page_b.wait_for_function(
            "document.getElementById('sumModel').textContent !== '加载中…'"
        )
        page_b.evaluate("() => window.__dshConfig.setAutosavePaused(true)")
        self.pause_autosave()

        self.go("search")
        self.page.fill("#searchResults", "7")
        self.trigger_save("search")
        self.wait_saves("search", 1)
        self.assertEqual(self.server.state["search"]["result_count"], 7)

        page_b.click('nav button[data-page="search"]')
        page_b.fill("#searchResults", "5")
        page_b.evaluate(
            """() => {
              document.getElementById('searchResults')
                .dispatchEvent(new Event('input', { bubbles: true }));
              window.__dshConfig.resetSignature('search');
              return window.__dshConfig.saveSectionNow('search');
            }"""
        )
        page_b.wait_for_selector("#searchSaveMessage:has-text('其他页面更新')")
        self.assertEqual(self.server.state["search"]["result_count"], 7)

        # 冲突后再次编辑：保留草稿，但不得自动写入覆盖 7。
        page_b.fill("#searchResults", "4")
        page_b.evaluate(
            """() => {
              document.getElementById('searchResults')
                .dispatchEvent(new Event('input', { bubbles: true }));
              window.__dshConfig.resetSignature('search');
              return window.__dshConfig.saveSectionNow('search');
            }"""
        )
        page_b.wait_for_timeout(400)
        self.assertEqual(self.server.state["search"]["result_count"], 7)
        self.assertEqual(page_b.input_value("#searchResults"), "4")
        page_b.close()

    def test_material_params_autosave(self):
        # 素材预算等高级参数改动后自动保存，载荷带新值。
        self.go("search")
        self.page.fill("#materialMaxChars", "1800")
        self.wait_saves("search", 1)
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertEqual(payload["material_max_chars"], 1800)
        self.assertEqual(self.console_errors, [])

    def test_master_toggle_off_autosaves_zero_call_state(self):
        # 关闭总开关立即自动保存；页面不出现「当前生效」概念。
        self.go("search")
        self.page.uncheck("#searchEnabled")
        self.wait_saves("search", 1)
        payload = self.server.saves("search")[-1]["data"]["data"]
        self.assertFalse(payload["enabled"])
        self.assertNotIn("当前生效", self.page.locator("#page-search").inner_text())
        self.assertEqual(self.mode_state(), "关闭")
        self.assertEqual(self.console_errors, [])

    def test_search_page_sort_keyboard_and_dark_theme(self):
        # 键盘可排序（Enter 触发），暗色主题下页面可读且不横向溢出。
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.go("search")
        first = self.page.eval_on_selector_all(
            ".mode-row .candidate-main strong",
            "els => els.map(e => e.textContent.trim())",
        )
        self.assertGreaterEqual(len(first), 2)
        down = self.page.locator(".mode-row .order-btn[data-direction='1']").first
        down.focus()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function(
            "document.getElementById('searchDraftHint').textContent.includes('有未保存修改')"
        )
        after = self.page.eval_on_selector_all(
            ".mode-row .candidate-main strong",
            "els => els.map(e => e.textContent.trim())",
        )
        self.assertNotEqual(first, after)

        self.page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
        self.assertEqual(
            self.page.evaluate("document.documentElement.getAttribute('data-theme')"),
            "dark",
        )
        self.assertTrue(self.page.is_visible("#searchPolicyCard"))
        overflow = self.page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        self.assertLessEqual(overflow, 1)
        self.assertEqual(self.console_errors, [])

    def test_search_sort_keeps_focus_and_labels_draft_preview(self):
        self.go("search")
        button = self.page.locator(
            ".mode-row .order-btn[data-direction='1']"
        ).first
        key = button.get_attribute("data-mode-move")
        button.focus()
        self.page.keyboard.press("Enter")
        active = self.page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el || !el.dataset) return null;
              return {move: el.dataset.modeMove || "", direction: el.dataset.direction || ""};
            }"""
        )
        self.assertIsNotNone(active)
        self.assertEqual(active["move"], key)
        self.assertIn(active["direction"], ("1", "-1"))
        # 已移除「当前生效/草稿预览」概念：行徽标只表示勾选状态。
        self.assertNotIn("当前生效", self.page.locator("#page-search").inner_text())
        self.assertNotIn("草稿预览", self.page.locator("#page-search").inner_text())
        badges = self.page.eval_on_selector_all(
            ".mode-row .status-badge",
            "els => els.map(e => e.textContent.trim())",
        )
        self.assertTrue(all(b in ("已勾选", "未启用") for b in badges), badges)
        self.assertEqual(self.console_errors, [])

    def test_confirm_cancel_returns_focus_and_search_test_shows_attempts(self):
        self.pause_autosave()
        self.go("search")
        self.page.fill("#searchResults", "4")
        self.page.dispatch_event("#searchResults", "input")
        # 主动测试必须立刻显示本次尝试与跳过原因。
        self.page.click("#searchTest")
        self.page.wait_for_selector("#searchTestResult:has-text('尝试 api')")
        detail = self.page.inner_text("#searchTestResult")
        self.assertIn("跳过 astrbot", detail)
        self.assertIn("anysearch_plugin_not_installed", detail)

        # 有未保存草稿时刷新会弹确认框；取消后焦点回到触发按钮。
        self.go("status")
        self.page.click("#refreshAll")
        self.page.wait_for_selector("#confirmLayer:not(.hidden)")
        self.page.click("#confirmCancel")
        self.page.wait_for_function(
            "document.getElementById('confirmLayer').classList.contains('hidden')"
        )
        active_id = self.page.evaluate(
            "() => document.activeElement ? document.activeElement.id : ''"
        )
        self.assertEqual(active_id, "refreshAll")
        self.assertEqual(self.console_errors, [])
