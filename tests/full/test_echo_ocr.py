"""合成 OCR 文本与坐标；可选本地引擎验证实机截图。"""

import copy
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from src.full.echo_ocr import (
    Completeness, LocalOcrEngine, OcrError, OcrToken, SubStatEntry, parse_sub_stats,
    validate_selection,
)
from src.echo_score import DEFAULT_STATS, load_json


def token(text, line=0, x=0, confidence=.98):
    return OcrToken(text, (x, line * 24, x + 70, line * 24 + 14), confidence)


HEADER = ("暴击率 + 10.5%", "固定攻击 + 40")
GAME_NAME = "共鸣回响·鸣式·虚造神型 Z C"
GAME_LEVEL = "+0 > +10 +20000 0/400"
UNOPENED_ROWS = (
    "强化至+15并激活辅音属性",
    "强化至+20并激活辅音属性",
    "强化至+25并激活辅音属性",
)


class EchoOcrTests(unittest.TestCase):
    def setUp(self):
        self.stats = load_json(DEFAULT_STATS)

    def parse(self, *rows):
        if len(rows) < 2 or not rows[1].startswith("固定"):
            rows = (*HEADER, *rows)
        return parse_sub_stats([token(text, line) for line, text in enumerate(rows)], self.stats)

    def test_all_configured_types_and_tiers(self):
        tiers = self.stats["sub_stats"]["tiers"]
        self.assertEqual(len(tiers), 13)
        for stat_id, tier in tiers.items():
            definition = self.stats["stat_definitions"][stat_id]
            suffix = "%" if definition["unit"] == "percentage_points" else ""
            for value in tier["values"]:
                with self.subTest(stat_id=stat_id, value=value):
                    result = self.parse(f"{definition['name_zh']} + {value:g}{suffix}")
                    self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)
                    self.assertEqual((result.rows[0].stat_id, result.rows[0].value), (stat_id, value))
                    self.assertEqual(result.rows[0].issues, ())
                    self.assertEqual(validate_selection([SubStatEntry(stat_id, value)], self.stats),
                                     (SubStatEntry(stat_id, value),))

    def test_pairs_split_tokens_by_position_and_preserves_order(self):
        data = [token("暴击率 + 10.5%", -2), token("固定攻击 + 40", -1),
                token("＋１０．５％", 1, 200), token("暴击伤害", 0), token("暴 击 率", 1),
                token("＋２１．０％", 0, 200), token("固定攻击：４０", 2), token("共鸣效率 +12.4%", 3)]
        for count in range(1, 5):
            with self.subTest(count=count):
                result = parse_sub_stats([item for item in data if item.box[1] < count * 24], self.stats)
                self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                                 [("crit_dmg", 21), ("crit_rate", 10.5), ("atk_flat", 40),
                                  ("energy_regen", 12.4)][:count])
                self.assertEqual(result.blocking_issues, ())

    def test_ignores_mapped_main_and_minor_rows_without_matching_their_values(self):
        result = self.parse(
            "暴击率 + 10.5%", "固定攻击 + 40",
            "暴击伤害 + 21%", "共鸣效率 + 12.4%", "固定防御 + 50")
        self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                         [("crit_dmg", 21), ("energy_regen", 12.4), ("def_flat", 50)])
        self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)
        self.assertEqual(result.blocking_issues, ())
        self.assertEqual(result.cost, 4)
        self.assertEqual(result.main_stat.stat_id, "crit_rate")
        self.assertEqual(result.minor_stat.stat_id, "atk_flat")
        self.assertAlmostEqual(result.confidence, .98)
        self.assertEqual(result.raw_lines[:2], ("暴击率 + 10.5%", "固定攻击 + 40"))

    def test_main_and_minor_names_are_resolved_through_configured_stat_ids(self):
        result = self.parse(
            "攻击百分比 + 7.1%", "固定生命 + 320", "暴击率 + 10.5%")
        self.assertEqual([(row.stat_id, row.value) for row in result.rows], [("crit_rate", 10.5)])
        self.assertEqual(result.blocking_issues, ())
        self.assertEqual(result.cost, 1)

    def test_game_names_and_unique_missing_character_in_header_determine_cost(self):
        examples = (
            ("攻击 18%", "生命 2280", 1, "atk_pct", "hp_flat"),
            ("灭伤害加成 30%", "攻击 100", 3, "havoc_dmg_bonus", "atk_flat"),
            ("暴击伤害 44%", "攻击 150", 4, "crit_dmg", "atk_flat"),
            ("防御 25.8%", "攻击 68", 3, "def_pct", "atk_flat"),
        )
        for main, minor, cost, main_id, minor_id in examples:
            with self.subTest(main=main, minor=minor):
                lines = (main, minor, "暴击率 10.5%")
                result = parse_sub_stats([token(text, line) for line, text in enumerate(lines)], self.stats)
                self.assertEqual(result.blocking_issues, ())
                self.assertEqual((result.cost, result.main_stat.stat_id, result.minor_stat.stat_id),
                                 (cost, main_id, minor_id))

    def test_optional_game_metadata_does_not_shift_equipment_or_sub_stats(self):
        for prefix in ((), (GAME_LEVEL,), (GAME_NAME, GAME_LEVEL), (GAME_NAME,)):
            with self.subTest(prefix=prefix):
                lines = (*prefix, "签暴击伤害 22.8%", "X攻击 78", "防御 70", "攻击 40")
                result = parse_sub_stats(
                    [token(text, line) for line, text in reversed(list(enumerate(lines)))],
                    self.stats)
                self.assertEqual(result.cost, 4)
                self.assertEqual((result.main_stat.stat_id, result.main_stat.value),
                                 ("crit_dmg", 22.8))
                self.assertEqual((result.minor_stat.stat_id, result.minor_stat.value),
                                 ("atk_flat", 78))
                self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                                 [("def_flat", 70), ("atk_flat", 40)])
                self.assertEqual(result.raw_lines, lines)
                self.assertEqual(result.unmatched_text, ())
                self.assertEqual(result.blocking_issues, ())
                self.assertEqual(result.review_issues, ())

    def test_equipment_icons_can_be_separate_tokens_and_include_plus_markers(self):
        data = [
            token("签", 0), token("暴 击 伤 害", 0, 30), token("２２．８％", 0, 240),
            token("+ X", 1), token("攻击", 1, 30), token("７８", 1, 240),
            token("防御", 2, 30), token("70", 2, 240),
        ]
        result = parse_sub_stats(data, self.stats)
        self.assertEqual((result.main_stat.stat_id, result.main_stat.value), ("crit_dmg", 22.8))
        self.assertEqual((result.minor_stat.stat_id, result.minor_stat.value), ("atk_flat", 78))
        self.assertEqual(result.main_stat.issues, ())
        self.assertEqual(result.minor_stat.issues, ())
        self.assertEqual([(row.stat_id, row.value) for row in result.rows], [("def_flat", 70)])
        self.assertEqual(result.blocking_issues, ())

    def test_metadata_keeps_ambiguous_and_unrecognized_main_rows_for_correction(self):
        for main in ("伤害加成 30%", "无法辨认 30%"):
            with self.subTest(main=main):
                lines = (GAME_NAME, GAME_LEVEL, main, "攻击 100", "暴击率 10.5%")
                result = parse_sub_stats([token(text, line) for line, text in enumerate(lines)],
                                         self.stats)
                self.assertEqual(result.main_stat.raw_text, main)
                self.assertIsNone(result.main_stat.stat_id)
                self.assertEqual((result.minor_stat.stat_id, result.minor_stat.value),
                                 ("atk_flat", 100))
                self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                                 [("crit_rate", 10.5)])
                self.assertTrue(result.header_issues)

    def test_enhancement_prompts_are_unopened_slots_not_unknown_stat_values(self):
        prompts = ("＋ 强化至 ＋１５ 并激活辅音属性",
                   "强化至+I5并激活辅音属性", UNOPENED_ROWS[2])
        result = self.parse("防御 70", "攻击 40", *prompts)
        self.assertEqual(len(result.rows), 5)
        self.assertEqual([row.is_unopened for row in result.rows],
                         [False, False, True, True, True])
        self.assertEqual([row.raw_text for row in result.rows[2:]], list(prompts))
        self.assertTrue(all(row.stat_id is None and row.value is None and not row.issues
                            for row in result.rows[2:]))
        self.assertEqual(result.unmatched_text, ())
        self.assertEqual(result.review_issues, ())
        self.assertEqual(result.blocking_issues, ())
        self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)

    def test_unopened_slots_do_not_make_zero_open_stats_importable(self):
        result = self.parse(*(f"强化至+{level}并激活辅音属性"
                              for level in (5, 10, 15, 20, 25)))
        self.assertEqual(len(result.rows), 5)
        self.assertTrue(all(row.is_unopened for row in result.rows))
        self.assertEqual(result.completeness, Completeness.BLOCKED)
        self.assertTrue(any("零条" in issue for issue in result.blocking_issues))

    def test_slot_limit_counts_both_open_stats_and_unopened_slots(self):
        result = self.parse("暴击率 10.5%", "暴击伤害 21%", "防御 70", "攻击 40",
                            *UNOPENED_ROWS[1:])
        self.assertEqual(len(result.rows), 6)
        self.assertEqual(sum(row.is_unopened for row in result.rows), 2)
        self.assertEqual(result.completeness, Completeness.BLOCKED)
        self.assertTrue(any("6" in issue for issue in result.blocking_issues))

    def test_metadata_without_stat_rows_is_blocked(self):
        result = parse_sub_stats([token(GAME_NAME), token(GAME_LEVEL, 1)], self.stats)
        self.assertEqual(result.rows, ())
        self.assertEqual(result.completeness, Completeness.BLOCKED)

    def test_sub_stats_use_same_names_and_unique_missing_character_rule(self):
        result = self.parse("生命 +320", "生命 +7.1%", "击伤害 +21%")
        self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                         [("hp_flat", 320), ("hp_pct", 7.1), ("crit_dmg", 21)])

    def test_fuzzy_names_handle_multiple_missing_characters_and_misread_characters(self):
        for main in ("湮灭加成 30%", "湮灭 30%"):
            with self.subTest(main=main):
                lines = (main, "攻击 100", "暴击伤書 21%", "共鳴效率 12.4%")
                result = parse_sub_stats([token(text, line) for line, text in enumerate(lines)], self.stats)
                self.assertEqual(result.cost, 3)
                self.assertEqual(result.main_stat.stat_id, "havoc_dmg_bonus")
                self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                                 [("crit_dmg", 21), ("energy_regen", 12.4)])
                self.assertTrue(result.review_issues)

    def test_numeric_tiers_disambiguate_fuzzy_sub_stat_names(self):
        result = self.parse("暴擊 10.5%", "暴击伤書 21%", "伤害加成 8.6%")
        self.assertEqual([(row.stat_id, row.value) for row in result.rows[:2]],
                         [("crit_rate", 10.5), ("crit_dmg", 21)])
        self.assertIsNone(result.rows[2].stat_id)
        self.assertFalse(result.blocking_issues)

    def test_numeric_tier_can_override_a_closer_fuzzy_name(self):
        result = self.parse("暴击伤書 10.5%")
        self.assertEqual((result.rows[0].stat_id, result.rows[0].value), ("crit_rate", 10.5))
        self.assertTrue(result.rows[0].issues)

    def test_fuzzy_name_with_incompatible_value_does_not_guess(self):
        result = self.parse("共鸣解放伤害加 10.5%", "暴击率 105%")
        self.assertIsNone(result.rows[0].stat_id)
        self.assertEqual((result.rows[1].stat_id, result.rows[1].value), ("crit_rate", None))

    def test_header_name_ambiguity_can_use_intermediate_value(self):
        result = self.parse("伤害 8.8%", "固定攻击 30", "暴击率 10.5%")
        self.assertEqual(result.main_stat.stat_id, "crit_dmg")
        self.assertEqual(result.cost, 4)

    def test_cost_one_fuzzy_header_does_not_assume_unverified_upgrade_steps(self):
        result = self.parse("攻击 18%", "固定生 2000", "暴击率 10.5%")
        self.assertEqual((result.minor_stat.stat_id, result.cost), ("hp_flat", 1))

    def test_unrecognized_sub_stat_can_be_corrected_without_new_screenshot(self):
        result = self.parse("无法辨认+21%")
        self.assertEqual(len(result.rows), 1)
        self.assertIsNone(result.rows[0].stat_id)
        self.assertFalse(result.blocking_issues)

    def test_ambiguous_missing_character_and_missing_units_do_not_guess(self):
        for main in ("伤害加成 30%", "共鸣 30%"):
            lines = (main, "攻击 100", "暴击率 10.5%")
            result = parse_sub_stats([token(text, line) for line, text in enumerate(lines)], self.stats)
            self.assertIsNone(result.main_stat.stat_id)
            self.assertIsNone(result.cost)
        result = self.parse("生命", "攻击", "防御")
        self.assertTrue(all(row.stat_id is None for row in result.rows))

    def test_partial_name_is_rejected_if_two_configured_names_match(self):
        stats = copy.deepcopy(self.stats)
        stats["stat_definitions"]["spectro_dmg_bonus"]["name_zh"] = "衍灭伤害加成"
        lines = ("灭伤害加成 30%", "攻击 100", "暴击率 10.5%")
        result = parse_sub_stats([token(text, line) for line, text in enumerate(lines)], stats)
        self.assertIsNone(result.main_stat.stat_id)
        self.assertIsNone(result.cost)
        self.assertTrue(result.blocking_issues)

    def test_multiple_stats_on_one_line_are_blocked_with_partial_names(self):
        for line in ("暴击率+10.5% 击伤害+21%", "暴击率+10.5% 暴击伤害",
                     "暴击率+10.5% 伤害加成+21%"):
            with self.subTest(line=line):
                result = self.parse(line)
                self.assertIn("同一识别行含有多个词条，无法验证行数。请重新截图。",
                              result.blocking_issues)

    def test_detects_cost_without_requiring_intermediate_main_or_minor_values_to_be_max(self):
        result = self.parse(
            "气动伤害加成 + 15.6%", "固定攻击 + 36", "暴击率 +10.5%")
        self.assertEqual(result.cost, 3)
        self.assertEqual(result.main_stat.value, 15.6)
        self.assertEqual(result.minor_stat.value, 36)

    def test_shared_cost_three_and_four_are_disambiguated_by_linear_level_values(self):
        cost3 = self.parse("攻击百分比 +25.2%", "固定攻击 +84", "暴击率 +10.5%")
        cost4 = self.parse("攻击百分比 +27.7%", "固定攻击 +126", "暴击率 +10.5%")
        self.assertEqual(cost3.cost, 3)
        self.assertEqual(cost4.cost, 4)

    def test_linear_cost_progression_matches_each_five_level_step(self):
        cost3_main = ("6.0%", "10.8%", "15.6%", "20.4%", "25.2%", "30.0%")
        cost3_minor = ("20", "36", "52", "68", "84", "100")
        cost4_main = ("6.6%", "11.9%", "17.2%", "22.4%", "27.7%", "33.0%")
        cost4_minor = ("30", "54", "78", "102", "126", "150")
        for step in range(6):
            with self.subTest(cost=3, step=step):
                result = self.parse(f"攻击百分比 +{cost3_main[step]}",
                                    f"固定攻击 +{cost3_minor[step]}", "暴击率 +10.5%")
                self.assertEqual(result.cost, 3)
            with self.subTest(cost=4, step=step):
                result = self.parse(f"攻击百分比 +{cost4_main[step]}",
                                    f"固定攻击 +{cost4_minor[step]}", "暴击率 +10.5%")
                self.assertEqual(result.cost, 4)
        cost4_crit_dmg = self.parse("暴击伤害 +8.8%", "固定攻击 +30", "暴击率 +10.5%")
        self.assertEqual(cost4_crit_dmg.cost, 4)

    def test_ambiguous_shared_cost_three_and_four_is_blocked_without_matching_level(self):
        result = self.parse("攻击百分比 + 20%", "固定攻击 + 80", "暴击率 +10.5%")
        self.assertIsNone(result.cost)
        self.assertEqual(result.completeness, Completeness.BLOCKED)
        self.assertTrue(result.blocking_issues)

    def test_intermediate_value_above_lower_cost_max_resolves_shared_stats(self):
        result = self.parse("攻击百分比 +33%", "固定攻击 +150", "暴击率 +10.5%")
        self.assertEqual(result.cost, 4)
        self.assertEqual(result.main_stat.value, 33)
        self.assertEqual(result.minor_stat.value, 150)

    def test_fixed_and_percentage_types_require_a_unit(self):
        result = self.parse("生命 + 7.1％", "攻击 + 40", "固定防御 + 50%", "暴击率 +10.5")
        self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                         [("hp_pct", 7.1), ("atk_flat", 40), ("def_flat", None), ("crit_rate", None)])
        self.assertEqual(result.rows[1].issues, ())
        self.assertIn("单位", result.rows[2].issues[0])
        self.assertIn("单位", result.rows[3].issues[0])

    def test_short_names_select_matching_types_from_units(self):
        result = self.parse(
            "暴击 + 10.5%", "攻击 + 7.1%", "攻击 + 40", "防御 + 8.1%", "防御 + 40", "防御")
        self.assertEqual([(row.stat_id, row.value) for row in result.rows],
                         [("crit_rate", 10.5), ("atk_pct", 7.1), ("atk_flat", 40),
                          ("def_pct", 8.1), ("def_flat", 40), (None, None)])
        self.assertEqual(result.rows[0].issues, ())
        self.assertEqual(result.rows[1].issues, ())
        self.assertEqual(result.rows[2].issues, ())
        self.assertEqual(result.rows[3].issues, ())
        self.assertEqual(result.rows[4].issues, ())
        self.assertIn("百分比不明确", result.rows[5].issues[0])

    def test_retains_unmatched_text_and_does_not_snap_to_nearby_tier(self):
        result = self.parse("副词条", "暴击率 +105%", "无法辨认 +21.0%", "共鸣效率 +12.4001%", "未解锁")
        self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)
        self.assertEqual(result.unmatched_text, ("副词条", "无法辨认 +21.0%", "未解锁"))
        self.assertTrue(all(row.value is None for row in result.rows))
        self.assertIn("小数点", result.rows[0].issues[0])
        self.assertIsNone(result.rows[1].stat_id)
        self.assertEqual(len(result.raw_lines), 7)

    def test_does_not_pair_across_rows_and_requires_manual_completeness_check(self):
        result = self.parse("暴击率", "10.5%", "共鸣效率 +12.4%")
        self.assertIsNone(result.rows[0].value)
        self.assertIsNone(result.rows[1].stat_id)
        self.assertTrue(result.review_issues)

    def test_duplicate_is_correctable_but_five_and_non_target_are_blocked(self):
        result = self.parse("暴击率+10.5%", "暴击率+10.5%")
        self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)
        self.assertIn("重复", result.rows[1].issues[0])
        for rows in ((), ("未解锁",), ("主词条", "暴击率+10.5%"),
                     ("重构结果", "暴击率+10.5%"), ("暴击率+10.5% 暴击伤害+21%",),
                 ("暴击率+10.5%", "暴击伤害+21%", "共鸣效率+12.4%", "固定攻击+40",
                  "固定防御+50", "生命百分比+7.1%"),
                 ("暴击率+10.5%", "暴击伤害+21%", "共鸣效率+12.4%", "固定攻击+40",
                  "固定防御+50", "生命百分比误字+5O")):
            with self.subTest(rows=rows):
                result = self.parse(*rows)
                self.assertEqual(result.completeness, Completeness.BLOCKED)
                self.assertTrue(result.blocking_issues)
                if len(rows) == 6:
                    self.assertEqual(len(result.rows), 6)

    def test_five_sub_stats_are_valid_and_can_be_selected_as_full(self):
        rows = ("暴击率+10.5%", "暴击伤害+21%", "共鸣效率+12.4%",
            "固定攻击+40", "固定防御+50")
        result = self.parse(*rows)
        self.assertEqual(len(result.rows), 5)
        self.assertEqual(result.blocking_issues, ())
        self.assertEqual(len(validate_selection(
            [SubStatEntry(row.stat_id, row.value) for row in result.rows], self.stats)), 5)

    def test_validation_rejects_empty_five_duplicates_and_illegal_values(self):
        valid = SubStatEntry("crit_rate", 10.5)
        for entries in ([], [valid] * 5, [valid, valid], [SubStatEntry("healing_bonus", 10.5)],
                        [SubStatEntry("crit_rate", 10.50001)], [SubStatEntry("crit_rate", float("nan"))],
                        [SubStatEntry("crit_rate", True)]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                validate_selection(entries, self.stats)

    def test_engine_initializes_lazily_and_reuses_instance(self):
        engine = LocalOcrEngine()
        raw = [[[(2, 1), (9, 2), (9, 5), (1, 4)], "暴击率", .95]]
        with patch.object(engine, "_load") as load:
            load.return_value.return_value = (raw, None)
            load.assert_not_called()
            image = object()
            self.assertEqual(engine.recognize(image), [OcrToken("暴击率", (1., 1., 9., 5.), .95)])
            engine.recognize(image)
            load.assert_called_once_with()
            load.return_value.assert_called_with(image, use_det=True, use_cls=True, use_rec=True)

    def test_engine_load_failure_is_typed_and_can_be_retried(self):
        engine = LocalOcrEngine()
        with patch.object(engine, "_load", side_effect=[RuntimeError("broken model"), lambda *_a, **_kw: (None, None)]):
            with self.assertRaisesRegex(OcrError, "broken model") as error:
                engine.recognize(object())
            self.assertEqual(error.exception.category, "OCR 模型初始化失败")
            self.assertEqual(engine.recognize(object()), [])

    def test_optional_engine_missing_reports_dependency_error(self):
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": None}), \
                self.assertRaises(OcrError) as error:
            LocalOcrEngine().recognize(object())
        self.assertEqual(error.exception.category, "OCR 依赖不可用")


@unittest.skipUnless(find_spec("rapidocr_onnxruntime") is not None,
                     "实机截图回归需要可选 OCR 依赖")
class EchoOcrSampleTests(unittest.TestCase):
    def test_sample_with_optional_metadata_and_actual_local_engine(self):
        import cv2
        import numpy as np

        image = cv2.imread(str(Path(__file__).with_name("sample.png")))
        self.assertIsNotNone(image, "缺少或无法读取 tests/full/sample.png 实机截图")
        stats = load_json(DEFAULT_STATS)
        engine = LocalOcrEngine()
        for top in (0, 40, 80):
            with self.subTest(crop_top=top):
                result = parse_sub_stats(engine.recognize(np.ascontiguousarray(image[top:])), stats)
                self.assertEqual(result.cost, 4)
                self.assertEqual((result.main_stat.stat_id, result.main_stat.value),
                                 ("crit_dmg", 22.8))
                self.assertEqual((result.minor_stat.stat_id, result.minor_stat.value),
                                 ("atk_flat", 78))
                self.assertEqual([(row.stat_id, row.value) for row in result.rows
                                  if not row.is_unopened], [("def_flat", 70), ("atk_flat", 40)])
                self.assertEqual([row.is_unopened for row in result.rows],
                                 [False, False, True, True, True])
                self.assertEqual(result.unmatched_text, ())
                self.assertEqual(result.blocking_issues, ())
                self.assertEqual(result.review_issues, ())
                self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)


if __name__ == "__main__":
    unittest.main()
