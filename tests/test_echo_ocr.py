"""合成 OCR 文本与坐标，无需安装本地引擎。"""

import sys
import unittest
from unittest.mock import patch

from src.echo_ocr import (
    Completeness, LocalOcrEngine, OcrError, OcrToken, SubStatEntry, parse_sub_stats,
    validate_selection,
)
from src.echo_score import DEFAULT_STATS, load_json


def token(text, line=0, x=0, confidence=.98):
    return OcrToken(text, (x, line * 24, x + 70, line * 24 + 14), confidence)


HEADER = ("暴击率 + 10.5%", "固定攻击 + 40")


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
        result = self.parse("副词条", "暴击率 +105%", "暴击伤書 +21.0%", "共鸣效率 +12.4001%", "未解锁")
        self.assertEqual(result.completeness, Completeness.NEEDS_REVIEW)
        self.assertEqual(result.unmatched_text, ("副词条", "暴击伤書 +21.0%", "未解锁"))
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
        for rows in ((), ("未解锁",), ("暴击伤書+21%",), ("主词条", "暴击率+10.5%"),
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


if __name__ == "__main__":
    unittest.main()
