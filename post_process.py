"""
AI 响应后处理模块。

负责清理模型误输出的标题/游戏名，并把结构化正文、活动标题、
活动说明链接和发布前补充提示拼装成用户可复制的消息。
"""

import re

ACTIVITY_INTRO = "什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390"

FIELD_ORDER = (
    "发售平台",
    "游玩时间",
    "推荐人群及理由",
    "游戏介绍",
    "故事/感想",
)
_PLACEHOLDER_VALUES = frozenset({
    "未提供",
    "资料未提供",
    "待补充",
    "用户未提供",
    "用户未填写",
    "暂无",
    "无",
    "未知",
    "不详",
    "n/a",
    "na",
    "none",
    "null",
})
_FIELD_RE = re.compile(r"^\s*([^：:]{1,24})\s*[：:]\s*(.*)$")


def parse_ai_response(text: str) -> dict:
    """
    解析 AI 返回内容，尝试提取标题
    与原项目 aiService.js 的 parseAIResponse 逻辑一致

    Args:
        text: AI 返回的原始文本

    Returns:
        {"title": str, "content": str}
    """
    title_match = re.match(r'^【我的百分之一】\+【(.+?)】', text)
    title = title_match.group(1).strip() if title_match else ''

    body = text[title_match.end():].strip() if title_match else text.strip()

    # 去除开头的"正文："前缀
    body = re.sub(r'^正文：\s*', '', body).strip()

    return {"title": title, "content": body}


def strip_generated_title_and_game_name(text: str) -> str:
    """
    清洗 AI 误输出的标题、游戏名称行等
    与原项目 promptService.js 的 stripGeneratedTitleAndGameName 逻辑一致

    Args:
        text: AI 返回内容

    Returns:
        清洗后的文本
    """
    if not text:
        return ''
    result = text
    result = re.sub(r'^标题：.*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'^【我的百分之一】\+【.*】.*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'^游戏名称：.*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'^正文：\s*', '', result, flags=re.MULTILINE)
    result = re.sub(
        r'^.*什么是【我的百分之一】.*$', '', result, flags=re.MULTILINE
    )
    result = re.sub(
        r'^.*taptap\.cn/moment/371075389700702390.*$',
        '',
        result,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return result.strip()


def _canonical_field(label: str) -> str:
    label = label.strip()
    if "发售平台" in label:
        return "发售平台"
    if any(
        key in label
        for key in ("游玩时间", "玩游戏的时间", "游戏时间", "游玩时长")
    ):
        return "游玩时间"
    if "推荐" in label:
        return "推荐人群及理由"
    if "游戏介绍" in label:
        return "游戏介绍"
    if any(key in label for key in ("故事", "感想", "心得")):
        return "故事/感想"
    return label


def _parse_field_line(line: str) -> tuple[str, str] | None:
    """解析普通/Markdown 加粗字段行，识别活动原文等价写法。"""
    raw = line.strip()
    raw = re.sub(r"^[*_\s]+", "", raw)
    match = re.match(r"^([^：:]{1,32}?)\s*[：:]\s*(.*)$", raw)
    if not match:
        return None
    label = re.sub(r"[*_\s]+$", "", match.group(1)).strip()
    value = match.group(2).strip()
    value = re.sub(r"^[*_\s]+", "", value)
    value = re.sub(r"[*_\s]+$", "", value)
    canonical = _canonical_field(label)
    if canonical not in FIELD_ORDER:
        return None
    return canonical, value


def _is_placeholder(value: str) -> bool:
    normalized = value.strip()
    normalized = re.sub(r"^[*_\s]+|[*_\s]+$", "", normalized)
    normalized = normalized.strip().strip("。；;,.，")
    if not normalized:
        return True
    low = normalized.lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    return (
        normalized.startswith("未提供")
        or normalized.startswith("资料未提供")
        or normalized.startswith("用户未提供")
        or normalized.startswith("用户未填写")
        or normalized.startswith("待补充")
        or normalized.startswith("暂无")
    )


def prepare_article_body(
    ai_body: str, has_experience: bool = False
) -> tuple[str, list[str]]:
    """清理正文占位值，并返回明确的空字段。

    设计原则：
    - 只有识别到字段且值为空/占位时才报告缺失；
    - 自然段文章没有字段标题，不据此断言缺少游戏介绍或故事；
    - 未提供经历时，删除带“故事/感想”标题的段落，不删除普通自然段；
    - Markdown 加粗、空白和中英文冒号均可识别；
    - “游戏当时发售平台”“玩游戏的时间”等活动原文写法可识别。
    """
    body = strip_generated_title_and_game_name(ai_body or "")
    lines = body.splitlines()
    cleaned: list[str] = []
    missing: list[str] = []
    skip_story = False

    for line in lines:
        parsed = _parse_field_line(line)
        if parsed is not None:
            canonical, value = parsed
            if canonical == "故事/感想" and not has_experience:
                skip_story = True
                if canonical not in missing:
                    missing.append(canonical)
                continue
            skip_story = False
            if _is_placeholder(value):
                if canonical not in missing:
                    missing.append(canonical)
                continue
            cleaned.append(f"{canonical}：{value}")
            continue

        if skip_story:
            if not line.strip():
                skip_story = False
                cleaned.append("")
            continue
        cleaned.append(line.rstrip())

    ordered_missing = [label for label in FIELD_ORDER if label in missing]
    body = "\n".join(cleaned).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body, ordered_missing


def article_body_length(ai_body: str, has_experience: bool = False) -> int:
    """统计清理后正文的非空白字符数，不把标题/提示算入。"""
    body, _missing = prepare_article_body(ai_body, has_experience)
    return len(re.sub(r"\s+", "", body))


def build_post_content(
    game_name: str, ai_body: str, has_experience: bool = False
) -> str:
    """拼装正文区：游戏名称 + 清理后的正文（不含活动标题/说明）。"""
    body, _missing = prepare_article_body(ai_body, has_experience)
    return f"游戏名称：{game_name}\n{body}" if body else f"游戏名称：{game_name}"


def build_final_message(
    game_name: str,
    ai_body: str,
    experience: str = "",
    warnings: list[str] | None = None,
) -> str:
    """拼装可复制的标题、活动说明和正文。

    不把“未提供/待补充”等占位值放进正文；缺失信息、长度不足和
    截断提示放在正文外，并说明插件不会自动发布或提交活动。
    """
    has_experience = bool(str(experience or "").strip())
    body, missing = prepare_article_body(ai_body, has_experience)
    title = f"【我的百分之一】+【{game_name}】"
    post = build_post_content(game_name, body, has_experience)

    notes: list[str] = []
    if missing:
        notes.append(
            "生成结果中以下字段为空（不属于正文）："
            + "、".join(missing)
        )
    if not has_experience:
        notes.append(
            "未提供个人经历。客观介绍不能替代个人故事，"
            "请勿把搜索结果写成自己的游玩经历。"
        )
    if missing or not has_experience:
        notes.append(
            f"补充方式：小作文 {game_name} 经历：<游玩时间、故事或感想>"
        )
    for warning in warnings or []:
        if warning:
            notes.append(str(warning))
    notes.append(
        "请复制标题和正文后自行发布；插件不会自动发布、添加图片或提交活动。"
    )
    notes.append("生成内容仍可能不准确，发布前请自行核对。")

    final = f"{title}\n\n{ACTIVITY_INTRO}\n\n{post}"
    return final + "\n\n---\n📝 发布前提示（不属于正文）：\n" + "\n".join(notes)
