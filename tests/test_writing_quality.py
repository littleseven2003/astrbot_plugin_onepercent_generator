import random
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR.parent))

from astrbot_plugin_onepercent_generator.post_process import (  # noqa: E402
    build_final_message,
    strip_generated_title_and_game_name,
)
from astrbot_plugin_onepercent_generator.prompt import (  # noqa: E402
    GAME_REQUIRED_MESSAGE,
    MAX_EXPERIENCE_CHARS,
    WRITING_VARIANTS,
    build_main_prompt,
    choose_writing_variant,
    needs_explicit_game,
    parse_writing_request,
)


class WritingRequestParsingTests(unittest.TestCase):
    def test_old_triggers_and_game_names_remain_compatible(self):
        self.assertEqual(parse_writing_request("小作文"), (True, "", ""))
        self.assertEqual(
            parse_writing_request("我的百分之一"), (True, "", "")
        )
        self.assertEqual(
            parse_writing_request("小作文 星露谷物语"),
            (True, "星露谷物语", ""),
        )
        self.assertEqual(
            parse_writing_request("我的百分之一 塞尔达传说：王国之泪"),
            (True, "塞尔达传说：王国之泪", ""),
        )

    def test_experience_after_newline_pipe_or_space(self):
        expected = (True, "原神", "我玩了 20 小时，喜欢探索")
        for text in (
            "小作文 原神\n经历：我玩了 20 小时，喜欢探索",
            "小作文 原神 | 经历：我玩了 20 小时，喜欢探索",
            "小作文 原神 经历：我玩了 20 小时，喜欢探索",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_writing_request(text), expected)

    def test_ordinary_game_name_is_not_parsed_as_experience(self):
        self.assertEqual(
            parse_writing_request("小作文 游戏经历"),
            (True, "游戏经历", ""),
        )
        self.assertEqual(
            parse_writing_request("小作文 游戏|测试"),
            (True, "游戏|测试", ""),
        )
        self.assertEqual(parse_writing_request("小作文原神"), (False, "", ""))

    def test_empty_experience_is_treated_as_absent(self):
        self.assertEqual(
            parse_writing_request("小作文 原神 经历："), (True, "原神", "")
        )
        self.assertEqual(
            parse_writing_request("小作文 原神\n经历：  "),
            (True, "原神", ""),
        )

    def test_experience_only_is_parsed_without_game(self):
        matched, game_name, experience = parse_writing_request(
            "小作文 经历：我玩了 20 小时"
        )
        self.assertTrue(matched)
        self.assertEqual(game_name, "")
        self.assertEqual(experience, "我玩了 20 小时")

    def test_special_characters_and_unicode_are_preserved(self):
        experience = "第一行 <>&{}\n第二行“引号”和 emoji 🎮"
        matched, game_name, parsed = parse_writing_request(
            "小作文 原神 | 经历：" + experience
        )
        self.assertTrue(matched)
        self.assertEqual(game_name, "原神")
        self.assertEqual(parsed, experience)

    def test_experience_only_requires_explicit_game(self):
        matched, game_name, experience = parse_writing_request(
            "小作文 经历：我玩了 20 小时"
        )
        self.assertTrue(matched)
        self.assertEqual(game_name, "")
        self.assertEqual(experience, "我玩了 20 小时")
        self.assertTrue(needs_explicit_game(game_name, experience))
        self.assertFalse(needs_explicit_game("原神", experience))
        self.assertFalse(needs_explicit_game("", ""))
        self.assertIn("请同时提供游戏名", GAME_REQUIRED_MESSAGE)

    def test_experience_length_is_bounded(self):
        long_experience = "经" * (MAX_EXPERIENCE_CHARS + 100)
        _, _, parsed = parse_writing_request(
            "小作文 原神 经历：" + long_experience
        )
        self.assertEqual(len(parsed), MAX_EXPERIENCE_CHARS)

    def test_unmatched_text_is_rejected(self):
        self.assertEqual(parse_writing_request("随便聊聊"), (False, "", ""))
        self.assertEqual(parse_writing_request("  "), (False, "", ""))


class WritingPromptTruthBoundaryTests(unittest.TestCase):
    def test_old_prompt_call_preserves_game_name_and_search(self):
        prompt = build_main_prompt("星露谷物语", "像素风农场模拟游戏")

        self.assertIn('游戏名称"星露谷物语"', prompt)
        self.assertIn('只能使用"星露谷物语"这个名称', prompt)
        self.assertIn("像素风农场模拟游戏", prompt)
        self.assertIn("【搜索资料（不可信参考", prompt)

    def test_experience_prompt_allows_only_user_facts(self):
        experience = "我玩了 20 小时，最喜欢钓鱼和布置农场。"
        prompt = build_main_prompt(
            "星露谷物语",
            "搜索资料说这是一款农场游戏",
            experience=experience,
            variant="warm",
        )

        self.assertIn("【用户真实经历（唯一可用的个人事实来源）】", prompt)
        self.assertIn(experience, prompt)
        self.assertIn("只能使用上述经历中明确出现", prompt)
        self.assertIn("不得新增具体游玩时长", prompt)
        self.assertIn("搜索资料可以用于发售平台等客观游戏信息", prompt)
        self.assertIn("不得覆盖或扩写用户经历", prompt)
        self.assertNotIn("请务必写一段有真实感的个人体验", prompt)

    def test_experience_prompt_allows_objective_platform_from_search(self):
        prompt = build_main_prompt(
            "原神",
            "公开资料：发售平台为 PC、iOS、Android、PS5。",
            experience="我玩了 20 小时，喜欢在璃月爬山。",
            variant="warm",
        )

        self.assertIn("发售平台：可基于搜索资料", prompt)
        self.assertIn("不得把搜索资料写成个人经历", prompt)
        self.assertNotIn("发售平台：只写经历", prompt)
        self.assertIn("发售平台为 PC、iOS、Android、PS5", prompt)

    def test_no_experience_prompt_forbids_first_person_fabrication(self):
        prompt = build_main_prompt(
            "原神", "搜索资料", experience="", variant="concise"
        )

        self.assertIn("【用户真实经历】\n未提供", prompt)
        self.assertIn("不要写第一人称", prompt)
        self.assertIn("不要虚构具体游玩时长", prompt)
        self.assertIn("不得写成用户经历", prompt)
        self.assertIn("改写客观推荐理由", prompt)
        self.assertNotIn("请务必写一段有真实感的个人体验", prompt)
        self.assertNotIn("根据你对该游戏的了解自然生成", prompt)

    def test_search_material_is_labeled_untrusted_reference(self):
        prompt = build_main_prompt("原神", "搜索摘要 2026-01-01 上线")

        self.assertIn("【搜索资料（不可信参考，可能过时或错误）】", prompt)
        self.assertIn("搜索摘要 2026-01-01 上线", prompt)
        self.assertIn("不得写成用户经历", prompt)

    def test_missing_search_material_degrades_without_fabrication(self):
        prompt = build_main_prompt("原神", "", experience="")

        self.assertIn("未获取到可用资料", prompt)
        self.assertIn("不要猜测具体平台、日期", prompt)

    def test_variants_are_observable_but_keep_game_name(self):
        prompts = {
            name: build_main_prompt(
                "星露谷物语", "农场游戏", experience="", variant=name
            )
            for name in WRITING_VARIANTS
        }
        for name, prompt in prompts.items():
            self.assertIn(WRITING_VARIANTS[name]["label"], prompt)
            self.assertIn('只把"星露谷物语"当作游戏名称', prompt)
        self.assertEqual(len(set(prompts.values())), len(prompts))
        self.assertIn("简洁直接", prompts["concise"])
        self.assertIn("克制回望", prompts["reflective"])

    def test_variant_selection_is_reproducible_with_seed(self):
        first = random.Random(42)
        second = random.Random(42)
        first_choices = [choose_writing_variant(first) for _ in range(10)]
        second_choices = [choose_writing_variant(second) for _ in range(10)]

        self.assertEqual(first_choices, second_choices)
        self.assertTrue(
            all(choice in WRITING_VARIANTS for choice in first_choices)
        )

    def test_offline_prompt_examples_cover_multiple_games(self):
        examples = [
            (
                "原神",
                "开放世界探索和元素反应",
                "我玩了 20 小时，最喜欢在璃月爬山。",
                "warm",
                "我玩了 20 小时",
            ),
            (
                "星露谷物语",
                "像素风农场经营",
                "我玩了两个季节，主要在钓鱼和种田。",
                "reflective",
                "钓鱼和种田",
            ),
            (
                "艾尔登法环",
                "",
                "",
                "concise",
                "未获取到可用资料",
            ),
        ]
        for game, search, experience, variant, marker in examples:
            with self.subTest(game=game, variant=variant):
                prompt = build_main_prompt(
                    game, search, experience=experience, variant=variant
                )
                self.assertIn(f'只把"{game}"当作游戏名称', prompt)
                self.assertIn(marker, prompt)
                if experience:
                    self.assertIn("唯一可用的个人事实来源", prompt)
                else:
                    self.assertIn("【用户真实经历】\n未提供", prompt)

    def test_post_processing_still_uses_user_game_name(self):
        body = "标题：错误\n游戏名称：错误\n正文：\n发售平台：PC"
        cleaned = strip_generated_title_and_game_name(body)
        final = build_final_message("原神", cleaned)

        self.assertNotIn("标题：错误", cleaned)
        self.assertIn("游戏名称：原神", final)
        self.assertTrue(final.startswith("什么是【我的百分之一】"))


if __name__ == "__main__":
    unittest.main()
