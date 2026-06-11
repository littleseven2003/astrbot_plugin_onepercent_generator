"""
黑名单管理模块
管理群聊/私聊的功能启用状态
"""

# KV 存储键
BLACKLIST_KEY = "blacklist"


def _default_blacklist() -> dict:
    """默认空黑名单"""
    return {"groups": [], "privates": []}


async def get_blacklist(kv_storage) -> dict:
    """
    获取当前黑名单

    Returns:
        {"groups": [str], "privates": [str]}
    """
    data = await kv_storage.get_kv_data(BLACKLIST_KEY)
    if data is None:
        return _default_blacklist()
    if isinstance(data, str):
        import json
        data = json.loads(data)
    # 确保结构完整
    if "groups" not in data:
        data["groups"] = []
    if "privates" not in data:
        data["privates"] = []
    return data


async def save_blacklist(kv_storage, blacklist: dict):
    """保存黑名单"""
    import json
    await kv_storage.put_kv_data(BLACKLIST_KEY, json.dumps(blacklist))


async def is_enabled(session_id: str, session_type: str, default_enabled: bool, kv_storage) -> bool:
    """
    检查当前会话是否启用

    Args:
        session_id: 会话 ID（群号或 QQ 号）
        session_type: "group" 或 "private"
        default_enabled: 默认启用状态
        kv_storage: KV 存储对象

    Returns:
        是否启用
    """
    blacklist = await get_blacklist(kv_storage)

    if session_type == "group":
        is_blocked = session_id in blacklist.get("groups", [])
    else:
        is_blocked = session_id in blacklist.get("privates", [])

    # 默认启用 → 在黑名单中则禁用
    # 默认禁用 → 不在黑名单中则仍禁用（即白名单模式）
    if default_enabled:
        return not is_blocked
    else:
        return False


async def enable_session(session_id: str, session_type: str, kv_storage) -> str:
    """
    启用会话功能（从黑名单移除）

    Returns:
        操作结果消息
    """
    blacklist = await get_blacklist(kv_storage)

    if session_type == "group":
        if session_id in blacklist["groups"]:
            blacklist["groups"].remove(session_id)
            await save_blacklist(kv_storage, blacklist)
            return f"✅ 已为群聊 {session_id} 开启小作文功能"
        else:
            return f"✅ 群聊 {session_id} 的小作文功能已经是开启状态"
    else:
        if session_id in blacklist["privates"]:
            blacklist["privates"].remove(session_id)
            await save_blacklist(kv_storage, blacklist)
            return f"✅ 已为私聊 {session_id} 开启小作文功能"
        else:
            return f"✅ 私聊 {session_id} 的小作文功能已经是开启状态"


async def disable_session(session_id: str, session_type: str, kv_storage) -> str:
    """
    禁用会话功能（加入黑名单）

    Returns:
        操作结果消息
    """
    blacklist = await get_blacklist(kv_storage)

    if session_type == "group":
        if session_id not in blacklist["groups"]:
            blacklist["groups"].append(session_id)
            await save_blacklist(kv_storage, blacklist)
            return f"🚫 已为群聊 {session_id} 关闭小作文功能"
        else:
            return f"🚫 群聊 {session_id} 的小作文功能已经是关闭状态"
    else:
        if session_id not in blacklist["privates"]:
            blacklist["privates"].append(session_id)
            await save_blacklist(kv_storage, blacklist)
            return f"🚫 已为私聊 {session_id} 关闭小作文功能"
        else:
            return f"🚫 私聊 {session_id} 的小作文功能已经是关闭状态"


async def add_to_blacklist(target_id: str, target_type: str, kv_storage) -> str:
    """WebUI 用：添加到黑名单"""
    blacklist = await get_blacklist(kv_storage)
    key = "groups" if target_type == "group" else "privates"
    if target_id not in blacklist[key]:
        blacklist[key].append(target_id)
        await save_blacklist(kv_storage, blacklist)
        return "添加成功"
    return "已在黑名单中"


async def remove_from_blacklist(target_id: str, target_type: str, kv_storage) -> str:
    """WebUI 用：从黑名单移除"""
    blacklist = await get_blacklist(kv_storage)
    key = "groups" if target_type == "group" else "privates"
    if target_id in blacklist[key]:
        blacklist[key].remove(target_id)
        await save_blacklist(kv_storage, blacklist)
        return "移除成功"
    return "不在黑名单中"
