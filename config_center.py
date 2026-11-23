"""统一配置中心的数据归一化、迁移、会话/管理员/身份策略。"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from .whitelist import migration_required, validate_umo_list

ADMIN_USERS = "admin_users"
SESSIONS = "session_settings"
IDENTITY_LINKS = "identity_links"


def parse_umo(umo: str) -> tuple[str, str]:
    text = str(umo or "").strip()
    parts = text.split(":", 2)
    connector = parts[0] if parts and parts[0] else ""
    raw_type = parts[1].lower() if len(parts) > 1 else ""
    if raw_type in ("groupmessage", "group", "group_message"):
        chat_type = "group"
    elif raw_type in ("friendmessage", "private", "friend", "direct"):
        chat_type = "private"
    else:
        chat_type = "unknown"
    return connector, chat_type


def _safe_list(value):
    return list(value) if isinstance(value, (list, tuple, set)) else []


def session_entry_error(item) -> str:
    """按配置中心保存端同一套规则诊断单个会话条目。

    返回空串表示条目可用；否则返回机器码，供状态接口与写路径展示。
    读路径必须和写路径一致，不能把保存端会拒绝的条目显示成“已启用”。
    """
    if not isinstance(item, dict):
        return "entry_not_object"
    umo = str(item.get("umo") or "").strip()
    if not umo:
        return "umo_missing"
    # 与 AstrBot 宿主 core/umo_alias.parse_umo 保持同一口径：按 ":" 最多切
    # 三段，第三段是完整 session_id，允许其中再含冒号（qq:FriendMessage::b
    # 的 session_id 是 ":b"，qq:FriendMessage:a:b 是 "a:b"）。只有整个
    # session_id 为空（qq:FriendMessage:）才算缺少会话标识，不能按“段数”
    # 判断，否则会误拒宿主能正常往返的合法会话 ID。
    parts = umo.split(":", 2)
    if len(parts) < 3:
        return "umo_malformed"
    platform, message_type, session_id = parts
    if not platform.strip() or not message_type.strip() or not session_id:
        return "umo_malformed"
    connector, chat_type = parse_umo(umo)
    if not connector or chat_type == "unknown":
        return "umo_unknown_type"
    return ""


class ConfigCenter:
    def __init__(self, plugin):
        self.plugin = plugin
        self.config = plugin.config
        self.config_error = ""
        # 会话条目级诊断（index/umo/reason），状态接口据此让管理员定位并修复。
        self.invalid_sessions: list[dict] = []
        self.normalize()

    # ---------- normalization / migration ----------
    def normalize(self) -> None:
        config = self.config
        if "whitelist_umos" in config:
            error = validate_umo_list(config.get("whitelist_umos"))
            if error:
                self.config_error = error
        # 旧 admin_qqs 不再自动变成管理员：没有可靠连接器作用域时，
        # 静默授予所有连接器权限是不安全的。迁移模块只把它们放进
        # legacy_admin_pending，等待用户明确选择连接器后重新添加。
        admin_users = config.get(ADMIN_USERS)
        if not isinstance(admin_users, list):
            admin_users = []
        normalized = []
        for item in admin_users:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("uid") or "").strip()
            if not uid:
                continue
            normalized.append({
                "connector": str(item.get("connector") or "").strip(),
                "uid": uid,
                "legacy": bool(item.get("legacy", False)),
            })
        config[ADMIN_USERS] = normalized
        if not isinstance(config.get("legacy_admin_pending"), list):
            config["legacy_admin_pending"] = []
        if not isinstance(config.get("legacy_umo_pending"), list):
            config["legacy_umo_pending"] = []
        config.setdefault("legacy_restrictions_pending", False)

        settings = config.get(SESSIONS)
        if not isinstance(settings, dict):
            if SESSIONS in config:
                # 字段存在但不是对象：不能当成“未配置”再伪造默认值放行。
                # 保留原始值作为会话列表内容，交给下面的诊断统一拒绝。
                settings = {
                    "allow_private": True,
                    "allow_group": True,
                    "sessions": settings,
                }
                if not self.config_error:
                    self.config_error = "invalid_session_settings"
                config[SESSIONS] = settings
            else:
                # 旧配置完全没有 session_settings 字段：只沿用可识别的旧
                # 白名单生成明确列表。没有可迁移条目时保持 null（尚未配置），
                # 不能按 v2 语义写成空列表——那等于把未知状态静默改成“不限制”。
                sessions = []
                for umo in _safe_list(config.get("whitelist_umos")):
                    if not isinstance(umo, str) or not umo.strip():
                        continue
                    if session_entry_error({"umo": umo}):
                        continue
                    connector, chat_type = parse_umo(umo.strip())
                    sessions.append({
                        "umo": umo.strip(),
                        "connector": connector,
                        "type": chat_type,
                        "enabled": True,
                    })
                settings = {
                    "allow_private": True,
                    "allow_group": True,
                    "sessions": sessions if sessions else None,
                }
                if not sessions and not self.config_error:
                    self.config_error = "sessions_unset"
                config[SESSIONS] = settings
        else:
            settings = dict(settings)
            raw_sessions = settings.get("sessions")
            is_list = isinstance(raw_sessions, list)
            settings["allow_private"] = bool(settings.get("allow_private", True))
            settings["allow_group"] = bool(settings.get("allow_group", True))
            sessions = []
            invalid_sessions = []
            session_error = ""
            if not is_list:
                # null（schema 默认）表示“尚未配置”，其他类型表示类型错误。
                # 两者都必须拒绝普通会话：真实宿主会先把 null 补成默认值，
                # 若在这里归一化成 []，就等于把“未配置”静默变成“不限制”。
                session_error = (
                    "sessions_unset"
                    if raw_sessions is None
                    else "invalid_session_list"
                )
            else:
                for index, item in enumerate(raw_sessions):
                    reason = session_entry_error(item)
                    if reason:
                        invalid_sessions.append({
                            "index": index,
                            "umo": str(item.get("umo") or "").strip()
                            if isinstance(item, dict) else "",
                            "reason": reason,
                        })
                        continue
                    umo = str(item.get("umo") or "").strip()
                    connector, chat_type = parse_umo(umo)
                    sessions.append({
                        "umo": umo,
                        "connector": connector,
                        "type": chat_type,
                        "enabled": bool(item.get("enabled", True)),
                    })
                # 非空但存在写路径会拒绝的条目：读路径必须同样报错，
                # 不能把无效条目静默丢弃成“部分生效”。
                if raw_sessions and not sessions:
                    session_error = "no_valid_sessions"
                elif invalid_sessions:
                    session_error = "invalid_session_entries"
            self.invalid_sessions = invalid_sessions
            if not self.config_error:
                self.config_error = session_error
            # 出错时保留原始值：否则下一次保存/重启会把非法值洗成
            # 合法的“明确不限”，配置错误随之消失。
            settings["sessions"] = raw_sessions if session_error else sessions
            config[SESSIONS] = settings

        links = config.get(IDENTITY_LINKS)
        if not isinstance(links, list):
            config[IDENTITY_LINKS] = []
        else:
            config[IDENTITY_LINKS] = [
                {
                    "id": str(item.get("id") or uuid.uuid4().hex),
                    "connector": str(item.get("connector") or "").strip(),
                    "uids": [
                        str(uid).strip()
                        for uid in _safe_list(item.get("uids"))
                        if str(uid).strip()
                    ],
                    "created_at": item.get("created_at") or time.time(),
                }
                for item in links if isinstance(item, dict)
            ]
        config.setdefault("config_center_version", 1)

    @property
    def admin_users(self) -> list[dict]:
        return self.config.get(ADMIN_USERS, [])

    @property
    def settings(self) -> dict:
        return self.config.get(SESSIONS, {})

    @property
    def identity_links(self) -> list[dict]:
        return self.config.get(IDENTITY_LINKS, [])

    def migration_required(self) -> bool:
        if self.legacy_umo_pending or self.config.get("legacy_restrictions_pending"):
            return True
        try:
            version = int(self.config.get("config_migration_version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= 1:
            return False
        return migration_required(
            self.config.get("whitelist_umos"),
            self.config.get("whitelist_groups"),
            self.config.get("whitelist_privates"),
        )

    @property
    def legacy_admin_pending(self) -> list[dict]:
        pending = self.config.get("legacy_admin_pending")
        return pending if isinstance(pending, list) else []

    @property
    def legacy_umo_pending(self) -> list[dict]:
        pending = self.config.get("legacy_umo_pending")
        return pending if isinstance(pending, list) else []

    def legacy_admin_present(self) -> bool:
        return bool(self.legacy_admin_pending) or any(
            item.get("legacy") for item in self.admin_users
        )

    # ---------- admin / session ----------
    def is_admin(self, connector: str, uid: str) -> bool:
        uid = str(uid or "")
        connector = str(connector or "")
        for item in self.admin_users:
            if str(item.get("uid")) != uid:
                continue
            scope = str(item.get("connector") or "")
            # 空作用域不是通配符；旧管理员必须迁移到明确连接器后才生效。
            if scope and scope == connector:
                return True
        return False

    def is_session_allowed(self, umo: str) -> bool:
        if self.config_error:
            return False
        connector, chat_type = parse_umo(umo)
        if chat_type == "private" and not self.settings.get("allow_private", True):
            return False
        if chat_type == "group" and not self.settings.get("allow_group", True):
            return False
        sessions = self.settings.get("sessions", [])
        if self.migration_required():
            # 待迁移期间仍可放行已经精确映射好的旧 UMO 会话；空列表或
            # 无法映射的旧群/私聊限制一律安全关闭。
            if not sessions:
                return False
            return any(
                item.get("enabled", True) and item.get("umo") == umo
                for item in sessions
            )
        if not sessions:
            return True
        return any(
            item.get("enabled", True) and item.get("umo") == umo
            for item in sessions
        )

    def add_admin(self, connector: str, uid: str) -> None:
        connector = str(connector or "").strip()
        uid = str(uid or "").strip()
        if not connector:
            raise ValueError("管理员必须指定连接器")
        if not uid:
            raise ValueError("UID 不能为空")
        # UID 不是 UMO；禁止把包含冒号的完整会话标识当人员身份。
        if ":" in uid:
            raise ValueError("UID 不能是 UMO，请填写 /sid 返回的发送者 UID")
        if not any(
            item.get("connector") == connector and item.get("uid") == uid
            for item in self.admin_users
        ):
            self.admin_users.append({
                "connector": connector,
                "uid": uid,
                "legacy": False,
            })

    def remove_admin(self, connector: str, uid: str) -> None:
        self.config[ADMIN_USERS] = [
            item for item in self.admin_users
            if not (str(item.get("connector") or "") == str(connector or "")
                    and str(item.get("uid")) == str(uid))
        ]

    def add_session(self, umo: str) -> None:
        umo = str(umo or "").strip()
        if not umo or umo.count(":") < 2:
            raise ValueError("请输入 /sid 返回的完整 UMO（platform:type:session）")
        connector, chat_type = parse_umo(umo)
        if not connector or chat_type == "unknown":
            raise ValueError("无法从 UMO 识别连接器或会话类型")
        sessions = self.settings.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
            self.settings["sessions"] = sessions
        for item in sessions:
            if not isinstance(item, dict):
                continue
            if item.get("umo") == umo:
                item["enabled"] = True
                item["connector"] = connector
                item["type"] = chat_type
                return
        sessions.append({
            "umo": umo,
            "connector": connector,
            "type": chat_type,
            "enabled": True,
        })

    def remove_session(self, umo: str) -> None:
        sessions = self.settings.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        self.settings["sessions"] = [
            item for item in sessions
            if isinstance(item, dict) and item.get("umo") != umo
        ]

    def toggle_session(self, umo: str, enabled: bool) -> None:
        for item in self.settings.get("sessions", []):
            if not isinstance(item, dict):
                continue
            if item.get("umo") == umo:
                item["enabled"] = bool(enabled)
                return
        raise ValueError("会话不存在")

    # ---------- identity ----------
    def identity_key(self, connector: str, uid: str) -> str:
        connector = str(connector or "")
        uid = str(uid or "")
        for link in self.identity_links:
            if link.get("connector") != connector:
                continue
            if uid in link.get("uids", []):
                return f"link:{link.get('id')}"
        return f"{connector}:{uid}"

    def link_identities(self, connector: str, uids: list[str]) -> dict:
        connector = str(connector or "").strip()
        clean = sorted({str(uid).strip() for uid in uids if str(uid).strip()})
        if not connector:
            raise ValueError("关联身份必须指定连接器")
        if len(clean) < 2:
            raise ValueError("至少选择两个身份")
        # 同一连接器内合并已有 link，跨连接器绝不自动合并。
        target_id = uuid.uuid4().hex
        remaining = []
        merged = set(clean)
        for link in self.identity_links:
            if link.get("connector") != connector:
                remaining.append(link)
                continue
            if set(link.get("uids", [])) & merged:
                merged.update(link.get("uids", []))
            else:
                remaining.append(link)
        remaining.append({
            "id": target_id,
            "connector": connector,
            "uids": sorted(merged),
            "created_at": time.time(),
        })
        self.config[IDENTITY_LINKS] = remaining
        return remaining[-1]

    def unlink_identity(self, link_id: str) -> None:
        self.config[IDENTITY_LINKS] = [
            link for link in self.identity_links
            if link.get("id") != link_id
        ]

    def known_identities(self, stats_events=None) -> list[dict]:
        seen = {}
        for link in self.identity_links:
            for uid in link.get("uids", []):
                key = f"{link.get('connector')}:{uid}"
                seen[key] = {
                    "connector": link.get("connector"),
                    "uid": uid,
                    "linked": link.get("id"),
                }
        for event in stats_events or []:
            user = event.get("user") if isinstance(event, dict) else None
            if not isinstance(user, dict):
                continue
            connector = str(user.get("connector") or "")
            uid = str(user.get("uid") or "")
            if not connector or not uid:
                continue
            key = f"{connector}:{uid}"
            seen.setdefault(key, {
                "connector": connector,
                "uid": uid,
                "linked": self.identity_key(connector, uid).removeprefix("link:")
                if self.identity_key(connector, uid).startswith("link:")
                else "",
            })
        return sorted(seen.values(), key=lambda x: (x["connector"], x["uid"]))
