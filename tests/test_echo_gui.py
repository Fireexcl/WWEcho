"""离屏创建真实 Qt 控件并验证交互，不要求桌面显示服务。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import importlib.util
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, QThread, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QGridLayout, QHBoxLayout, QPushButton, QTabWidget
from src.echo_gui import EchoWindow, ScoreProgressBar
from src.echo_score import DEFAULT_STATS, EchoScorer, load_json


def strategy_result(reason="fake", *, unavailable=False):
    return {
        "zero": {"full_success_lower": .3, "full_success_upper": .4,
                 "success_probability": .25, "attempt_cost": 8,
                 "expected_cost": None if unavailable else 32,
                 "expected_embryos": None if unavailable else 4,
                 "expected_tuning": None if unavailable else 20,
                 "expected_experience": None if unavailable else 10},
        "current": {"full_success_lower": .1, "full_success_upper": .2,
                    "success_probability": .05, "recommend_continue": False,
                    "baseline_success_lower": .3, "reason": reason, "future_cost": 3},
        "score_resolution": .1, "method": "fake-grid",
    }


class ControlledPlanner:
    """可控构建/分析；每个测试独立重置，所有等待均有超时。"""

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.calls = []
        cls.active = 0
        cls.max_active = 0
        cls.gate = threading.Event()
        cls.gate.set()
        cls.build_gate = threading.Event()
        cls.build_gate.set()
        cls.error = None
        cls.unavailable = False

    def __init__(self, scorer, costs, target, **params):
        self.scorer, self.costs, self.target, self.params = scorer, costs, target, params
        self.thread = threading.get_ident()
        self.instances.append(self)
        if not self.build_gate.wait(5):
            raise RuntimeError("fake build timeout")

    def analyze(self, echo):
        cls = type(self)
        cls.active += 1
        cls.max_active = max(cls.max_active, cls.active)
        cls.calls.append((self, copy.deepcopy(echo), threading.get_ident()))
        try:
            if not cls.gate.wait(5):
                raise RuntimeError("fake analyze timeout")
            if cls.error:
                raise cls.error
            return strategy_result(f"target={self.target:g};count={len(echo['sub_stats'])}",
                                   unavailable=cls.unavailable)
        finally:
            cls.active -= 1


class EchoGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ControlledPlanner.reset()
        module = types.ModuleType("src.echo_strategy")
        module.ActivationPlanner = ControlledPlanner
        module.DEFAULT_COSTS = DEFAULT_STATS
        self.module_patch = patch.dict(sys.modules, {"src.echo_strategy": module})
        self.module_patch.start()
        self.window = EchoWindow()

    def tearDown(self):
        ControlledPlanner.gate.set()
        ControlledPlanner.build_gate.set()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.module_patch.stop()

    def wait_until(self, predicate, timeout=3000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), self.window.strategy_status.text())

    def choose_example(self):
        stat_ids = ["crit_rate", "crit_dmg", "atk_pct", "energy_regen", "basic_atk_dmg_bonus"]
        for stat_id in list(self.window.selected_sub_stats):
            if stat_id not in stat_ids:
                self.window.sub_stat_buttons[stat_id].click()
        for stat_id in stat_ids:
            button = self.window.sub_stat_buttons[stat_id]
            if not button.isChecked():
                button.click()
            tier = self.window.sub_tier_inputs[stat_id]
            tier.setCurrentIndex(tier.count() - 1)
        self.window.main_stat.setCurrentIndex(self.window.main_stat.findData("crit_rate"))

    def test_example_live_score_and_target(self):
        self.choose_example()
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")
        self.window.target.setValue(20)
        self.assertIn("已达目标", self.window.summary.text())
        self.window.target.setValue(50)
        self.assertIn(f"还差 {50 - expected:g} 分", self.window.summary.text())
        self.assertEqual(self.window.details.rowCount(), 7)
        self.window.target.setValue(0)
        self.assertEqual(self.window.progress.value(), int(expected / 50 * 1000))

    def test_score_results_directly_below_target_before_strategy_parameters(self):
        layout = self.window.score_label.parentWidget().layout()
        self.assertIs(layout.itemAt(0).layout().itemAt(1).widget(), self.window.target)
        self.assertIs(layout.itemAt(1).layout().itemAt(0).widget(), self.window.score_label)
        self.assertIs(layout.itemAt(2).widget(), self.window.progress_status)
        self.assertIs(layout.itemAt(3).widget(), self.window.progress)
        self.assertIs(layout.itemAt(5).widget(), self.window.details)
        self.assertIs(layout.itemAt(6).widget(), self.window.cost_divider)
        self.assertEqual(layout.itemAt(7).widget().text(), "成本估算相关参数")
        self.assertIs(layout.itemAt(8).layout(), self.window.cost_parameters_layout)

    def test_cost_parameters_use_four_equal_columns(self):
        grid = self.window.cost_parameters_layout
        for column, box in enumerate(self.window.strategy_inputs.values()):
            self.assertIs(grid.itemAtPosition(1, column).widget(), box)
            self.assertEqual(grid.columnStretch(column), 1)
            self.assertIsNotNone(grid.itemAtPosition(0, column).widget())
        self.assertEqual(grid.columnCount(), 4)

    def test_weight_source_default_import_edit_restore_and_unrelated_changes(self):
        label = self.window.weight_source_label
        self.assertEqual(label.text(), "默认-爱弥斯-企鹅bot")
        layout = self.window.weight_tabs.parentWidget().layout()
        self.assertEqual(layout.indexOf(label) + 1, layout.indexOf(self.window.weight_tabs))
        self.window.target.setValue(41)
        self.window.cost.setCurrentIndex(0)
        self.window.strategy_inputs["recovery"].setValue(.75)
        self.assertEqual(label.text(), "默认-爱弥斯-企鹅bot")
        for inputs in self.window.weight_inputs.values():
            self.window.restore_weights()
            box = next(iter(inputs.values()))
            box.setValue(box.value() + 1)
            self.assertEqual(label.text(), "自定义权重")
        preset = self.window.current_weights()
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/角色权重.json", "")), \
                patch("src.echo_gui.load_json", return_value=preset):
            self.window.import_weights()
        self.assertEqual(label.text(), "导入-角色权重.json")
        self.window.clear_sub_stats()
        self.assertEqual(label.text(), "导入-角色权重.json")
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("", "")):
            self.window.import_weights()
        self.assertEqual(label.text(), "导入-角色权重.json")
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/bad.json", "")), \
                patch("src.echo_gui.load_json", side_effect=ValueError("bad preset")), \
                patch("src.echo_gui.QMessageBox.warning"):
            self.window.import_weights()
        self.assertEqual(label.text(), "导入-角色权重.json")
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        box.setValue(box.value() + 1)
        self.assertEqual(label.text(), "自定义权重")
        self.window.restore_weights()
        self.assertEqual(label.text(), "默认-爱弥斯-企鹅bot")

    def test_stats_divider_and_clear_button_style(self):
        layout = self.window.stats_divider.parentWidget().layout()
        self.assertIs(layout.itemAt(1).widget(), self.window.stats_divider)
        self.assertIs(layout.itemAt(2).layout(), self.window.sub_options_layout)
        self.assertEqual(self.window.clear_sub_stats_button.objectName(), "clearSelection")
        self.assertEqual(self.window.width(), 980)

    def test_strategy_metric_cards_match_results_and_clear_pending_values(self):
        self.window._strategy_timer.stop()
        self.window._display_strategy(strategy_result())
        metrics = self.window.strategy_metrics
        self.assertEqual(metrics["zero_probability"].text(), "25.00%")
        self.assertEqual(metrics["zero_cost"].text(), "32")
        self.assertEqual(metrics["current_probability"].text(), "10.00%")
        self.assertEqual(metrics["future_cost"].text(), "3")
        self.window._display_strategy(strategy_result(unavailable=True))
        self.assertEqual(metrics["zero_cost"].text(), "不可达 / ∞")
        self.assertEqual(metrics["zero_cost"].property("tone"), "muted")
        self.window.target.setValue(41)
        self.assertTrue(all(label.text() == "—" for label in metrics.values()))

    def test_score_table_highlights_contributions_and_aligns_numbers(self):
        self.assertTrue(self.window.details.alternatingRowColors())
        self.assertFalse(self.window.details.showGrid())
        for row in range(self.window.details.rowCount()):
            cell = self.window.details.item(row, 4)
            self.assertTrue(cell.font().bold())
            self.assertTrue(cell.textAlignment() & Qt.AlignmentFlag.AlignRight)
            self.assertEqual(cell.foreground().color().name(), "#2456a6")

    def test_minimum_window_width_does_not_require_page_horizontal_scroll(self):
        self.window.resize(760, 940)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.centralWidget().horizontalScrollBar().maximum(), 0)

    def test_strategy_results_share_one_row_with_equal_columns(self):
        layout = self.window.score_label.parentWidget().layout()
        results = layout.itemAt(10).layout()
        self.assertIsInstance(results, QHBoxLayout)
        self.assertEqual(results.count(), 2)
        self.assertIs(results.itemAt(0).widget(), self.window.zero_strategy.parentWidget())
        self.assertIs(results.itemAt(1).widget(), self.window.current_strategy.parentWidget())
        self.assertEqual(results.stretch(0), 1)
        self.assertEqual(results.stretch(1), 1)

    def test_progress_uses_fixed_fifty_point_scale_when_target_changes(self):
        expected = int(self.window._score_result["score"] / 50 * 1000)
        for target in (0, 20, 40, 50, 87):
            self.window.target.setValue(target)
            self.assertEqual(self.window.progress.value(), expected)
            self.assertEqual(self.window.progress.target_score, target)
            self.assertEqual(self.window.progress_target_label.text(), f"│ 目标 {target:g} 分")

    def test_target_marker_position_resize_and_visible_over_full_bar(self):
        progress = ScoreProgressBar()
        try:
            progress.show()
            for width in (202, 402):
                progress.resize(width, 18)
                for target in (0, 20, 40, 50, 87):
                    progress.set_target_score(target)
                    self.app.processEvents()
                    marker = progress.target_marker
                    expected_x = round((width - 2) * min(target / 50, 1))
                    self.assertEqual(marker.geometry().getRect(), (expected_x, 0, 2, 18))
                    progress.setValue(1000)
                    image = progress.grab().toImage()
                    self.assertEqual(image.pixelColor(expected_x, 9).name(), "#243247")
        finally:
            progress.close()
            progress.deleteLater()

    def test_progress_recommendation_colors_and_pending_reset(self):
        self.window._strategy_timer.stop()
        result = strategy_result()
        for recommend, text, color in [(True, "推荐继续", "#16a34a"),
                                       (False, "推荐放弃", "#dc2626")]:
            result["current"]["recommend_continue"] = recommend
            self.window._display_strategy(result)
            self.assertEqual(self.window.progress_status.text(), text)
            self.assertIn(color, self.window.progress.styleSheet())
            self.assertIn(color, self.window.progress_status.styleSheet())
        self.window.target.setValue(41)
        self.assertEqual(self.window.progress_status.text(), "推荐计算中")
        self.assertIn("#94a3b8", self.window.progress.styleSheet())

    def test_five_substats_final_status_overrides_strategy_recommendation(self):
        self.choose_example()
        score = self.window._score_result["score"]
        result = strategy_result()
        for target, text, color in [(int(score), "达成目标", "#16a34a"),
                                    (50, "下次好运", "#dc2626")]:
            self.window.target.setValue(target)
            self.assertEqual(self.window.progress_status.text(), text)
            for recommend in (True, False):
                result["current"]["recommend_continue"] = recommend
                self.window._display_strategy(result)
                self.assertEqual(self.window.progress_status.text(), text)
                self.assertIn(color, self.window.progress.styleSheet())
        self.window.clear_sub_stats()
        self.assertEqual(self.window.progress_status.text(), "推荐计算中")
        self.window.target.setValue(0)
        self.assertEqual(self.window.progress_status.text(), "达成目标")

    def test_weight_edit_and_restore(self):
        self.choose_example()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(30)
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")
        self.window.restore_weights()
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")

    def test_weight_tab_order_and_layout(self):
        tabs = self.window.findChild(QTabWidget)
        self.assertEqual([tabs.tabText(i) for i in range(tabs.count())],
                         ["副词条权重", "Cost 1 主词条权重", "Cost 3 主词条权重", "Cost 4 主词条权重", "小词条权重"])
        self.assertEqual(tabs.currentIndex(), 0)
        for tab_index, category, pairs in [(0, "sub_stats", 2), (1, "main_stats_1", 2),
                                           (2, "main_stats_3", 2), (3, "main_stats_4", 2)]:
            table = tabs.widget(tab_index)
            inputs = self.window.weight_inputs[category]
            self.assertEqual(table.columnCount(), pairs * 2)
            self.assertEqual(table.rowCount(), (len(inputs) + pairs - 1) // pairs)
            self.assertTrue(table.horizontalHeader().isHidden())
            self.assertEqual(table.verticalScrollBar().maximum(), 0)
            self.assertGreaterEqual(table.height(), table.rowCount() * 40)
            for index, (stat_id, box) in enumerate(inputs.items()):
                row, pair = divmod(index, pairs)
                self.assertEqual(table.item(row, pair * 2).text(),
                                 self.window.stats["stat_definitions"][stat_id]["name_zh"])
                self.assertIs(table.cellWidget(row, pair * 2 + 1), box)
            if len(inputs) % pairs:
                self.assertIsNone(table.cellWidget(table.rowCount() - 1, 3))

    def test_weights_disable_stepping_but_allow_manual_input(self):
        self.choose_example()
        for inputs in self.window.weight_inputs.values():
            for box in inputs.values():
                self.assertEqual(box.buttonSymbols(), QDoubleSpinBox.ButtonSymbols.NoButtons)
                original = box.value()
                box.stepUp()
                box.stepDown()
                for key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                    QTest.keyClick(box, key)
                wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, 120),
                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.NoScrollPhase, False)
                QApplication.sendEvent(box, wheel)
                self.assertEqual(box.value(), original)
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        box.setFocus()
        box.selectAll()
        QTest.keyClicks(box, "30.1256")
        QTest.keyClick(box, Qt.Key.Key_Return)
        self.assertEqual(box.value(), 30.1256)
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")
        self.window.target.stepUp()
        self.assertEqual(self.window.target.value(), 31)

    def test_minor_weights_tab_roundtrip_restore_and_old_preset(self):
        table = self.window.weight_tabs.widget(4)
        self.assertEqual(table.rowCount(), 2)
        self.assertEqual(table.columnCount(), 4)
        labels = [table.item(0, 0).text(), table.item(0, 2).text(), table.item(1, 0).text()]
        self.assertEqual(labels, ["Cost 4 · 固定攻击", "Cost 3 · 固定攻击", "Cost 1 · 固定生命"])
        self.assertEqual(set(self.window.weight_inputs["minor_stats"]), {"1", "3", "4"})
        previous_score = self.window.score_label.text()
        for cost, value in [("4", .12), ("3", .1), ("1", .01)]:
            self.window.weight_inputs["minor_stats"][cost].setValue(value)
        saved = self.window.current_weights()
        self.assertEqual(saved["minor_stats"], {"4": {"atk_flat": .12},
                                               "3": {"atk_flat": .1}, "1": {"hp_flat": .01}})
        self.assertNotEqual(self.window.score_label.text(), previous_score)
        self.window.restore_weights()
        self.assertEqual(self.window.current_weights()["minor_stats"], self.window.defaults["minor_stats"])
        self.window.apply_weights(saved)
        self.assertEqual(self.window.current_weights(), saved)
        invalid = copy.deepcopy(saved)
        invalid["minor_stats"]["4"]["atk_flat"] = 2_000_000
        with self.assertRaises(ValueError):
            self.window.apply_weights(invalid)
        self.assertEqual(self.window.current_weights(), saved)
        del saved["minor_stats"]
        self.window.apply_weights(saved)
        self.assertTrue(all(box.value() == 0 for box in self.window.weight_inputs["minor_stats"].values()))

    def test_cost_refreshes_main_and_keeps_selected_buttons(self):
        self.choose_example()
        self.window.cost.setCurrentIndex(0)
        self.assertEqual(self.window.main_stat.count(), 3)
        self.assertEqual(self.window.main_stat.currentData(), "atk_pct")
        self.window.sub_stat_buttons["crit_dmg"].click()
        self.assertEqual(len(self.window.current_echo()["sub_stats"]), 4)

    def test_each_cost_resets_default_main_stat(self):
        self.assertEqual(self.window.current_echo()["main_stat"], {"name": "crit_rate", "value": 22})
        for cost, name, value in [(1, "atk_pct", 18), (3, "atk_pct", 30),
                                   (4, "crit_rate", 22), (1, "atk_pct", 18)]:
            self.window.main_stat.setCurrentIndex(self.window.main_stat.findData("hp_pct"))
            self.window.cost.setCurrentIndex(self.window.cost.findData(cost))
            self.assertEqual(self.window.current_echo()["main_stat"], {"name": name, "value": value})

    def test_minor_stat_follows_cost_and_contributes_to_total(self):
        for cost, name, value in [(1, "固定生命", 2280), (3, "固定攻击", 100), (4, "固定攻击", 150)]:
            self.window.weight_inputs["minor_stats"][str(cost)].setValue(.1)
            self.window.cost.setCurrentIndex(self.window.cost.findData(cost))
            self.assertEqual(self.window.minor_stat_label.text(), name)
            self.assertIn(str(value), self.window.minor_value.text())
            result = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())
            self.assertGreater(result["minor_score"], 0)
            self.assertAlmostEqual(result["score"], result["main_score"] + result["minor_score"], places=5)
            self.assertEqual(self.window.details.item(1, 0).text(), "小词条")
            self.assertEqual(self.window.details.item(1, 2).text(), str(value))
            self.assertIn(f"小词条 {result['minor_score']:g} 分", self.window.summary.text())

    def test_cost_weights_independent_roundtrip_and_legacy_import(self):
        self.window.weight_inputs["main_stats_1"]["atk_pct"].setValue(1)
        self.window.weight_inputs["main_stats_3"]["atk_pct"].setValue(3)
        self.window.weight_inputs["main_stats_4"]["atk_pct"].setValue(4)
        saved = self.window.current_weights()
        self.window.restore_weights()
        self.window.apply_weights(saved)
        self.assertEqual(self.window.current_weights(), saved)
        for cost in (1, 3, 4):
            self.window.cost.setCurrentIndex(self.window.cost.findData(cost))
            self.window.main_stat.setCurrentIndex(self.window.main_stat.findData("atk_pct"))
            scored = EchoScorer(self.window.stats, saved).score(self.window.current_echo())
            self.assertEqual(scored["main_stat"]["weight"], cost)
            self.assertEqual(self.window.score_label.text(), f"{scored['score']:g} 分")
        from src.echo_score import DEFAULT_WEIGHTS
        legacy = load_json(DEFAULT_WEIGHTS)
        legacy["main_stats"] = {name: value for pool in legacy["main_stats"].values()
                    for name, value in pool.items()}
        self.window.apply_weights(legacy)
        for cost in ("1", "3", "4"):
            self.assertEqual(self.window.current_weights()["main_stats"][cost]["atk_pct"],
                             legacy["main_stats"]["atk_pct"])

    def test_all_sub_types_have_correct_tiers(self):
        for stat_id, data in self.window.stats["sub_stats"]["tiers"].items():
            tier = self.window.sub_tier_inputs[stat_id]
            self.assertEqual([tier.itemData(i) for i in range(tier.count())], data["values"])

    def test_sub_stat_options_use_three_columns_with_adjacent_tiers(self):
        self.assertEqual(len(self.window.sub_stat_buttons), 13)
        self.assertTrue(all(isinstance(button, QPushButton) and button.isCheckable()
                            for button in self.window.sub_stat_buttons.values()))
        for stat_id, button in self.window.sub_stat_buttons.items():
            self.assertIs(button.parentWidget(), self.window.sub_tier_inputs[stat_id].parentWidget())
            self.assertEqual(self.window.sub_options_layout.indexOf(button.parentWidget()) >= 0, True)
        coordinates = [self.window.sub_options_layout.getItemPosition(
            self.window.sub_options_layout.indexOf(button.parentWidget()))[:2]
            for button in self.window.sub_stat_buttons.values()]
        self.assertEqual(max(column for _, column in coordinates), 2)
        self.assertEqual(len(set(row for row, _ in coordinates)), 5)

    def test_clear_button_is_next_to_selected_count(self):
        layouts = self.window.findChildren(QGridLayout)
        header = next(grid for grid in layouts if grid.itemAtPosition(0, 3) is not None
                      and grid.itemAtPosition(0, 3).layout() is not None
                      and grid.itemAtPosition(0, 3).layout().indexOf(self.window.selected_count) >= 0)
        toolbar = header.itemAtPosition(0, 3).layout()
        self.assertEqual(toolbar.indexOf(self.window.clear_sub_stats_button),
                         toolbar.indexOf(self.window.selected_count) + 1)

    def test_theoretical_max_tracks_weights_and_not_selected_echo(self):
        weights = self.window.current_weights()
        for pool in weights["minor_stats"].values():
            for name in pool:
                pool[name] = 0
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        for cost, pool in weights["main_stats"].items():
            weights["main_stats"][cost] = dict.fromkeys(pool, 0)
        weights["main_stats"]["4"]["healing_bonus"] = 25
        for stat_id, value in [("crit_rate", 20), ("crit_dmg", 15), ("atk_pct", 10),
                               ("energy_regen", 5), ("atk_flat", 2.5), ("hp_flat", 1)]:
            weights["sub_stats"][stat_id] = value
        self.window.apply_weights(weights)
        self.assertEqual(self.window.theoretical_max_label.text(), "理论最高分：50 分（未对齐：2021）")
        self.window.cost.setCurrentIndex(0)
        self.window.clear_sub_stats_button.click()
        self.assertEqual(self.window.theoretical_max_label.text(), "理论最高分：50 分（未对齐：2021）")
        self.window.weight_inputs["sub_stats"]["hp_flat"].setValue(3)
        self.assertEqual(self.window.theoretical_max_label.text(), "理论最高分：50 分（未对齐：3181）")
        for inputs in self.window.weight_inputs.values():
            for box in inputs.values():
                box.setValue(0)
        self.assertEqual(self.window.theoretical_max_label.text(), "理论最高分：0 分（未对齐：0）")
        self.assertEqual(self.window.score_label.text(), "0 分")

    def test_sub_stat_options_limit_selection_and_remove_directly(self):
        self.choose_example()
        self.assertEqual(len(self.window.current_echo()["sub_stats"]), 5)
        sixth = next(stat_id for stat_id, button in self.window.sub_stat_buttons.items()
                     if not button.isChecked())
        self.window.sub_stat_buttons[sixth].click()
        self.assertFalse(self.window.sub_stat_buttons[sixth].isChecked())
        self.assertEqual(len(self.window.current_echo()["sub_stats"]), 5)
        selected = self.window.selected_sub_stats[1]
        self.window.sub_stat_buttons[selected].click()
        self.assertNotIn(selected, [item["name"] for item in self.window.current_echo()["sub_stats"]])
        self.assertEqual(len(self.window.current_echo()["sub_stats"]), 4)

    def test_checking_expands_tier_dropdown_and_clear_preserves_tier_values(self):
        self.window.clear_sub_stats_button.click()
        stat_id = "crit_rate"
        tier = self.window.sub_tier_inputs[stat_id]
        saved_value = tier.itemData(tier.count() - 1)
        self.window.sub_stat_buttons[stat_id].click()
        QTest.qWait(30)
        self.assertTrue(tier.isEnabled())
        self.assertTrue(tier.view().isVisible())
        tier.setCurrentIndex(tier.count() - 1)
        self.window.clear_sub_stats_button.click()
        self.assertEqual(self.window.selected_sub_stats, [])
        self.assertEqual(self.window.selected_count.text(), "0 / 5")
        self.assertFalse(tier.isEnabled())
        self.assertEqual(tier.currentData(), saved_value)
        self.window.sub_stat_buttons[stat_id].click()
        self.assertEqual(tier.currentData(), saved_value)

    def test_invalid_import_is_atomic_and_valid_roundtrips(self):
        original = self.window.current_weights()
        invalid = copy.deepcopy(original)
        invalid["main_stats"]["1"]["hp_pct"] = 99
        invalid["sub_stats"]["crit_rate"] = 2_000_000
        with self.assertRaises(ValueError):
            self.window.apply_weights(invalid)
        self.assertEqual(self.window.current_weights(), original)
        original["sub_stats"]["crit_rate"] = 12.3456
        self.window.apply_weights(original)
        self.assertEqual(self.window.current_weights(), original)

    def test_tier_binding_survives_middle_removal_and_reselection(self):
        self.choose_example()
        saved = {item["name"]: item["value"] for item in self.window.current_echo()["sub_stats"]}
        saved_inputs = dict(self.window.sub_tier_inputs)
        self.window.sub_stat_buttons["crit_dmg"].click()
        self.assertNotIn("crit_dmg", [item["name"] for item in self.window.current_echo()["sub_stats"]])
        self.assertFalse(saved_inputs["crit_dmg"].isEnabled())
        self.assertEqual(saved_inputs["crit_dmg"].currentData(), saved["crit_dmg"])
        tier = self.window.sub_tier_inputs["atk_pct"]
        tier.setCurrentIndex(0)
        echo = {item["name"]: item["value"] for item in self.window.current_echo()["sub_stats"]}
        self.assertEqual(echo["atk_pct"], tier.itemData(0))
        self.assertEqual(echo["energy_regen"], saved["energy_regen"])
        self.window.sub_stat_buttons["crit_dmg"].click()
        self.assertEqual(self.window.sub_tier_inputs["atk_pct"].currentIndex(), 0)
        self.assertEqual(self.window.sub_tier_inputs["crit_dmg"].currentData(), saved["crit_dmg"])
        # 档位绑定词条而非选中顺序：取消/重选只切换启用状态，不重置档位。
        expected = {**saved, "atk_pct": tier.itemData(0)}
        self.assertEqual({item["name"]: item["value"]
                          for item in self.window.current_echo()["sub_stats"]}, expected)
        for stat_id, original_input in saved_inputs.items():
            self.assertIs(self.window.sub_tier_inputs[stat_id], original_input)
        for stat_id in self.window.selected_sub_stats:
            self.assertTrue(self.window.sub_tier_inputs[stat_id].isEnabled())
        self.assertNotEqual(self.window.score_label.text(), "—")

    def test_zero_to_five_substats_score_and_strategy(self):
        self.assertEqual(len(self.window.selected_sub_stats), 0)
        self.assertTrue(all(not button.isChecked() for button in self.window.sub_stat_buttons.values()))
        self.assertTrue(all(not tier.isEnabled() for tier in self.window.sub_tier_inputs.values()))
        self.assertEqual(self.window.current_echo()["sub_stats"], [])
        self.assertIn("已激活 0/5", self.window.summary.text())
        self.assertEqual(self.window.details.rowCount(), 2)
        self.wait_until(lambda: "count=0" in self.window.current_strategy.text())
        self.choose_example()
        self.wait_until(lambda: "count=5" in self.window.current_strategy.text())

    def test_strategy_defaults_ranges_manual_input_and_parameter_forwarding(self):
        self.assertEqual(self.window.target.value(), 30)
        expected = {"recovery": (.66, 1),
                    "tuning_weight": (1, 1_000_000), "experience_weight": (1, 1_000_000),
                    "embryo_weight": (1, 1_000_000)}
        for name, (value, maximum) in expected.items():
            box = self.window.strategy_inputs[name]
            self.assertEqual((box.value(), box.minimum(), box.maximum()), (value, 0, maximum))
            self.assertEqual(box.buttonSymbols(), QDoubleSpinBox.ButtonSymbols.NoButtons)
            box.stepUp()
            self.assertEqual(box.value(), value)
        recovery = self.window.strategy_inputs["recovery"]
        recovery.selectAll()
        QTest.keyClicks(recovery, "0.75")
        QTest.keyClick(recovery, Qt.Key.Key_Return)
        for name, value in [("tuning_weight", 2),
                            ("experience_weight", 3), ("embryo_weight", 4)]:
            self.window.strategy_inputs[name].setValue(value)
        self.window.target.setValue(87)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        planner = ControlledPlanner.instances[-1]
        self.assertEqual(planner.params, {"recovery": .75,
                                         "tuning_weight": 2, "experience_weight": 3,
                                         "embryo_weight": 4, "bins": 1000})
        self.assertEqual(planner.target, 87)
        self.assertEqual(planner.costs, load_json(DEFAULT_STATS))
        self.assertEqual(planner.scorer.weights, {category: self.window.current_weights()[category]
                             for category in ("main_stats", "sub_stats")})
        self.assertEqual(ControlledPlanner.calls[-1][1], self.window.current_echo())
        self.assertNotEqual(planner.thread, threading.get_ident())

    def test_async_build_does_not_block_score_and_updates_widgets_on_main_thread(self):
        ControlledPlanner.build_gate.clear()
        threads = []
        display = self.window._display_strategy

        def record_display(result):
            threads.append(QThread.currentThread())
            display(result)

        self.window._display_strategy = record_display
        self.wait_until(lambda: bool(ControlledPlanner.instances))
        self.assertEqual(self.window.current_strategy.text(), "—")
        self.choose_example()
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")
        ControlledPlanner.build_gate.set()
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertEqual(threads, [self.app.thread()])
        self.assertTrue(all(call[2] != threading.get_ident() for call in ControlledPlanner.calls))

    def test_debounce_single_task_stale_result_rejection_and_latest_input(self):
        ControlledPlanner.gate.clear()
        rendered = []
        display = self.window._display_strategy

        def record_display(result):
            rendered.append(result["current"]["reason"])
            display(result)

        self.window._display_strategy = record_display
        self.wait_until(lambda: len(ControlledPlanner.calls) == 1)
        for value in (81, 82, 83, 84, 85):
            self.window.target.setValue(value)
        QTest.qWait(250)
        self.assertEqual(len(ControlledPlanner.calls), 1)
        self.assertEqual(self.window.current_strategy.text(), "—")
        self.assertEqual(self.window.progress_status.text(), "推荐计算中")
        ControlledPlanner.gate.set()
        self.wait_until(lambda: "target=85" in self.window.current_strategy.text())
        self.assertEqual(rendered, ["target=85;count=0"])
        self.assertEqual(self.window.progress_status.text(), "推荐放弃")
        self.assertEqual(len(ControlledPlanner.calls), 2)
        self.assertEqual(ControlledPlanner.max_active, 1)
        self.assertEqual([planner.target for planner in ControlledPlanner.instances], [30, 85])

    def test_debounce_and_planner_cache_reused_for_new_echo(self):
        for value in (81, 82, 83):
            self.window.target.setValue(value)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertEqual(len(ControlledPlanner.calls), 1)
        self.assertEqual(len(ControlledPlanner.instances), 1)
        self.window.sub_stat_buttons["crit_rate"].click()
        tier = self.window.sub_tier_inputs["crit_rate"]
        tier.setCurrentIndex(tier.count() - 1)
        self.wait_until(lambda: len(ControlledPlanner.calls) == 2 and
                        "策略已更新" in self.window.strategy_status.text())
        self.assertEqual(len(ControlledPlanner.instances), 1)
        self.assertEqual(ControlledPlanner.calls[-1][1], self.window.current_echo())
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(21)
        self.wait_until(lambda: len(ControlledPlanner.instances) == 2)

    def test_result_labels_distinguish_probabilities_and_unavailable_costs(self):
        ControlledPlanner.unavailable = True
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        for label in (self.window.zero_strategy, self.window.current_strategy):
            self.assertIn("完整激活概率区间", label.text())
            self.assertIn("相对基准策略成功率（近似）", label.text())
        self.assertIn("30.00% ～ 40.00%", self.window.zero_strategy.text())
        self.assertIn("25.00%", self.window.zero_strategy.text())
        self.assertEqual(self.window.zero_strategy.text().count("无法获得 / ∞"), 4)
        self.assertIn("建议停止", self.window.current_strategy.text())
        self.assertIn("0 词条达标概率下界基准", self.window.current_strategy.text())
        self.assertIn("非成本最优", self.window.current_strategy.text())
        for text in ("分数网格近似", "0 词条基准", "占位", "失败经验", "调谐和胚子无回收"):
            self.assertIn(text, self.window.result_hint.text())

    def test_strategy_error_is_visible_without_affecting_scoring(self):
        ControlledPlanner.error = ValueError("controlled failure")
        self.choose_example()
        self.wait_until(lambda: "controlled failure" in self.window.strategy_status.text())
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")
        self.assertEqual(self.window.zero_strategy.text(), "—")

    def test_parameter_boundaries_and_zero_cost_weights(self):
        for box in self.window.strategy_inputs.values():
            box.setValue(-1)
            self.assertEqual(box.value(), 0)
        self.window.strategy_inputs["recovery"].setValue(2)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertEqual(ControlledPlanner.instances[-1].params, {
            "recovery": 1, "tuning_weight": 0,
            "experience_weight": 0, "embryo_weight": 0, "bins": 1000,
        })

    def test_stale_error_and_invalid_input_do_not_restore_old_result(self):
        ControlledPlanner.gate.clear()
        self.wait_until(lambda: len(ControlledPlanner.calls) == 1)
        # 制造评分不合法的新快照，旧任务的错误和成功均不得覆盖输入错误。
        stat_id = "crit_rate"
        self.window.sub_stat_buttons[stat_id].click()
        tier = self.window.sub_tier_inputs[stat_id]
        tier.addItem("invalid", -1)
        tier.setCurrentIndex(tier.count() - 1)
        ControlledPlanner.error = ValueError("stale failure")
        ControlledPlanner.gate.set()
        self.wait_until(lambda: self.window._strategy_task is None)
        self.assertEqual(self.window.strategy_status.text(), "策略输入无效")
        self.assertEqual(self.window.zero_strategy.text(), "—")
        self.assertIn("输入有误", self.window.summary.text())
        self.assertEqual(self.window.progress_status.text(), "输入有误")
        self.assertEqual(len(ControlledPlanner.calls), 1)
        ControlledPlanner.error = None
        tier.setCurrentIndex(0)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())

    def test_close_with_pending_and_running_task_drains_pool(self):
        ControlledPlanner.gate.clear()
        self.wait_until(lambda: len(ControlledPlanner.calls) == 1)
        self.window.target.setValue(99)
        # 非 GUI 线程释放 fake，验证 close 等待退出且不启动待处理输入。
        release = threading.Timer(.05, ControlledPlanner.gate.set)
        release.start()
        try:
            self.window.close()
        finally:
            release.join()
        self.app.processEvents()
        self.assertEqual(self.window._strategy_pool.activeThreadCount(), 0)
        self.assertIsNone(self.window._strategy_pending)
        self.assertEqual(len(ControlledPlanner.calls), 1)
        self.assertNotIn("策略已更新", self.window.strategy_status.text())

    def test_close_before_debounce_does_not_start_task(self):
        self.window.close()
        QTest.qWait(250)
        self.assertEqual(ControlledPlanner.calls, [])


class EchoGuiRealStrategyTests(unittest.TestCase):
    """真实默认精度快速路径及低精度 DP；不将 GUI 测试变成性能测试。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_strategy(self, window, timeout=10):
        deadline = time.monotonic() + timeout
        yield_to_worker = threading.Event()
        while "策略已更新" not in window.strategy_status.text() and time.monotonic() < deadline:
            if "失败" in window.strategy_status.text() or "有误" in window.strategy_status.text():
                break
            self.app.processEvents()
            # qWait 紧密循环会拖慢后台首次导入 NumPy；显式释放 GIL，
            # 同时持续派发 Qt 信号，不预导入或替换默认真实 planner。
            yield_to_worker.wait(.01)
        self.assertIn("策略已更新", window.strategy_status.text())

    def assert_results_match_api(self, window, result):
        """核对所有策略展示字段和评分明细，避免 fake 掩盖 API 字段错配。"""
        def amount(value):
            return "无法获得 / ∞" if value is None else f"{value:,.4g}"

        def interval(item):
            return (f"完整激活概率区间：{item['full_success_lower']:.2%} ～ "
                    f"{item['full_success_upper']:.2%}\n"
                    f"相对基准策略成功率（近似）：{item['success_probability']:.2%}")

        zero, current = result["zero"], result["current"]
        self.assertEqual(window.zero_strategy.text(), interval(zero) +
                         f"\n单次尝试加权净成本：{amount(zero['attempt_cost'])}"
                         f"\n成功期望总加权净成本：{amount(zero['expected_cost'])}"
                         f"\n期望胚子：{amount(zero['expected_embryos'])}"
                         f" · 调谐：{amount(zero['expected_tuning'])}"
                         f" · 净经验：{amount(zero['expected_experience'])}")
        recommendation = "建议继续" if current["recommend_continue"] else "建议停止"
        self.assertEqual(window.current_strategy.text(), interval(current) +
                         f"\n0 词条达标概率下界基准：{current['baseline_success_lower']:.2%}"
                         f"\n{recommendation}（与同类新胚子比较，非成本最优）"
                         f"\n原因：{current['reason']}"
                         f"\n后续加权净成本：{amount(current['future_cost'])}")
        self.assertEqual(window.strategy_status.text(),
                         f"策略已更新 · 分数网格近似步长 {result['score_resolution']:g} · {result['method']}")
        scored = window._strategy_cache["planner"].scorer.score(window.current_echo())
        self.assertEqual(window.score_label.text(), f"{scored['score']:g} 分")
        score, target = scored["score"], window.target.value()
        status = (f"已达目标 · 超出 {score - target:g} 分" if score >= target else
              f"未达目标 · 还差 {target - score:g} 分")
        self.assertEqual(window.summary.text(), status +
                 f"\n主词条 {scored['main_score']:g} 分 + 副词条 {scored['sub_score']:g} 分"
                 f" + 小词条 {scored['minor_score']:g} 分"
                 f" · 已激活 {scored['activated_count']}/5")
        self.assertEqual(window.progress.value(),
                         min(1000, int(score / 50 * 1000)))
        entries = [("主词条", scored["main_stat"]), ("小词条", scored["minor_stat"])] + [("副词条", item) for item in scored["sub_stats"]]
        self.assertEqual(window.details.rowCount(), len(entries))
        for row, (category, item) in enumerate(entries):
            suffix = "%" if window.stats["stat_definitions"][item["stat_id"]]["unit"] == "percentage_points" else ""
            self.assertEqual([window.details.item(row, col).text() for col in range(5)],
                             [category, item["name"], f"{item['value']:g}{suffix}",
                              f"{item['weight']:g}", f"{item['score']:g}"])

    @unittest.skipUnless(importlib.util.find_spec("src.echo_strategy"), "等待策略模块实现")
    def test_real_gui_end_to_end_strategy(self):
        window = EchoWindow()
        try:
            # 保留真实完整属性/概率/成本及 bins=1000，不替换任何 planner。
            # 主词条已达标，默认精度端到端测试无需遍历完整 DP。
            window.target.setValue(1)
            self.wait_for_strategy(window)
            planner = window._strategy_cache["planner"]
            self.assertEqual(planner.bins, 1000)
            self.assertEqual(planner.target, 1)
            self.assert_results_match_api(window, planner.analyze(window.current_echo()))
            self.assertIn("已激活 0/5", window.summary.text())

            self.assertIn("完整激活概率区间：100.00% ～ 100.00%", window.zero_strategy.text())
            self.assertIn("期望胚子：1 · 调谐：0 · 净经验：0", window.zero_strategy.text())
            self.assertIn("目标已达成，无需继续", window.current_strategy.text())
            self.assertIn("建议停止", window.current_strategy.text())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    @unittest.skipUnless(importlib.util.find_spec("src.echo_strategy"), "等待策略模块实现")
    def test_real_low_bins_planner_async_results(self):
        from src import echo_strategy

        real_planner = echo_strategy.ActivationPlanner
        for target in (25, 1_000_000):
            with self.subTest(target=target):
                builds, analyses, displays = [], [], []

                class LowBinsPlanner(real_planner):
                    def __init__(self, scorer, costs, target, **params):
                        builds.append((threading.get_ident(), dict(params)))
                        # 仅测试注入降低分辨率，仍执行真实构建/概率/阈值/成本算法。
                        super().__init__(scorer, costs, target, **{**params, "bins": 20})

                    def analyze(self, echo):
                        result = super().analyze(echo)
                        analyses.append((threading.get_ident(), copy.deepcopy(echo), result))
                        return result

                with patch.object(echo_strategy, "ActivationPlanner", LowBinsPlanner):
                    window = EchoWindow()
                    display = window._display_strategy

                    def record_display(result):
                        displays.append(QThread.currentThread())
                        display(result)

                    window._display_strategy = record_display
                    try:
                        window.main_stat.setCurrentIndex(window.main_stat.findData("crit_rate"))
                        window.target.setValue(target)
                        self.wait_for_strategy(window)
                        self.assertEqual(len(builds), 1)
                        self.assertEqual(builds[0][1]["bins"], 1000)
                        self.assertNotEqual(builds[0][0], threading.get_ident())
                        self.assertEqual(len(analyses), 1)
                        self.assertNotEqual(analyses[0][0], threading.get_ident())
                        self.assertEqual(analyses[0][1], window.current_echo())
                        self.assertEqual(displays, [self.app.thread()])
                        self.assertEqual(window._strategy_cache["planner"].bins, 20)
                        result = analyses[0][2]
                        self.assert_results_match_api(window, result)
                        if target == 25:
                            self.assertGreater(result["zero"]["success_probability"], 0)
                            self.assertLess(result["zero"]["success_probability"], 1)
                            self.assertIsNotNone(result["zero"]["expected_cost"])
                            self.assertTrue(result["current"]["recommend_continue"])
                        else:
                            self.assertEqual(result["zero"]["success_probability"], 0)
                            self.assertEqual(window.zero_strategy.text().count("无法获得 / ∞"), 4)
                            self.assertFalse(result["current"]["recommend_continue"])
                    finally:
                        # 注入保持有效直到在途任务结束，避免影响后续测试。
                        window.close()
                        window.deleteLater()
                        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()