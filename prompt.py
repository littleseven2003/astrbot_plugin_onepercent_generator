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
        return text.strip(), ""

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
    if search_summary:
        return (
            "【搜索资料（不可信参考，可能过时或错误）】\n"
            f"{search_summary}"
        )
    return (
        "【搜索资料】\n未获取到可用资料。不要猜测具体平台、日期、"
        "评价或用户经历。"
    )


def _experience_section(experience: str) -> tuple[str, str, str]:
    """返回（经历区、事实边界、字段要求）。"""
    if experience:
        experience_block = (
            "【用户真实经历（唯一可用的个人事实来源）】\n"
            f"{experience}"
        )
        boundaries = """【事实边界】
- 只能使用上述经历中明确出现的时间、事件、人物、感受和细节；没有出现的信息一律不要补造。
- 不得新增具体游玩时长、某天发生的事、个人心理活动或主观评价。
- 搜索资料可以用于发售平台等客观游戏信息，但不得当作你的个人经历，也不得覆盖或扩写用户经历。
- 经历没有提到的个人字段（如游玩时间、个人故事、主观感受）写“未提供”或“待补充”，不要猜测；发售平台等客观字段可按明确的搜索资料写。"""
        fields = """【需要输出的字段】
发售平台：可基于搜索资料中明确的客观平台信息；不确定写“资料未提供”，不得把搜索资料写成个人经历。
游玩时间：只写经历中明确出现的时长或时间；未出现写“未提供”。
推荐人群：只基于经历明确信息和游戏客观类型；不确定写“待补充”。
个人故事/推荐理由：只能重组经历中明确出现的事实和感受，不新增细节。"""
    else:
        experience_block = (
            "【用户真实经历】\n未提供。不要写第一人称“我玩过/我喜欢/"
            "我玩了 X 小时”等个人经历。"
        )
        boundaries = """【事实边界】
- 不要虚构具体游玩时长、个人事件、主观感受或第一人称游玩经历。
- 搜索资料不可信，只作游戏客观信息参考，不得写成用户经历。
- 可以写客观推荐、游戏类型、平台和适合人群；不确定的内容写“资料未提供”或“待补充”。"""
        fields = """【需要输出的字段】
发售平台：可基于搜索资料写明确平台；不确定写“资料未提供”。
游玩时间：用户未提供，写“未提供”，不得推测。
推荐人群：基于游戏类型或公开资料客观推荐；不确定写“待补充”。
个人故事/推荐理由：用户未提供真实经历，改写客观推荐理由；不要编造第一人称经历。"""
    return experience_block, boundaries, fields


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
            "用户提供了真实经历。正文围绕这些经历组织，不得把搜索资料"
            "写成用户经历。"
        )
        opening_hint = "开头可从用户经历中明确出现的某个细节切入。"
    else:
        mode_line = (
            "用户未提供真实经历。正文写客观推荐或清楚的待补充内容，"
            "不得虚构个人游玩经历。"
        )
        opening_hint = "开头从游戏客观特点切入，不要写“我”的游玩经历。"

    return f"""你是一个熟悉游戏社区发帖风格的中文写作助手。请根据玩家输入的游戏名称"{game_name}"生成一篇游戏推荐帖的正文内容。

【重要约束】
- 只把"{game_name}"当作游戏名称，不要使用活动标题格式或活动名称作为搜索、理解、生成的游戏关键词
- 不要输出帖子标题
- 不要输出“游戏名称：”这一行，这一行会由程序固定填入玩家输入的游戏名称
- 不要改写、翻译、扩写或纠正玩家输入的游戏名称
- 正文中提到游戏时，只能使用"{game_name}"这个名称
- {mode_line}

【写作风格：{style["label"]}】
- {style["opening"]}
- {style["rhythm"]}
- {opening_hint}

{experience_block}

{boundaries}

【帖子结构】（按顺序写，不要输出段落标签，直接写内容）：
1. 先写发售平台、游玩时间、推荐人群
2. 再写游戏介绍和推荐理由。不得为了“真实感”补造用户没有提供的经历。

【写作要求】
- 语气像真实玩家在论坛分享，但不要假装拥有用户没有提供的游玩经历
- 不直接复制搜索资料，用自己的话表达；不把搜索资料当作已核验事实
- 不要输出“标题：”“正文：”这类标记，直接输出帖子内容即可
- 不要写活动说明和玩家许愿信息，这些部分会另外处理

{search_block}

{fields}

请直接输出从“发售平台：”开始的正文内容。"""
