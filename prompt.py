"""
Prompt 模板组装与输入解析模块。

保留旧版 build_main_prompt(game_name, search_summary) 调用方式，
并增加可选真实经历和可控写作风格。
"""

import random
import re

SYSTEM_PROMPT = "你是一个中文写作助手，请严格按照用户要求的格式输出。"

MAX_EXPERIENCE_CHARS = 600
TRIGGERS = ("小作文", "我的百分之一")
EXPERIENCE_MARKERS = ("经历：", "经历:")

DEFAULT_WRITING_VARIANT = "warm"
ARTICLE_TARGET_MIN_CHARS = 800
ARTICLE_TARGET_MAX_CHARS = 1200
GAME_REQUIRED_MESSAGE = (
    "请同时提供游戏名，例如：小作文 原神 经历：我玩了 20 小时。"
)

# 少量可观察的结构/语气变化。所有变体都不得改变游戏名，也不得突破
# 下面的事实边界。
WRITING_VARIANTS = {
    "warm": {
        "label": "温暖自然",
        "opening": (
            "从游戏最突出的一个具体机制、场景或氛围写起，像朋友聊天，"
            "不要用空泛口号或套话开头。"
        ),
        "rhythm": "段落有长有短，少用排比和大词，避免每段都用同一种句式。",
    },
    "concise": {
        "label": "简洁直接",
        "opening": (
            "开头直接给结论或推荐对象，先讲适合谁，再讲一两个具体理由，"
            "短句为主。"
        ),
        "rhythm": "删掉重复形容词，优先写具体机制和体验边界，不堆砌赞美。",
    },
    "reflective": {
        "label": "克制回望",
        "opening": (
            "从‘为什么值得推荐给某类玩家’切入，语气克制，用具体设计或"
            "机制作依据。"
        ),
        "rhythm": "允许回望式表达，但不要虚构个人经历，也不要写夸张情绪。",
    },
}


ACTIVITY_FIELD_NAMES = (
    "游戏名称",
    "游戏当时发售平台",
    "发售平台",
    "玩游戏的时间",
    "游玩时间",
    "游戏时间",
    "推荐人群（理由）",
    "推荐人群",
    "正文",
)
_ACTIVITY_LINE_RE = re.compile(
    r"^[ \t]*(?:" + "|".join(
        re.escape(name) for name in ACTIVITY_FIELD_NAMES
    ) + r")\s*[：:][^\n]*$",
    re.MULTILINE,
)
_INLINE_FIELD_SPLIT_RE = re.compile(
    r"\s+(?=(?:" + "|".join(
        re.escape(name) for name in ACTIVITY_FIELD_NAMES
    ) + r")\s*[：:])"
)
_ACTIVITY_VALUE_RE = re.compile(
    r"(?:" + "|".join(
        re.escape(name) for name in ACTIVITY_FIELD_NAMES
    ) + r")\s*[：:]\s*([^\n]*)"
)


def parse_activity_fields(text: str) -> dict:
    """从用户消息里取活动固定字段（平台/时间/推荐人群）。

    只在用户明确写出「字段：值」时返回，不做任何猜测；占位提示
    （【请补充…】）不当作真实值。返回规范键：发售平台、游玩时间、
    推荐人群。
    """
    value = str(text or "")
    fields: dict[str, str] = {}
    for match in _ACTIVITY_VALUE_RE.finditer(value):
        raw = match.group(1).strip()
        if not raw or raw.startswith("【请补充"):
            continue
        label = match.group(0).split("：", 1)[0].split(":", 1)[0].strip()
        label = label.replace("（理由）", "")
        if label in ("发售平台", "游戏当时发售平台"):
            fields.setdefault("发售平台", raw)
        elif label in ("玩游戏的时间", "游玩时间", "游戏时间"):
            fields.setdefault("游玩时间", raw)
        elif label == "推荐人群":
            fields.setdefault("推荐人群", raw)
    return fields


def strip_activity_field_lines(text: str) -> str:
    """把用户消息里独立成行的固定字段行移除，留下游戏名/经历正文。"""
    cleaned = _ACTIVITY_LINE_RE.sub("", str(text or ""))
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()



def _split_experience(text: str) -> tuple[str, str]:
    """从触发语后的文本中分离游戏名和可选经历。

    只在明确的“经历：/经历:”标记前分割，且标记必须位于开头、空白
    之后或 |/｜ 分隔符之后。因此普通游戏名（包括含空格、冒号、竖线
    的名称）不会被误认为经历。
    """
    best: tuple[int, str] | None = None
    for marker in EXPERIENCE_MARKERS:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            if index == 0:
                valid = True
            else:
                prev = text[index - 1]
                valid = prev.isspace() or prev in "|｜"
            if valid and (best is None or index < best[0]):
                best = (index, marker)
                break
            start = index + 1

    if best is None:
        cleaned = strip_activity_field_lines(text)
        # 同一行里紧随游戏名写字段（如「原神 玩游戏的时间：半小时」）时，
        # 在字段名前的空白处截断，字段值不进入游戏名。
        parts = _INLINE_FIELD_SPLIT_RE.split(cleaned, maxsplit=1)
        return parts[0].strip(), ""

    index, marker = best
    game_name = text[:index].rstrip()
    game_name = re.sub(r"(?:\s*[|｜]\s*)+$", "", game_name).strip()
    experience = text[index + len(marker):].strip()
    if len(experience) > MAX_EXPERIENCE_CHARS:
        experience = experience[:MAX_EXPERIENCE_CHARS].rstrip()
    return game_name, experience


def parse_writing_request(text: str) -> tuple[bool, str, str]:
    """解析小作文触发请求。

    Returns:
        (matched, game_name, experience)

    兼容：
    - 小作文 / 我的百分之一 → 随机游戏
    - 小作文 原神 / 我的百分之一 原神 → 指定游戏
    - 小作文 原神 经历：... → 指定游戏 + 真实经历
    - 小作文 原神\\n经历：... → 同上
    - 小作文 原神 | 经历：... → 同上

    经历标记必须是显式的“经历：”或“经历:”，普通游戏名不会被当作
    经历。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return False, "", ""

    for trigger in TRIGGERS:
        if text == trigger:
            return True, "", ""
        if not text.startswith(trigger):
            continue
        after = text[len(trigger):]
        if not after or not (
            after[0].isspace() or after[0] in "|｜"
        ):
            continue
        game_name, experience = _split_experience(after.strip())
        return True, game_name, experience

    return False, "", ""


EXTRA_FIELD_LABELS = (
    ("许愿卡", ("许愿卡牌", "许愿卡")),
    ("游戏ID", ("游戏ID", "游戏 id")),
    ("账号ID", ("账号ID", "账号 id")),
    ("区服", ("区服", "服务器")),
    ("系统", ("系统",)),
)

_EXTRA_LABEL_PATTERN = (
    r"许愿卡牌?|游戏\s*[Ii][Dd]|账号\s*[Ii][Dd]|区服|服务器|系统"
)
_EXTRA_RE = re.compile(
    r"(?P<label>" + _EXTRA_LABEL_PATTERN + r")\s*[:：]\s*"
    r"(?P<value>.+?)"
    r"(?=\s*(?:" + _EXTRA_LABEL_PATTERN + r")\s*[:：]|[|｜\n]|$)",
    re.IGNORECASE,
)


def _canonical_extra_label(label: str) -> str:
    text = re.sub(r"\s+", "", label).lower()
    if text.startswith("许愿卡"):
        return "许愿卡"
    if text.startswith("游戏"):
        return "游戏ID"
    if text.startswith("账号"):
        return "账号ID"
    if text.startswith("区服") or text.startswith("服务器"):
        return "区服"
    return "系统"


def parse_optional_extras(text: str) -> tuple[str, dict]:
    """从用户输入里提取活动可选尾注字段，并把它们从经历中移除。

    只识别用户明确写出的“标签：值”，支持许愿卡、游戏 ID、账号 ID、
    区服/服务器、系统；没有写就不产生字段，绝不猜测。
    返回（清理后的文本, 按活动顺序排列的字段字典）。
    """
    value = str(text or "")
    extras: dict[str, str] = {}
    for match in _EXTRA_RE.finditer(value):
        canonical = _canonical_extra_label(match.group("label"))
        raw = match.group("value").strip().strip("。；;,，")
        if raw:
            extras.setdefault(canonical, raw)
    cleaned = _EXTRA_RE.sub("", value)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"(?:\s*[|｜]\s*)+$", "", cleaned).strip()
    ordered = {
        label: extras[label]
        for label, _markers in EXTRA_FIELD_LABELS
        if label in extras
    }
    return cleaned, ordered


def needs_explicit_game(game_name: str, experience: str) -> bool:
    """只提供经历、没有游戏名时不能随机换成另一个游戏。"""
    return bool(str(experience or "").strip() and not str(game_name or "").strip())


def choose_writing_variant(rng=None) -> str:
    """选择写作变体；传入 random.Random(seed) 可在测试中固定。"""
    choices = tuple(WRITING_VARIANTS)
    chooser = rng if rng is not None else random
    try:
        selected = chooser.choice(choices)
    except AttributeError:
        selected = random.choice(choices)
    return selected if selected in WRITING_VARIANTS else DEFAULT_WRITING_VARIANT


def _search_section(search_summary: str) -> str:
    """搜索素材区：标题、来源、链接、片段直接入模（默认路径）。

    片段可能被按句子截断；链接只用于核对，不要求原文引用，也不能把
    素材里的说法写成用户自己的经历。
    """
    if search_summary:
        return (
            "【搜索素材（不可信参考，可能过时或错误；不是用户经历）】\n"
            "以下每条包含来源、标题、链接和片段；片段可能被截断。可以"
            "依据其中的类型、背景、美术和核心玩法演绎合理情节，但不要"
            "照抄；不得把素材里第三方玩家的故事写成用户自己的经历，也"
            "不得编造素材没有的机制、数值、剧情或事件。\n"
            f"{search_summary}"
        )
    return (
        "【搜索素材】\n未获取到可用资料。按游戏常识写合理、自然的"
        "平台、时期与推荐人群，不要编造具体日期、版本、销量或用户经历。"
    )


def _experience_section(experience: str) -> tuple[str, str, str]:
    """返回（经历区、事实边界、输出格式要求）。

    活动要求以玩家自己的故事/感想为主、游戏介绍为辅，前置字段按官方
    写法输出；缺失字段整行省略，不写占位值。
    """
    if experience:
        experience_block = (
            "【用户真实经历（唯一可用的个人事实来源，优先保留）】\n"
            f"{experience}"
        )
        boundaries = """【事实边界】
- 用户经历里明确出现的时间、事件、人物、感受和细节优先保留，不能改写、替换或删掉。
- 可以围绕用户经历补写与该游戏背景一致的第一人称场景、选择和感受，让故事完整；补写内容不得与搜索素材里的类型、背景、美术或核心玩法矛盾。
- 不要编造游戏机制、系统数值、剧情、版本、销量或发售日期；不要编造玩家的现实身份或真实姓名。用户没给游玩时间时，可以结合发售时间与正文故事写一个合理时期，但不能早于游戏发售。
- 搜索素材只作客观游戏信息的参考，不得把素材里第三方玩家的故事写成用户自己的经历，也不得覆盖或扩写用户经历。
- 素材不足或互相矛盾时，宁可写短、写泛，也不要给出具体的虚构游戏设定或事件断言。"""
        fields = """【输出格式】输出正文，并写下面三个前置字段行（游戏名称与“正文：”标签由程序固定，不要输出）：
游戏当时发售平台：结合搜索素材与游戏常识写真实、合理的发售平台（如 PC / PS4 / PS5 / Xbox / Switch）；用户已经提供平台时保持用户说法。
玩游戏的时间：结合游戏发售时间与正文故事写一个合理的游玩时期或时长，不能早于游戏发售；用户已经提供时间时保持用户说法。
推荐人群（理由）：结合游戏类型与正文观点写具体理由。
正文以用户经历为主线，游戏介绍服务于故事，自然成段，不要写“游戏介绍：”“故事/感想：”这类小标题。"""
    else:
        experience_block = (
            "【用户真实经历】\n未提供。可以写第一人称的合理游玩场景，并"
            "按发售时间给出合理的游玩时期；不能编造玩家的现实身份或具体日期。"
        )
        boundaries = """【事实边界】
- 搜索素材只作游戏客观信息参考：可以依据它演绎合理的游玩场景，但不得编造素材里没有的机制、数值、剧情或事件。
- 素材明确给出类型、背景、美术风格或核心玩法时，第一人称场景必须严格符合这些设定，不能出现类型矛盾（例如把恐怖游戏写成轻松日常、把硬核射击写成休闲换装）。
- 素材不足或互相矛盾时，不写具体的第一人称事件，改为客观推荐、类型说明和适合人群。
- 不要编造具体日期、现实身份或销量；游玩时间按发售时间写合理时期，不要输出占位值。"""
        fields = """【输出格式】输出正文，并写下面三个前置字段行（游戏名称与“正文：”标签由程序固定，不要输出）：
游戏当时发售平台：结合搜索素材与游戏常识写真实、合理的发售平台；拿不准时写“多平台”。
玩游戏的时间：结合游戏发售时间与正文故事写一个合理的游玩时期或时长，不能早于游戏发售。
推荐人群（理由）：基于游戏类型与正文观点写具体理由。
写自然的推荐草稿，客观但有具体细节；第一人称场景必须与素材一致，缺少可靠素材时不要写具体事件。"""
    return experience_block, boundaries, fields


def is_thin_experience(experience: str) -> bool:
    """经历是否只有时长/一句泛评，缺少可写成故事的具体片段。"""
    text = str(experience or "")
    # 去掉时长与时间短语后，剩余内容过短，说明没有可用的场景细节。
    without_time = re.sub(
        r"(?:约|大约|差不多|将近)?\s*\d+(?:\.\d+)?\s*个?\s*"
        r"(?:小时|分钟|天|周|星期|个月|年)|半小时|半个小时|"
        r"(?:去年|前年|今年|那年|初中|高中|大学|小学|儿时|小时候)?的?"
        r"(?:暑假|寒假|春天|夏天|秋天|冬天|国庆|五一|春节|元旦)",
        "",
        text,
    )
    stripped = re.sub(r"[\s，。；;,.!！?？、]", "", without_time)
    return len(stripped) < 12



def build_main_prompt(
    game_name: str,
    search_summary: str,
    experience: str = "",
    variant: str = DEFAULT_WRITING_VARIANT,
) -> str:
    """组装主 Prompt。

    旧调用 build_main_prompt(game_name, search_summary) 保持兼容；
    experience 只接受用户显式提供的内容，variant 用于可控风格变化。
    """
    game_name = str(game_name or "").strip()
    search_summary = str(search_summary or "").strip()
    experience = str(experience or "").strip()
    if len(experience) > MAX_EXPERIENCE_CHARS:
        experience = experience[:MAX_EXPERIENCE_CHARS].rstrip()

    style = WRITING_VARIANTS.get(
        variant, WRITING_VARIANTS[DEFAULT_WRITING_VARIANT]
    )
    experience_block, boundaries, fields = _experience_section(experience)
    search_block = _search_section(search_summary)

    if experience:
        mode_line = (
            "用户提供了真实经历。这是一篇“我的百分之一”式的个人故事："
            "正文首段必须用第一人称写用户给出的具体场景、动作或选择；"
            "可以围绕经历补写与该游戏背景一致的情节，但不得改写用户"
            "事实，也不得与搜索素材的类型、背景或玩法矛盾。"
        )
        # 有经历时，风格变体只保留节奏差异；首段一律由经历切入，
        # 不能出现「从最突出机制写起」「先给推荐结论」这类冲突指令。
        style_block = (
            "【写作风格：第一人称回忆】\n"
            f"- {style['rhythm']}\n"
            "- 首段用第一人称写用户给出的具体场景、动作或选择，"
            "不写游戏简介，不从机制、系统、玩法写起"
        )
        opening_hint = (
            "首段直接落到用户经历里的一个具体时刻或场景，"
            "不要先介绍游戏。"
        )
        structure = (
            "1. 游戏名称与正文标签由程序固定；你负责正文和「游戏当时发售平台」「玩游戏的时间」「推荐人群（理由）」三行\n"
            "2. 正文围绕“发生了什么、当时怎么想、后来怎么看”展开；"
            "只有用户提供了时间节点和情绪变化才写\n"
            "3. 所有补写的场景必须符合该游戏的类型与背景（恐怖写资源"
            "紧张与取舍、硬核射击写战术与失误代价），不能出现类型矛盾\n"
            "4. 游戏机制只在解释某个具体情节时带出，每处最多一句，"
            "正文最多带出两项相关机制；禁止用小标题、编号或连续段落"
            "逐项介绍玩法、系统和功能\n"
            "5. 游戏内容介绍只作背景，占比明显少于个人故事或玩后感想\n"
            "6. 正文 4-6 段自然段，不要写成游戏介绍加感想的拼盘"
        )
        thin_line = (
            "- 经历信息很少（比如只有时长或一句泛泛评价）：写一篇短草稿，"
            "用与素材一致的一般性场景补足，但不要编造用户的具体事件或"
            "强烈情绪。"
            if is_thin_experience(experience) else ""
        )
    else:
        mode_line = (
            "用户未提供真实经历。可以写有代入感的第一人称推荐：场景、"
            "选择和感受必须严格符合搜索素材里的游戏类型、背景、美术与"
            "核心玩法；素材不足或互相矛盾时退回客观推荐，不写具体事件。"
        )
        opening_hint = (
            "开头从游戏最具体的一个机制或场景切入，不要用空泛口号开头。"
        )
        structure = (
            "1. 游戏名称与正文标签由程序固定；你负责正文和「游戏当时发售平台」「玩游戏的时间」「推荐人群（理由）」三行\n"
            "2. 正文：先落到一个与素材一致的具体场景或机制，说明它怎么玩、"
            "适合什么口味的人，再写这类玩家可能遇到的取舍\n"
            "3. 素材不足时改成客观推荐，不编造具体剧情、数值或事件\n"
            "4. 正文 4-6 段自然段，避免“游戏介绍 + 适合人群”的模板感"
        )
        style_block = (
            f"【写作风格：{style['label']}】\n"
            f"- {style['opening']}\n"
            f"- {style['rhythm']}\n"
            f"- {opening_hint}"
        )
        thin_line = ""

    return f"""你是一个熟悉游戏社区发帖风格的中文写作助手。请根据玩家输入的游戏名称"{game_name}"生成一篇游戏推荐帖的正文内容。

【重要约束】
- 只把"{game_name}"当作游戏名称，不要使用活动标题格式或活动名称作为搜索、理解、生成的游戏关键词
- 不要输出帖子标题
- 不要输出“游戏名称：”这一行，这一行会由程序固定填入玩家输入的游戏名称
- 不要改写、翻译、扩写或纠正玩家输入的游戏名称
- 正文中提到游戏时，只能使用"{game_name}"这个名称
- {mode_line}

{style_block}

{experience_block}

{boundaries}

【篇幅目标】
正文约 {ARTICLE_TARGET_MIN_CHARS}-{ARTICLE_TARGET_MAX_CHARS} 个中文字，这是产品目标。素材或经历不足时宁可写短，但不要用空话、重复形容词或机械排比凑字数。

【帖子结构】：
{structure}

【写作要求】
- 读起来像真人发帖：有具体的时间、地点、动作、犹豫或取舍，少用总结句和升华句
- 故事必须符合该游戏的类型与背景：恐怖写压迫与资源取舍，硬核射击写战术与失误代价，休闲经营写节奏与日常；不能出现类型矛盾
- 演绎只补场景、动作和感受，不新增游戏机制、数值、剧情、版本或销量
- 避免 AIGC 腔：不要“总的来说/总而言之/值得一提的是/在这个游戏中/不禁让人”，不要每段都用同一句式，不要三段式排比
- 不说教、不喊口号，不写“这款游戏值得每一个热爱生活的人”这类空话
- 不直接复制搜索素材，用自己的话表达；不把素材当作已核验事实，不编造日期、版本或销量
- 禁止写成品评测或玩法说明书：不逐条介绍玩法、系统、功能；不写“核心玩法/特色系统/游戏亮点”这类段落
{thin_line}
- 不写许愿卡、游戏 ID、账号 ID、区服或系统信息，这些由程序按用户提供的内容追加
- 不要输出活动标题、活动说明链接或“游戏名称：”行，程序会统一拼装
- 不要声称已发布、已添加图片或已提交活动
- 不要输出“标题：”“正文：”这类标记，直接输出帖子内容

{search_block}

{fields}

字段名、标题、活动说明和正文标签由程序统一拼装；只输出正文与上面三个字段行，不要输出「【请补充……】」这类占位值。
请直接输出帖子内容。"""
