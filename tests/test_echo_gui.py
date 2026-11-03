"""离屏创建真实 Qt 控件并验证交互，不要求桌面显示服务。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QThread, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QStyledItemDelegate, QTabWidget, QToolButton, QWidget,
)
from src.echo_gui import EchoWindow, ScoreProgressBar
from src.echo_score import DEFAULT_STATS, EchoScorer, load_json
from src.echo_weight_presets import DEFAULT_PRESET_NAME, WeightPresetStore


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
                    "reason": reason, "future_cost": 3, "continue_cost": 36,
                    "restart_cost": 25, "expected_total_cost": 25},
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
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.presets_path = Path(directory.name) / "weight-presets.json"
        path_patch = patch("src.echo_gui.weight_presets_path", return_value=self.presets_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        legacy_patch = patch("src.echo_gui.legacy_weight_presets_path",
                     return_value=Path(directory.name) / "legacy" / "weight-presets.json")
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        dialog_patch = patch("src.echo_gui.QMessageBox.exec", return_value=QMessageBox.StandardButton.Discard)
        dialog_patch.start()
        self.addCleanup(dialog_patch.stop)
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

    def test_four_sections_collapse_independently_and_preserve_inputs(self):
        self.assertEqual(len(self.window.sections), 4)
        self.window.show()
        self.app.processEvents()
        original = self.window.current_weights(), self.window.current_echo(), self.window.target.value()
        for index, section in enumerate(self.window.sections):
            self.assertTrue(section.toggle_button.isChecked())
            self.assertTrue(section.toggle_button.text().startswith("▼"))
            expanded_height = section.sizeHint().height()
            QTest.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertTrue(section.content.isHidden())
            self.assertTrue(section.toggle_button.text().startswith("▶"))
            self.assertLess(section.sizeHint().height(), expanded_height)
            for other in self.window.sections[index + 1:]:
                self.assertFalse(other.content.isHidden())
        self.assertEqual(original, (self.window.current_weights(), self.window.current_echo(),
                                    self.window.target.value()))
        for section in self.window.sections:
            section.toggle_button.setFocus()
            QTest.keyClick(section.toggle_button, Qt.Key.Key_Space)
            self.assertFalse(section.content.isHidden())
            self.assertTrue(section.toggle_button.text().startswith("▼"))

    def test_collapsed_results_receive_background_strategy_updates(self):
        section = self.window.sections[2]
        section.toggle_button.click()
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertTrue(section.content.isHidden())
        self.assertNotEqual(self.window.strategy_metrics["zero_probability"].text(), "—")
        section.toggle_button.click()
        self.assertFalse(section.content.isHidden())
        self.assertIn("开完剩余词条达标概率", self.window.current_strategy.text())

    def test_score_results_directly_below_target_before_strategy_parameters(self):
        layout = self.window.score_label.parentWidget().layout()
        self.assertEqual(layout.itemAt(0).widget().text(),
                         "可自由设置目标分数，系统将据此进行开孔推荐。")
        self.assertIs(layout.itemAt(1).layout().itemAt(1).widget(), self.window.target)
        self.assertIs(layout.itemAt(2).layout().itemAt(0).widget(), self.window.score_label)
        self.assertIs(layout.itemAt(3).widget(), self.window.progress_status)
        self.assertIs(layout.itemAt(4).widget(), self.window.progress)
        self.assertIs(layout.itemAt(6).widget(), self.window.custom_weights_notice)
        self.assertIs(layout.itemAt(7).widget(), self.window.details)
        self.assertIs(layout.itemAt(8).widget(), self.window.cost_divider)
        self.assertEqual(layout.itemAt(9).widget().text(), "成本估算相关参数")
        self.assertIs(layout.itemAt(10).layout(), self.window.cost_parameters_layout)

    def test_cost_parameters_use_five_equal_columns(self):
        grid = self.window.cost_parameters_layout
        for column, box in enumerate(self.window.strategy_inputs.values()):
            self.assertIs(grid.itemAtPosition(1, column).widget(), box)
            self.assertEqual(grid.columnStretch(column), 1)
            self.assertIsNotNone(grid.itemAtPosition(0, column).widget())
        self.assertEqual(grid.columnCount(), 5)

    def test_cost_labels_explain_both_material_refunds_and_units(self):
        grid = self.window.cost_parameters_layout
        self.assertEqual([grid.itemAtPosition(0, column).widget().text() for column in range(5)],
                 ["经验回收比例", "打孔器回收比例", "打孔器权重", "每1000经验权重", "胚子权重"])
        hint = self.window.result_hint.text()
        self.assertIn("每次 10 个打孔器", hint)
        self.assertIn("经验默认返还 70%", hint)
        self.assertIn("打孔器默认返还 30%", hint)
        self.assertIn("余量保留", hint)
        self.assertIn("强化和开孔不会失败", hint)
        self.assertIn("主动弃置回收", hint)
        self.assertIn("保留不回收", hint)
        self.assertNotIn("失败回收", hint)
        self.assertNotIn("占位", hint)

    def test_requested_input_tooltips_removed_other_hints_preserved(self):
        for cost, box in self.window.weight_inputs["minor_stats"].items():
            with self.subTest(cost=cost):
                self.assertEqual(box.toolTip(), "")
        self.assertEqual(self.window.cost_parameters_label.toolTip(), "")
        for name in ("recovery", "tuning_recovery", "experience_weight"):
            with self.subTest(parameter=name):
                self.assertEqual(self.window.strategy_inputs[name].toolTip(), "")
        for name, label in self.window.strategy_metrics.items():
            with self.subTest(metric=name):
                self.assertTrue(label.parentWidget().toolTip())
        for section in self.window.sections:
            self.assertTrue(section.toggle_button.toolTip())
        self.assertTrue(self.window.progress.toolTip())
        self.assertTrue(self.window.theoretical_max_help.toolTip())
        self.assertTrue(self.window.reconstruction_budget.toolTip())

    def test_cost_default_inputs_read_config_and_forward_to_worker(self):
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        params = ControlledPlanner.instances[-1].params
        self.assertEqual(params["experience_weight"], 1.6)
        self.assertEqual(params["recovery"], .7)
        self.assertEqual(params["tuning_recovery"], .3)
        self.assertEqual(params["tuning_weight"], 1)
        self.assertEqual(params["embryo_weight"], 1)

    def test_cost_default_inputs_follow_config_override(self):
        cost_path = DEFAULT_STATS.with_name("activation-costs.json")
        costs = load_json(cost_path)
        costs["recovery_ratio"] = .6
        costs["tuning_recovery_ratio"] = .2
        costs["default_weights"] = {"tuning_weight": 2, "experience_weight": 3.2,
                                    "embryo_weight": 0}

        def custom_config(path):
            return costs if path == cost_path else load_json(path)

        with patch("src.echo_gui.load_json", side_effect=custom_config):
            window = EchoWindow()
            try:
                self.assertEqual({name: box.value() for name, box in window.strategy_inputs.items()},
                                 {"recovery": .6, "tuning_recovery": .2, **costs["default_weights"]})
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_cost_default_config_unavailable_does_not_break_scoring(self):
        cost_path = DEFAULT_STATS.with_name("activation-costs.json")
        for error in (OSError("missing costs"), ValueError("bad costs")):
            def unavailable_config(path):
                if path == cost_path:
                    raise error
                return load_json(path)

            with self.subTest(error=error), patch("src.echo_gui.load_json", side_effect=unavailable_config), \
                    patch.object(sys.modules["src.echo_strategy"], "DEFAULT_COSTS", cost_path):
                window = EchoWindow()
                try:
                    self.assertIsNotNone(window._score_result)
                    self.assertEqual(window.cost_parameters_label.toolTip(), "")
                    self.wait_until(lambda: str(error) in window.strategy_status.text())
                    self.assertIn("策略计算失败（不影响评分）", window.strategy_status.text())
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()

    def test_weight_source_default_import_edit_restore_and_unrelated_changes(self):
        combo = self.window.weight_preset_combo
        self.assertEqual(combo.currentText(), DEFAULT_PRESET_NAME)
        self.assertTrue(self.window.custom_weights_notice.isHidden())
        self.window.target.setValue(41)
        self.window.cost.setCurrentIndex(0)
        self.window.strategy_inputs["recovery"].setValue(.75)
        self.assertEqual(combo.currentText(), DEFAULT_PRESET_NAME)
        for inputs in self.window.weight_inputs.values():
            self.window.restore_weights()
            box = next(iter(inputs.values()))
            box.setValue(box.value() + 1)
            self.assertEqual(combo.currentText(), DEFAULT_PRESET_NAME + "（有修改）")
            self.assertFalse(self.window.custom_weights_notice.isHidden())
        self.window.restore_weights()
        preset = self.window.current_weights()
        preset["sub_stats"]["crit_rate"] += 1
        incoming = {"schema_version": 1, "presets": [
            {"id": "imported-id", "name": "导入方案", "weights": preset}]}
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/角色权重.json", "")), \
                patch("src.echo_gui.load_json", return_value=incoming):
            self.window.import_weights()
        self.assertEqual(combo.currentText(), DEFAULT_PRESET_NAME)
        combo.setCurrentIndex(combo.findData("imported-id"))
        self.assertEqual(combo.currentText(), "导入方案")
        self.assertEqual(self.window.current_weights(), preset)
        self.window.clear_sub_stats()
        self.assertEqual(combo.currentText(), "导入方案")
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("", "")):
            self.window.import_weights()
        self.assertEqual(self.window.current_weights(), preset)
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/bad.json", "")), \
                patch("src.echo_gui.load_json", side_effect=ValueError("bad preset")), \
                patch("src.echo_gui.QMessageBox.warning"):
            self.window.import_weights()
        self.assertEqual(self.window.current_weights(), preset)
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        box.setValue(box.value() + 1)
        self.assertEqual(combo.currentText(), "导入方案（有修改）")
        combo.setCurrentIndex(combo.findData(self.window.preset_store.presets[0]["id"]))
        self.assertEqual(combo.currentText(), DEFAULT_PRESET_NAME)
        self.assertTrue(self.window.custom_weights_notice.isHidden())

    def test_stats_divider_and_clear_button_style(self):
        layout = self.window.stats_divider.parentWidget().layout()
        self.assertEqual(layout.itemAt(0).widget().text(), "直接点击词条名进行勾选，再次点击可取消勾选。")
        self.assertIs(layout.itemAt(2).widget(), self.window.stats_divider)
        self.assertIs(layout.itemAt(3).layout(), self.window.sub_options_layout)
        self.assertEqual(self.window.minor_stat_label.contentsMargins().left(), 8)
        self.assertEqual(self.window.clear_sub_stats_button.objectName(), "clearSelection")
        self.assertEqual(self.window.width(), 980)

    def test_strategy_metric_cards_match_results_and_clear_pending_values(self):
        self.window._strategy_timer.stop()
        self.window._display_strategy(strategy_result())
        metrics = self.window.strategy_metrics
        self.assertEqual(metrics["zero_probability"].text(), "25.00%")
        self.assertEqual(metrics["zero_embryos"].text(), "4")
        self.assertEqual(metrics["zero_tuning"].text(), "20")
        self.assertEqual(metrics["zero_gold_tubes"].text(), "0.002")
        self.assertEqual(metrics["current_probability"].text(), "5.00%")
        self.assertEqual(metrics["current_cost"].text(), "3")
        self.window._display_strategy(strategy_result(unavailable=True))
        self.assertEqual(metrics["zero_embryos"].text(), "不可达 / ∞")
        self.assertEqual(metrics["zero_embryos"].property("tone"), "muted")
        self.window.target.setValue(41)
        self.assertTrue(all(label.text() == "—" for label in metrics.values()))

    def test_strategy_metric_titles_use_plain_language(self):
        expected = {
            "zero_probability": "单个胚子成功率",
            "zero_embryos": "消耗胚子",
            "zero_tuning": "消耗打孔器",
            "zero_gold_tubes": "消耗金密音筒",
            "current_probability": "后续成功率",
            "current_cost": "后续平均成本",
        }
        for key, title in expected.items():
            card = self.window.strategy_metrics[key].parentWidget()
            caption = card.findChild(QLabel, "metricCaption")
            self.assertEqual(caption.text(), title)

    def test_strategy_numbers_are_plain_and_not_scientific_notation(self):
        self.window._strategy_timer.stop()
        result = strategy_result()
        result["zero"].update(attempt_cost=12345.67, expected_cost=2345678.9,
                              expected_embryos=13.25, expected_tuning=98765.43,
                              expected_experience=1234567.89)
        result["current"]["future_cost"] = -12345.67
        self.window._display_strategy(result)
        text = self.window.zero_strategy.text()
        self.assertIn("单个胚子成功或弃置时平均成本：12345.67", text)
        self.assertIn("成功达标时累计成本：2345678.9", text)
        self.assertIn("一次性开五条达标概率：30.00% ～ 40.00%", text)
        self.assertEqual(self.window.strategy_metrics["zero_gold_tubes"].text(), "246.9136")
        self.assertEqual(self.window.strategy_metrics["current_cost"].text(), "-12345.67")
        self.assertNotRegex(text, r"\d[eE][+-]?\d")

    def test_ui_replaces_old_jargon_without_hiding_approximation(self):
        self.window._strategy_timer.stop()
        self.window._display_strategy(strategy_result())
        texts = [label.text() for label in self.window.findChildren(QLabel)]
        texts.extend(group.title() for group in self.window.findChildren(QGroupBox))
        texts.extend(widget.toolTip() for widget in self.window.findChildren(QWidget))
        texts.extend(self.window.reconstruction_budget_table.horizontalHeaderItem(column).text()
                     for column in range(self.window.reconstruction_budget_table.columnCount()))
        for text in texts:
            for old in ("净", "加权", "开孔素材", "相对基准策略", "完整激活概率"):
                self.assertNotIn(old, text)
        self.assertIn("概率本身是固定的", self.window.result_hint.text())
        self.assertIn("计算误差", self.window.result_hint.text())
        self.assertIn("估算范围", self.window.zero_strategy.text())
        self.assertIn("估算范围", self.window.current_strategy.text())
        self.assertNotIn("参考阈值", self.window.current_strategy.text())
        self.assertIn("建议放弃", self.window.current_strategy.text())
        self.assertIn("继续成本：36", self.window.current_strategy.text())
        self.assertIn("弃置成本：25", self.window.current_strategy.text())

    def test_score_table_highlights_contributions_and_aligns_numbers(self):
        short_table_height = self.window.details.height()
        self.choose_example()
        self.assertTrue(self.window.details.alternatingRowColors())
        self.assertFalse(self.window.details.showGrid())
        expected_height = (
            self.window.details.horizontalHeader().sizeHint().height()
            + self.window.details.rowCount()
            * self.window.details.verticalHeader().defaultSectionSize()
            + 2 * self.window.details.frameWidth())
        self.assertEqual(self.window.details.height(), expected_height)
        self.assertGreater(self.window.details.height(), short_table_height)
        self.assertEqual(
            self.window.details.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(self.window.details.rowCount(), 7)
        for row in range(self.window.details.rowCount()):
            cell = self.window.details.item(row, 4)
            self.assertTrue(cell.font().bold())
            self.assertTrue(cell.textAlignment() & Qt.AlignmentFlag.AlignRight)
            self.assertEqual(cell.foreground().color().name(), "#2456a6")

    def test_unaligned_help_uses_circular_question_button(self):
        self.assertIsInstance(self.window.theoretical_max_help, QToolButton)
        self.assertEqual(self.window.theoretical_max_help.text(), "?")
        self.assertEqual(self.window.theoretical_max_help.accessibleName(), "未对齐分数说明")
        self.assertIn("未对齐分数 =", self.window.theoretical_max_help.toolTip())
        self.assertEqual(self.window.theoretical_max_label.toolTip(), "")

    def test_unaligned_help_stays_next_to_score_at_different_widths(self):
        for width in (760, 980, 1280):
            self.window.resize(width, 940)
            self.window.show()
            self.app.processEvents()
            label = self.window.theoretical_max_label
            button = self.window.theoretical_max_help
            self.assertTrue(label.text().endswith("）"))
            self.assertLessEqual(label.width(), label.sizeHint().width())
            gap = button.x() - (label.x() + label.width())
            self.assertGreaterEqual(gap, 0)
            self.assertLessEqual(gap, 8)

    def test_unaligned_help_quick_hover_and_persistent_click(self):
        self.window.show()
        self.app.processEvents()
        button = self.window.theoretical_max_help
        self.app.sendEvent(button, QEvent(QEvent.Type.Leave))
        self.app.sendEvent(button, QEvent(QEvent.Type.Enter))
        self.assertEqual(button.hover_timer.interval(), 150)
        self.assertFalse(button.popup.isVisible())
        QTest.qWait(200)
        self.assertTrue(button.popup.isVisible())
        self.assertEqual(button.help_label.text(), button.toolTip())
        self.app.sendEvent(button, QEvent(QEvent.Type.Leave))
        self.assertFalse(button.popup.isVisible())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.app.sendEvent(button, QEvent(QEvent.Type.Leave))
        QTest.qWait(200)
        self.assertTrue(button.isChecked())
        self.assertTrue(button.popup.isVisible())
        self.assertFalse(button.hover_timer.isActive())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertFalse(button.popup.isVisible())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        QTest.keyClick(button, Qt.Key.Key_Escape)
        self.assertFalse(button.isChecked())
        self.assertFalse(button.popup.isVisible())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.window.hide()
        self.assertFalse(button.popup.isVisible())

    def test_minimum_window_width_does_not_require_page_horizontal_scroll(self):
        self.window.resize(760, 940)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.centralWidget().horizontalScrollBar().maximum(), 0)

    def test_strategy_results_share_one_row_with_equal_columns(self):
        layout = self.window.score_label.parentWidget().layout()
        results = layout.itemAt(12).layout()
        self.assertIsInstance(results, QHBoxLayout)
        self.assertEqual(results.count(), 2)
        self.assertIs(results.itemAt(0).widget(), self.window.zero_strategy.parentWidget())
        self.assertIs(results.itemAt(1).widget(), self.window.current_strategy.parentWidget())
        self.assertEqual(results.stretch(0), 1)
        self.assertEqual(results.stretch(1), 1)

    def test_zero_activation_metrics_use_two_by_two_grid_and_match_panel_height(self):
        layout = self.window.zero_strategy.parentWidget().layout()
        metrics = layout.itemAt(0).layout()
        self.assertIsInstance(metrics, QGridLayout)
        self.assertEqual(metrics.count(), 4)
        self.assertEqual([(metrics.getItemPosition(i)[0], metrics.getItemPosition(i)[1])
                          for i in range(metrics.count())], [(0, 0), (0, 1), (1, 0), (1, 1)])
        self.window.resize(980, 1000)
        self.window.show()
        self.app.processEvents()
        zero_panel = self.window.zero_strategy.parentWidget()
        current_panel = self.window.current_strategy.parentWidget()
        self.assertEqual(zero_panel.height(), current_panel.height())

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
        self.assertEqual(self.window.progress_status.text(), "分数达标 · 继续开满剩余 5 条")

    def test_reached_but_unfinished_echo_never_shows_stop_or_finished(self):
        self.window.target.setValue(0)
        result = strategy_result()
        result["current"]["recommend_continue"] = False
        for count, name in enumerate(list(self.window.sub_stat_buttons)[:5]):
            self.window._display_strategy(result)
            self.assertEqual(self.window.progress_status.text(),
                             f"分数达标 · 继续开满剩余 {5 - count} 条")
            self.assertIn(f"仍需开满剩余 {5 - count} 条", self.window.summary.text())
            self.assertIn("#16a34a", self.window.progress.styleSheet())
            self.window.sub_stat_buttons[name].click()
        self.assertEqual(self.window.progress_status.text(), "达成目标")

    def test_result_hint_requires_complete_five_slot_success(self):
        hint = self.window.result_hint.text()
        self.assertIn("达标后仍必须", hint)
        self.assertIn("+25", hint)
        self.assertIn("开满五条", hint)
        self.assertIn("剩余经验和打孔器", hint)

    def test_rounded_score_cannot_mark_unreached_echo_as_completed(self):
        weights = self.window.current_weights()
        for pool in weights["main_stats"].values():
            for name in pool:
                pool[name] = 0
        for pool in weights["minor_stats"].values():
            for name in pool:
                pool[name] = 0
        weights["main_stats"]["4"]["crit_rate"] = 1
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        weights["sub_stats"]["crit_dmg"] = .036
        self.window.apply_weights(weights)
        self.window.target.setValue(48.3389)
        for name in ("hp_flat", "def_flat", "hp_pct", "def_pct", "energy_regen"):
            self.window.sub_stat_buttons[name].click()
        self.assertEqual(self.window._score_result["score"], 48.3389)
        self.assertIn("未达目标", self.window.summary.text())
        self.assertEqual(self.window.progress_status.text(), "下次好运")

    def test_weight_edit_and_restore(self):
        self.choose_example()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(30)
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")
        self.window.restore_weights()
        expected = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected:g} 分")

    def add_weight_preset(self, name="队伍甲"):
        with patch("src.echo_gui.QInputDialog.getText", return_value=(name, True)):
            self.assertTrue(self.window.save_weights_as())
        return self.window.preset_store.selected_id

    def test_weight_preset_save_as_save_rename_and_restore_last_selection(self):
        original = self.window.current_weights()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(3.1256)
        changed = self.window.current_weights()
        preset_id = self.add_weight_preset()
        self.assertEqual(self.window.weight_preset_combo.currentText(), "队伍甲")
        self.assertEqual(self.window.preset_store.presets[0]["weights"], original)
        self.assertFalse(self.window._weights_dirty)
        with patch("src.echo_gui.QInputDialog.getText", return_value=("角色乙", True)):
            self.window.rename_weights_action.trigger()
        self.assertEqual(self.window.weight_preset_combo.currentText(), "角色乙")
        self.assertEqual(self.window.preset_store.selected_id, preset_id)
        self.window.weight_inputs["main_stats_4"]["crit_rate"].setValue(.75)
        self.window.save_weights_button.click()
        self.assertFalse(self.window._weights_dirty)
        window = EchoWindow()
        try:
            self.assertEqual(window.weight_preset_combo.currentText(), "角色乙")
            self.assertEqual(window.current_weights(), self.window.current_weights())
            self.assertEqual(window.current_weights()["sub_stats"], changed["sub_stats"])
            self.assertTrue(window.custom_weights_notice.isHidden())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_weight_preset_dirty_reverts_and_notice_above_score_table(self):
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        original = box.value()
        box.setValue(original + 1)
        self.assertEqual(self.window.weight_preset_combo.currentText(), DEFAULT_PRESET_NAME + "（有修改）")
        notice = self.window.custom_weights_notice
        self.assertEqual(notice.text(), "当前为自定义权重")
        self.assertFalse(notice.isHidden())
        self.assertEqual(notice.objectName(), "customWeightsNotice")
        layout = notice.parentWidget().layout()
        self.assertEqual(layout.indexOf(notice) + 1, layout.indexOf(self.window.details))
        self.window.target.setValue(39)
        self.assertTrue(self.window._weights_dirty)
        box.setValue(original)
        self.assertEqual(self.window.weight_preset_combo.currentText(), DEFAULT_PRESET_NAME)
        self.assertTrue(notice.isHidden())
        self.assertFalse(self.window._weights_dirty)

    def test_weight_preset_combo_blue_for_saved_and_orange_for_custom_weights(self):
        self.window.show()
        self.app.processEvents()
        combo = self.window.weight_preset_combo
        self.assertEqual(combo.objectName(), "weightPreset")
        selected_id = combo.currentData()
        saved = self.window.preset_store.current
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        original = box.value()
        for value, text, background, foreground in (
            (original, saved["name"], "#e8efff", "#2456a6"),
            (original + 1, saved["name"] + "（有修改）", "#fff8eb", "#b45309"),
            (original, saved["name"], "#e8efff", "#2456a6")):
            with self.subTest(text=text):
                box.setValue(value)
                self.app.processEvents()
                self.assertEqual(combo.currentText(), text)
                self.assertEqual(combo.palette().button().color().name(), background)
                self.assertEqual(combo.palette().buttonText().color().name(), foreground)
                if self.window._weights_dirty:
                    self.assertEqual(combo.palette().button().color(), box.palette().base().color())
                    self.assertEqual(combo.palette().buttonText().color(), box.palette().text().color())
                self.assertEqual(combo.currentData(), selected_id)
                self.assertEqual(self.window.preset_store.current, saved)
        self.assertEqual(self.window.cost.palette().button().color().name(), "#ffffff")
        box.setValue(original + 1)
        self.assertEqual(combo.palette().button().color().name(), "#fff8eb")
        self.assertTrue(self.window.save_weights())
        self.assertEqual(combo.currentText(), saved["name"])
        self.assertEqual(combo.palette().button().color().name(), "#e8efff")
        self.assertEqual(combo.palette().buttonText().color().name(), "#2456a6")

    def test_weight_preset_modified_label_keeps_original_name_for_save_and_confirmation(self):
        self.add_weight_preset("角色方案")
        selected_id = self.window.preset_store.selected_id
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        self.assertEqual(self.window.weight_preset_combo.currentText(), "角色方案（有修改）")

        def cancel_with_original_name(dialog):
            self.assertIn("角色方案", dialog.text())
            self.assertNotIn("自定义权重", dialog.text())
            return QMessageBox.StandardButton.Cancel

        with patch("src.echo_gui.QMessageBox.exec", cancel_with_original_name):
            self.assertFalse(self.window.close())
        self.assertEqual(self.window.weight_preset_combo.currentData(), selected_id)
        self.assertTrue(self.window.save_weights())
        self.assertEqual(self.window.weight_preset_combo.currentText(), "角色方案")
        self.assertEqual(self.window.preset_store.selected_id, selected_id)
        self.assertEqual(self.window.preset_store.current["weights"]["sub_stats"]["crit_rate"], 7)

    def test_weight_preset_popup_items_stay_white_and_neutral_in_both_states(self):
        self.add_weight_preset("第二套")
        self.window.show()
        self.app.processEvents()
        combo = self.window.weight_preset_combo
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        original = box.value()
        for value in (original, original + 1, original):
            with self.subTest(value=value):
                box.setValue(value)
                combo.showPopup()
                try:
                    self.app.processEvents()
                    view = combo.view()
                    self.assertIsInstance(combo.itemDelegate(), QStyledItemDelegate)
                    self.assertEqual(view.palette().base().color().name(), "#ffffff")
                    self.assertEqual(view.palette().text().color().name(), "#243247")
                    self.assertEqual(view.palette().highlight().color().name(), "#e8efff")
                    self.assertEqual(view.palette().highlightedText().color().name(), "#2456a6")
                    viewport = view.viewport()
                    image = viewport.grab().toImage()
                    rect = view.visualRect(combo.model().index(0, 0)).intersected(viewport.rect())
                    self.assertFalse(rect.isEmpty())
                    x = int((rect.right() - 8) * image.width() / viewport.width())
                    y = int(rect.center().y() * image.height() / viewport.height())
                    self.assertEqual(image.pixelColor(x, y).name(), "#ffffff")
                finally:
                    combo.hidePopup()

    def test_weight_preset_discard_buttons_restore_saved_values_for_all_tabs(self):
        for inputs in self.window.weight_inputs.values():
            box = next(iter(inputs.values()))
            box.setValue(box.value() + .25)
        self.add_weight_preset("需要还原的方案")
        saved = self.window.current_weights()
        self.assertNotEqual(saved, self.window.defaults)
        selected_id = self.window.preset_store.selected_id
        self.choose_example()
        echo, score = self.window.current_echo(), self.window.score_label.text()
        target = self.window.target.value()
        parameters = {name: box.value() for name, box in self.window.strategy_inputs.items()}
        self.window.show()
        self.app.processEvents()
        self.assertEqual(set(self.window.discard_weight_buttons), set(self.window.weight_inputs))
        self.assertTrue(all(button.isHidden() for button in self.window.discard_weight_buttons.values()))
        for index, category in enumerate(self.window.weight_inputs):
            with self.subTest(category=category):
                self.window.weight_tabs.setCurrentIndex(index)
                for inputs in self.window.weight_inputs.values():
                    box = next(iter(inputs.values()))
                    box.setValue(box.value() + 1)
                self.app.processEvents()
                self.assertTrue(all(not button.isHidden() for button in self.window.discard_weight_buttons.values()))
                button = self.window.discard_weight_buttons[category]
                self.assertTrue(button.isVisible())
                self.assertEqual(button.text(), "放弃修改")
                footer = button.parentWidget().layout().itemAt(2).layout()
                self.assertIs(footer.itemAt(1).widget(), button)
                self.assertIs(footer.itemAt(2).widget(), self.window.clear_weight_buttons[category])
                before_disk = self.presets_path.read_bytes()
                version = self.window._strategy_version
                with patch.object(self.window.preset_store, "_commit") as commit:
                    button.click()
                commit.assert_not_called()
                self.assertEqual(self.presets_path.read_bytes(), before_disk)
                self.assertEqual(self.window.current_weights(), saved)
                self.assertEqual(self.window.current_echo(), echo)
                self.assertEqual(self.window.score_label.text(), score)
                self.assertEqual(self.window.target.value(), target)
                self.assertEqual({name: box.value() for name, box in self.window.strategy_inputs.items()}, parameters)
                self.assertEqual(self.window.preset_store.selected_id, selected_id)
                self.assertEqual(self.window.weight_preset_combo.currentText(), "需要还原的方案")
                self.assertEqual(self.window.weight_preset_combo.palette().button().color().name(), "#e8efff")
                self.assertEqual(self.window._strategy_version, version + 1)
                self.assertFalse(self.window._weights_dirty)
                self.assertTrue(self.window.custom_weights_notice.isHidden())
                self.assertTrue(all(button.isHidden() for button in self.window.discard_weight_buttons.values()))

    def test_weight_preset_discard_reverts_pending_editor_text_without_saving(self):
        saved = self.window.current_weights()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        box = self.window.weight_inputs["main_stats_4"]["crit_rate"]
        box.selectAll()
        QTest.keyClicks(box, "3.1256")
        self.assertNotEqual(box.cleanText(), f"{box.value():.4f}")
        self.window.discard_weight_buttons["sub_stats"].click()
        self.assertEqual(self.window.current_weights(), saved)
        self.assertEqual(box.cleanText(), f"{box.value():.4f}")
        self.assertFalse(self.window._weights_dirty)

    def test_weight_preset_discard_visibility_follows_revert_save_and_switch(self):
        default_id = self.window.preset_store.selected_id
        self.add_weight_preset("新方案")
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        original = box.value()
        for action in (lambda: box.setValue(original), self.window.save_weights,
                       lambda: self.window.weight_preset_combo.setCurrentIndex(
                           self.window.weight_preset_combo.findData(default_id))):
            box.setValue(original + 1)
            self.assertTrue(all(not button.isHidden() for button in self.window.discard_weight_buttons.values()))
            action()
            self.assertTrue(all(button.isHidden() for button in self.window.discard_weight_buttons.values()))
            original = box.value()

    def test_weight_preset_discard_refreshes_score_and_background_strategy(self):
        self.choose_example()
        original_score = self.window.score_label.text()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        changed_planner = ControlledPlanner.instances[-1]
        self.window.discard_weight_buttons["sub_stats"].click()
        self.assertEqual(self.window.score_label.text(), original_score)
        self.assertEqual(self.window.current_strategy.text(), "—")
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertIsNot(ControlledPlanner.instances[-1], changed_planner)
        self.assertEqual(ControlledPlanner.instances[-1].scorer.weights["sub_stats"],
                         self.window.preset_store.current["weights"]["sub_stats"])

    def test_weight_preset_switch_save_discard_cancel_and_persist_selection(self):
        default_id = self.window.preset_store.selected_id
        new_id = self.add_weight_preset()
        combo = self.window.weight_preset_combo
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        for choice in (QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard,
                       QMessageBox.StandardButton.Save):
            with self.subTest(choice=choice):
                combo.setCurrentIndex(combo.findData(new_id))
                saved = self.window.preset_store.current["weights"]["sub_stats"]["crit_rate"]
                box.setValue(saved + 1)
                with patch("src.echo_gui.QMessageBox.exec", return_value=choice) as dialog:
                    combo.setCurrentIndex(combo.findData(default_id))
                dialog.assert_called_once()
                if choice == QMessageBox.StandardButton.Cancel:
                    self.assertEqual(combo.currentData(), new_id)
                    self.assertEqual(box.value(), saved + 1)
                    self.assertTrue(self.window._weights_dirty)
                else:
                    self.assertEqual(combo.currentData(), default_id)
                    self.assertFalse(self.window._weights_dirty)
                    self.assertTrue(self.window.custom_weights_notice.isHidden())
                store = WeightPresetStore(self.presets_path, self.window.stats, self.window.defaults)
                self.assertEqual(store.selected_id, combo.currentData())
                preset = next(item for item in store.presets if item["id"] == new_id)
                self.assertEqual(preset["weights"]["sub_stats"]["crit_rate"],
                                 saved + 1 if choice == QMessageBox.StandardButton.Save else saved)

    def test_weight_preset_save_failure_cancels_switch_and_close(self):
        default_id = self.window.preset_store.selected_id
        new_id = self.add_weight_preset()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        before = self.window.current_weights()
        with patch("src.echo_gui.QMessageBox.exec", return_value=QMessageBox.StandardButton.Save), \
                patch("src.echo_gui.QMessageBox.warning") as warning, \
                patch.object(self.window.preset_store, "_commit", side_effect=OSError("disk full")):
            self.window.weight_preset_combo.setCurrentIndex(
                self.window.weight_preset_combo.findData(default_id))
            self.assertEqual(self.window.weight_preset_combo.currentData(), new_id)
            self.assertFalse(self.window.close())
            self.assertFalse(self.window._closing)
            self.assertEqual(warning.call_count, 2)
        self.assertEqual(self.window.current_weights(), before)
        self.assertTrue(self.window._weights_dirty)

    def test_weight_preset_selection_write_failure_keeps_dirty_input(self):
        default_id = self.window.preset_store.selected_id
        new_id = self.add_weight_preset()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        with patch.object(self.window.preset_store, "_commit", side_effect=OSError("disk full")), \
                patch("src.echo_gui.QMessageBox.warning") as warning:
            self.window.weight_preset_combo.setCurrentIndex(
                self.window.weight_preset_combo.findData(default_id))
        warning.assert_called_once()
        self.assertEqual(self.window.weight_preset_combo.currentData(), new_id)
        self.assertEqual(self.window.weight_inputs["sub_stats"]["crit_rate"].value(), 7)
        self.assertTrue(self.window._weights_dirty)

    def test_weight_preset_close_choices_and_dialog_labels(self):
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)

        def cancel_with_labels(dialog):
            self.assertEqual([dialog.button(button).text() for button in
                              (QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Discard,
                               QMessageBox.StandardButton.Cancel)], ["保存", "放弃", "取消"])
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Save))
            return QMessageBox.StandardButton.Cancel

        with patch("src.echo_gui.QMessageBox.exec", cancel_with_labels):
            self.assertFalse(self.window.close())
        self.assertFalse(self.window._closing)
        self.assertTrue(self.window._weights_dirty)
        with patch("src.echo_gui.QMessageBox.exec", return_value=QMessageBox.StandardButton.Save):
            self.assertTrue(self.window.close())
        store = WeightPresetStore(self.presets_path, self.window.stats, self.window.defaults)
        self.assertEqual(store.current["weights"]["sub_stats"]["crit_rate"], 7)

    def test_weight_preset_close_discard_does_not_save(self):
        original = self.window.current_weights()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        self.assertTrue(self.window.close())
        store = WeightPresetStore(self.presets_path, self.window.stats, self.window.defaults)
        self.assertEqual(store.current["weights"], original)

    def test_weight_preset_pending_keyboard_input_is_saved(self):
        box = self.window.weight_inputs["sub_stats"]["crit_rate"]
        original = box.value()
        box.selectAll()
        QTest.keyClicks(box, "3.1256")
        self.assertEqual(box.value(), original)
        self.window.save_weights_button.click()
        self.assertEqual(box.value(), 3.1256)
        self.assertFalse(self.window._weights_dirty)
        self.assertEqual(self.window.preset_store.current["weights"]["sub_stats"]["crit_rate"], 3.1256)

    def test_weight_preset_rename_preserves_unsaved_edits(self):
        saved = self.window.current_weights()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        with patch("src.echo_gui.QInputDialog.getText", return_value=("新名字", True)):
            self.window.rename_weights()
        self.assertEqual(self.window.weight_preset_combo.currentText(), "新名字（有修改）")
        self.assertEqual(self.window.preset_store.current["name"], "新名字")
        self.assertEqual(self.window.preset_store.current["weights"], saved)
        self.assertEqual(self.window.weight_inputs["sub_stats"]["crit_rate"].value(), 7)

    def test_weight_preset_cancel_duplicate_and_empty_name_do_not_change_store(self):
        self.add_weight_preset()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        before = self.presets_path.read_bytes()
        for name, accepted in (("取消", False), (" ", True), (DEFAULT_PRESET_NAME, True)):
            with self.subTest(name=name), \
                    patch("src.echo_gui.QInputDialog.getText", return_value=(name, accepted)), \
                    patch("src.echo_gui.QMessageBox.warning"):
                self.assertFalse(self.window.save_weights_as())
                self.window.rename_weights()
            self.assertEqual(self.presets_path.read_bytes(), before)
            self.assertEqual(self.window.weight_preset_combo.currentText(), "队伍甲（有修改）")

    def test_weight_preset_delete_confirmation_and_protect_last(self):
        self.assertFalse(self.window.delete_weights_action.isEnabled())
        with patch("src.echo_gui.QMessageBox.warning") as warning:
            self.window.delete_weights()
        warning.assert_called_once()
        self.add_weight_preset()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        with patch("src.echo_gui.QMessageBox.exec", return_value=QMessageBox.StandardButton.No):
            self.window.delete_weights()
        self.assertEqual(self.window.weight_preset_combo.currentText(), "队伍甲（有修改）")
        with patch("src.echo_gui.QMessageBox.exec", return_value=QMessageBox.StandardButton.Yes):
            self.window.delete_weights_action.trigger()
        self.assertEqual(self.window.weight_preset_combo.count(), 1)
        self.assertEqual(self.window.weight_preset_combo.currentText(), DEFAULT_PRESET_NAME)
        self.assertFalse(self.window._weights_dirty)
        self.assertTrue(self.window.custom_weights_notice.isHidden())
        self.assertFalse(self.window.delete_weights_action.isEnabled())

    def test_weight_preset_nonzero_colors_and_clear_each_page_only(self):
        self.window.show()
        self.app.processEvents()
        for index, (category, inputs) in enumerate(self.window.weight_inputs.items()):
            with self.subTest(category=category):
                self.window.weight_tabs.setCurrentIndex(index)
                first = next(iter(inputs.values()))
                first.setValue(1)
                self.app.processEvents()
                self.assertTrue(first.property("nonzero"))
                self.assertEqual(first.palette().text().color().name(), "#b45309")
                other_values = {name: {stat: box.value() for stat, box in boxes.items()}
                                for name, boxes in self.window.weight_inputs.items() if name != category}
                button = self.window.clear_weight_buttons[category]
                page_layout = button.parentWidget().layout()
                footer = page_layout.itemAt(2).layout()
                self.assertIsNotNone(footer.itemAt(0).spacerItem())
                self.assertIs(footer.itemAt(2).widget(), button)
                discard = self.window.discard_weight_buttons[category]
                self.assertIs(footer.itemAt(1).widget(), discard)
                self.assertEqual(button.geometry().right(), page_layout.contentsRect().right())
                self.assertLess(discard.geometry().right(), button.geometry().left())
                self.assertLessEqual(page_layout.contentsRect().bottom() - button.geometry().bottom(), 2)
                version = self.window._strategy_version
                button.click()
                self.assertEqual(self.window._strategy_version, version + 1)
                self.assertTrue(all(box.value() == 0 and not box.property("nonzero") for box in inputs.values()))
                self.assertEqual(first.palette().text().color().name(), "#243247")
                self.assertEqual(other_values, {name: {stat: box.value() for stat, box in boxes.items()}
                                                for name, boxes in self.window.weight_inputs.items()
                                                if name != category})
        self.assertEqual(self.window.score_label.text(), "0 分")
        self.assertTrue(self.window._weights_dirty)

    def test_weight_preset_switch_refreshes_score_and_background_strategy(self):
        default_id = self.window.preset_store.selected_id
        self.choose_example()
        original_score = self.window.score_label.text()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        self.add_weight_preset()
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        old_planner = ControlledPlanner.instances[-1]
        self.assertNotEqual(self.window.score_label.text(), original_score)
        version = self.window._strategy_version
        self.window.weight_preset_combo.setCurrentIndex(self.window.weight_preset_combo.findData(default_id))
        self.assertEqual(self.window.score_label.text(), original_score)
        self.assertEqual(self.window._strategy_version, version + 1)
        self.assertEqual(self.window.current_strategy.text(), "—")
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertIsNot(ControlledPlanner.instances[-1], old_planner)
        self.assertEqual(ControlledPlanner.instances[-1].scorer.weights["sub_stats"],
                         self.window.current_weights()["sub_stats"])

    def test_weight_preset_export_full_library_with_custom_weights(self):
        self.add_weight_preset("第二套")
        saved = self.window.preset_store.export_document()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        draft = self.window.current_weights()
        destination = self.presets_path.with_name("export.json")
        with patch("src.echo_gui.QFileDialog.getSaveFileName", return_value=(str(destination), "")):
            self.window.export_weights()
        exported = load_json(destination)
        expected = copy.deepcopy(saved)
        expected["presets"][-1]["weights"] = draft
        self.assertEqual(exported, expected)
        self.assertEqual(self.window.preset_store.export_document(), saved)
        self.assertTrue(self.window._weights_dirty)
        self.assertEqual(self.window.weight_preset_combo.currentText(), "第二套（有修改）")
        self.assertIn("全部 2 个", self.window.statusBar().currentMessage())
        self.assertEqual(WeightPresetStore(destination, self.window.stats, self.window.defaults).presets,
                         exported["presets"])

    def test_weight_preset_reject_single_weights_without_changing_library_or_inputs(self):
        single = self.window.current_weights()
        single["sub_stats"]["crit_rate"] = 9
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                if dirty:
                    self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
                before_weights = self.window.current_weights()
                before_document = self.window.preset_store.export_document()
                before_disk = self.presets_path.read_bytes()
                before_text = self.window.weight_preset_combo.currentText()
                with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/单套.json", "")), \
                        patch("src.echo_gui.load_json", return_value=single), \
                        patch("src.echo_gui.QMessageBox.warning") as warning, \
                        patch("src.echo_gui.QMessageBox.exec") as confirm, \
                        patch.object(self.window, "apply_weights") as apply:
                    self.window.import_weights()
                warning.assert_called_once()
                self.assertIn("不支持单套权重", warning.call_args.args[2])
                confirm.assert_not_called()
                apply.assert_not_called()
                self.assertEqual(self.window.current_weights(), before_weights)
                self.assertEqual(self.window.preset_store.export_document(), before_document)
                self.assertEqual(self.presets_path.read_bytes(), before_disk)
                self.assertEqual(self.window.weight_preset_combo.currentText(), before_text)
                self.assertEqual(self.window._weights_dirty, dirty)

    def test_weight_preset_import_library_appends_by_id_without_changing_draft_or_selection(self):
        selected_id = self.window.preset_store.selected_id
        original_presets = self.window.preset_store.presets
        duplicate = copy.deepcopy(original_presets[0])
        duplicate["name"] = "不应该覆盖本地名称"
        duplicate["weights"]["sub_stats"]["crit_rate"] = 999
        new_preset = {"id": "external-id", "name": "外部方案", "weights": self.window.current_weights()}
        imported = {"schema_version": 1, "selected_id": "external-id", "presets": [duplicate, new_preset]}
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        draft = self.window.current_weights()
        version = self.window._strategy_version
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/方案库.json", "")), \
                patch("src.echo_gui.load_json", return_value=imported), \
                patch("src.echo_gui.QMessageBox.exec") as confirm:
            self.window.import_weights()
        confirm.assert_not_called()
        self.assertEqual(self.window.preset_store.presets, original_presets + [new_preset])
        self.assertEqual(self.window.preset_store.selected_id, selected_id)
        self.assertEqual(self.window.weight_preset_combo.currentData(), selected_id)
        self.assertEqual(self.window.weight_preset_combo.count(), 2)
        self.assertEqual(self.window.weight_preset_combo.currentText(), DEFAULT_PRESET_NAME + "（有修改）")
        self.assertEqual(self.window.current_weights(), draft)
        self.assertFalse(self.window.custom_weights_notice.isHidden())
        self.assertEqual(self.window._strategy_version, version)
        self.assertIn("新增 1 个，跳过重复 ID 1 个", self.window.statusBar().currentMessage())
        stored = WeightPresetStore(self.presets_path, self.window.stats, self.window.defaults)
        self.assertEqual(stored.presets, original_presets + [new_preset])
        self.assertEqual(stored.selected_id, selected_id)
        # 新方案可正常切换并参与评分，重复导入不会再增加。
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/方案库.json", "")), \
                patch("src.echo_gui.load_json", return_value=imported):
            self.window.import_weights()
        self.assertEqual(self.window.weight_preset_combo.count(), 2)
        self.assertIn("新增 0 个，跳过重复 ID 2 个", self.window.statusBar().currentMessage())
        self.window.weight_preset_combo.setCurrentIndex(self.window.weight_preset_combo.findData("external-id"))
        self.assertEqual(self.window.current_weights(), new_preset["weights"])
        self.assertFalse(self.window._weights_dirty)
        expected = EchoScorer(self.window.stats, new_preset["weights"]).score(self.window.current_echo())
        self.assertEqual(self.window.score_label.text(), f"{expected['score']:g} 分")

    def test_weight_preset_import_library_same_name_different_id_gets_suffix(self):
        imported = {"schema_version": 1, "presets": [
            {"id": "new-id", "name": DEFAULT_PRESET_NAME, "weights": self.window.current_weights()}]}
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/方案库.json", "")), \
                patch("src.echo_gui.load_json", return_value=imported):
            self.window.import_weights()
        combo = self.window.weight_preset_combo
        self.assertEqual(combo.itemText(combo.findData("new-id")), DEFAULT_PRESET_NAME + "（导入 1）")
        self.assertIn("同名自动重命名 1 个", self.window.statusBar().currentMessage())

    def test_weight_preset_import_invalid_library_or_write_failure_preserves_draft(self):
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        draft = self.window.current_weights()
        saved = self.window.preset_store.export_document()
        before = self.presets_path.read_bytes()
        valid = {"id": "valid", "name": "有效", "weights": draft}
        invalid = {"id": "invalid", "name": "无效", "weights": {}}
        for presets in ([valid, invalid], {"not": "a-list"}):
            with self.subTest(presets=presets), \
                    patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/方案库.json", "")), \
                    patch("src.echo_gui.load_json", return_value={"schema_version": 1, "presets": presets}), \
                    patch("src.echo_gui.QMessageBox.warning") as warning:
                self.window.import_weights()
            warning.assert_called_once()
            self.assertEqual(self.window.current_weights(), draft)
            self.assertEqual(self.window.preset_store.export_document(), saved)
            self.assertEqual(self.presets_path.read_bytes(), before)
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("/tmp/方案库.json", "")), \
                patch("src.echo_gui.load_json", return_value={"schema_version": 1, "presets": [valid]}), \
                patch("src.echo_weight_presets.os.replace", side_effect=OSError("disk full")), \
                patch("src.echo_gui.QMessageBox.warning") as warning:
            self.window.import_weights()
        warning.assert_called_once()
        self.assertEqual(self.window.current_weights(), draft)
        self.assertEqual(self.window.preset_store.export_document(), saved)
        self.assertEqual(self.presets_path.read_bytes(), before)
        self.assertTrue(self.window._weights_dirty)

    def test_weight_preset_export_to_local_library_updates_saved_state_and_revision(self):
        self.add_weight_preset("第二套")
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        with patch("src.echo_gui.QFileDialog.getSaveFileName", return_value=(str(self.presets_path), "")):
            self.window.export_weights()
        self.assertFalse(self.window._weights_dirty)
        self.assertTrue(self.window.custom_weights_notice.isHidden())
        self.assertEqual(load_json(self.presets_path)["presets"][-1]["weights"], self.window.current_weights())
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(8)
        self.assertTrue(self.window.save_weights())

    def test_weight_preset_export_to_local_library_external_conflict_keeps_edits(self):
        other = WeightPresetStore(self.presets_path, self.window.stats, self.window.defaults)
        other.add("其他窗口新增", other.current["weights"])
        before = self.presets_path.read_bytes()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        with patch("src.echo_gui.QFileDialog.getSaveFileName", return_value=(str(self.presets_path), "")), \
                patch("src.echo_gui.QMessageBox.warning") as warning:
            self.window.export_weights()
        warning.assert_called_once()
        self.assertEqual(self.presets_path.read_bytes(), before)
        self.assertTrue(self.window._weights_dirty)
        self.assertEqual(self.window.weight_inputs["sub_stats"]["crit_rate"].value(), 7)

    def test_weight_preset_failed_export_preserves_existing_file(self):
        destination = self.presets_path.with_name("export.json")
        original = self.window.preset_store.export_document()
        destination.write_text(json.dumps(original), encoding="utf-8")
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(7)
        with patch("src.echo_gui.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
                patch("src.echo_weight_presets.os.replace", side_effect=OSError("disk full")), \
                patch("src.echo_gui.QMessageBox.warning") as warning:
            self.window.export_weights()
        warning.assert_called_once()
        self.assertEqual(load_json(destination), original)
        self.assertTrue(self.window._weights_dirty)

    def test_weight_preset_initial_write_failure_still_allows_scoring_and_retry(self):
        path = self.presets_path.with_name("first-run.json")
        with patch("src.echo_weight_presets.os.replace", side_effect=OSError("disk full")):
            window = EchoWindow(presets_path=path)
        try:
            self.assertIsNotNone(window._score_result)
            self.assertEqual(window.weight_preset_combo.currentText(), DEFAULT_PRESET_NAME + "（有修改）")
            self.assertIn("尚未保存", window.statusBar().currentMessage())
            self.assertFalse(window.preset_store.persisted)
            self.assertTrue(window.save_weights())
            self.assertFalse(window._weights_dirty)
            self.assertTrue(path.exists())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_weight_preset_default_location_migrates_existing_user_library(self):
        legacy = self.presets_path.parent / "legacy" / "weight-presets.json"
        source = WeightPresetStore(legacy, self.window.stats, self.window.defaults)
        weights = self.window.current_weights()
        weights["sub_stats"]["crit_rate"] = 7
        source.add("已有方案", weights)
        original = legacy.read_bytes()
        destination = self.presets_path.with_name("portable-presets.json")
        with patch("src.echo_gui.weight_presets_path", return_value=destination):
            window = EchoWindow()
        try:
            self.assertEqual(window.preset_store.path, destination)
            self.assertEqual(window.preset_store.migrated_from, legacy)
            self.assertEqual(window.weight_preset_combo.currentText(), "已有方案")
            self.assertEqual(window.current_weights(), weights)
            self.assertFalse(window._weights_dirty)
            self.assertIn(str(destination), window.statusBar().currentMessage())
            window.weight_inputs["sub_stats"]["crit_rate"].setValue(8)
            self.assertTrue(window.save_weights())
            self.assertEqual(legacy.read_bytes(), original)
            self.assertEqual(window.preset_store.current["weights"]["sub_stats"]["crit_rate"], 8)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_weight_preset_explicit_location_does_not_read_legacy_user_library(self):
        destination = self.presets_path.with_name("explicit.json")
        with patch("src.echo_gui.legacy_weight_presets_path") as legacy:
            window = EchoWindow(presets_path=destination)
        try:
            legacy.assert_not_called()
            self.assertIsNone(window.preset_store.migrated_from)
            self.assertEqual(window.preset_store.path, destination)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_weight_preset_existing_local_library_does_not_query_user_directory(self):
        with patch("src.echo_gui.legacy_weight_presets_path") as legacy:
            window = EchoWindow()
        try:
            legacy.assert_not_called()
            self.assertIsNone(window.preset_store.migrated_from)
            self.assertEqual(window.preset_store.path, self.presets_path)
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_weight_preset_import_export_dialogs_default_to_local_directory(self):
        with patch("src.echo_gui.QFileDialog.getOpenFileName", return_value=("", "")) as open_dialog:
            self.window.import_weights()
        self.assertEqual(open_dialog.call_args.args[2], str(self.presets_path.parent))
        with patch("src.echo_gui.QFileDialog.getSaveFileName", return_value=("", "")) as save_dialog:
            self.window.export_weights()
        self.assertEqual(save_dialog.call_args.args[2], str(self.presets_path.with_name("weight-presets-export.json")))

    def test_weight_tab_order_and_layout(self):
        tabs = self.window.findChild(QTabWidget)
        self.assertEqual([tabs.tabText(i) for i in range(tabs.count())],
                         ["副词条权重", "Cost 1 主词条权重", "Cost 3 主词条权重", "Cost 4 主词条权重", "小词条权重"])
        self.assertEqual(tabs.currentIndex(), 0)
        for tab_index, category, pairs in [(0, "sub_stats", 2), (1, "main_stats_1", 2),
                                           (2, "main_stats_3", 2), (3, "main_stats_4", 2)]:
            table = self.window.weight_tables[category]
            self.assertIs(table.parentWidget(), tabs.widget(tab_index))
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
        table = self.window.weight_tables["minor_stats"]
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
        expected = {"recovery": (.7, 1), "tuning_recovery": (.3, 1),
                "tuning_weight": (1, 1_000_000), "experience_weight": (1.6, 1_000_000),
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
        for name, value in [("tuning_recovery", .25), ("tuning_weight", 2),
                            ("experience_weight", 3), ("embryo_weight", 4)]:
            self.window.strategy_inputs[name].setValue(value)
        self.window.target.setValue(87)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        planner = ControlledPlanner.instances[-1]
        self.assertEqual(planner.params, {"recovery": .75, "tuning_recovery": .25,
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
        self.assertIn("一次性开五条达标概率", self.window.zero_strategy.text())
        self.assertIn("单个胚子成功率", self.window.zero_strategy.parentWidget().findChild(QLabel, "metricCaption").text())
        self.assertIn("开完剩余词条达标概率", self.window.current_strategy.text())
        self.assertIn("后续成功率", self.window.current_strategy.parentWidget().findChild(QLabel, "metricCaption").text())
        self.assertIn("30.00% ～ 40.00%", self.window.zero_strategy.text())
        self.assertEqual(self.window.strategy_metrics["zero_probability"].text(), "25.00%")
        self.assertEqual(self.window.current_strategy.text().count("无法获得 / ∞"), 0)
        self.assertEqual(self.window.zero_strategy.text().count("无法获得 / ∞"), 1)
        self.assertIn("建议放弃", self.window.current_strategy.text())
        self.assertIn("按材料权重比较累计成本", self.window.current_strategy.text())
        self.assertNotIn("参考阈值", self.window.current_strategy.text())
        for text in ("计算误差", "修改成本权重", "重新优化", "用户提供数据", "主动弃置回收", "70%", "30%", "500"):
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
        self.window.strategy_inputs["tuning_recovery"].setValue(2)
        self.wait_until(lambda: "策略已更新" in self.window.strategy_status.text())
        self.assertEqual(ControlledPlanner.instances[-1].params, {
            "recovery": 1, "tuning_recovery": 1, "tuning_weight": 0,
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

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path_patch = patch("src.echo_gui.weight_presets_path",
                           return_value=Path(directory.name) / "weight-presets.json")
        path_patch.start()
        self.addCleanup(path_patch.stop)
        legacy_patch = patch("src.echo_gui.legacy_weight_presets_path",
                     return_value=Path(directory.name) / "legacy" / "weight-presets.json")
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        dialog_patch = patch("src.echo_gui.QMessageBox.exec", return_value=QMessageBox.StandardButton.Discard)
        dialog_patch.start()
        self.addCleanup(dialog_patch.stop)

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
            if value is None:
                return "无法获得 / ∞"
            text = f"{value:.2f}".rstrip("0").rstrip(".")
            return "0" if text == "-0" else text

        zero, current = result["zero"], result["current"]
        self.assertEqual(window.zero_strategy.text(),
                         f"成功达标时累计成本：{amount(zero['expected_cost'])}"
                         f"\n单个胚子成功或弃置时平均成本：{amount(zero['attempt_cost'])}"
                         f"\n一次性开五条达标概率：{zero['full_success_lower']:.2%} ～ "
                         f"{zero['full_success_upper']:.2%}（估算范围）")
        recommendation = "建议继续" if current["recommend_continue"] else "建议放弃"
        continue_cost = "—" if current["continue_cost"] is None else amount(current["continue_cost"])
        restart_cost = "—" if current["restart_cost"] is None else amount(current["restart_cost"])
        self.assertEqual(window.current_strategy.text(),
             f"{recommendation}（按材料权重比较累计成本）"
                         f"\n原因：{current['reason']}"
             f"\n继续培养到成功的平均成本：{continue_cost}"
             f"\n弃置重开到成功的平均成本：{restart_cost}"
             f"\n开完剩余词条达标概率：{current['full_success_lower']:.2%} ～ "
             f"{current['full_success_upper']:.2%}（估算范围）")
        self.assertEqual(window.strategy_status.text(),
                 "策略已更新 · 累计成本策略（概率和成本为近似计算）")
        scored = window._strategy_cache["planner"].scorer.score(window.current_echo())
        self.assertEqual(window.score_label.text(), f"{scored['score']:g} 分")
        score, target = scored["score"], window.target.value()
        status = (f"已达目标 · 超出 {score - target:g} 分" if score >= target else
              f"未达目标 · 还差 {target - score:g} 分")
        if score >= target and scored["activated_count"] < 5:
            status += f" · 仍需开满剩余 {5 - scored['activated_count']} 条"
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

            self.assertIn("一次性开五条达标概率：100.00% ～ 100.00%", window.zero_strategy.text())
            self.assertEqual(window.strategy_metrics["zero_embryos"].text(), "1")
            self.assertEqual(window.strategy_metrics["zero_tuning"].text(), "50")
            self.assertEqual(window.strategy_metrics["zero_gold_tubes"].text(), "28.6")
            self.assertIn("开满剩余 5 条", window.current_strategy.text())
            self.assertIn("建议继续", window.current_strategy.text())
            self.assertIn("继续成本：278.8", window.current_strategy.text())
            self.assertNotIn("建议放弃", window.current_strategy.text())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_real_material_weight_change_rebuilds_planner_and_changes_recommendation(self):
        window = EchoWindow()
        try:
            weights = window.current_weights()
            for category in ("main_stats", "minor_stats"):
                for cost, pool in weights[category].items():
                    weights[category][cost] = dict.fromkeys(pool, 0)
            weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
            weights["main_stats"]["4"]["crit_rate"] = 1
            weights["sub_stats"]["crit_rate"] = 1
            window.apply_weights(weights)
            window.cost.setCurrentIndex(window.cost.findData(4))
            window.main_stat.setCurrentIndex(window.main_stat.findData("crit_rate"))
            window.target.setValue(40)
            window.sub_stat_buttons["hp_flat"].click()
            self.wait_for_strategy(window)

            first = window._strategy_cache["planner"]
            result = first.analyze(window.current_echo())
            self.assertEqual(first.bins, 1000)
            self.assertFalse(result["current"]["recommend_continue"])
            self.assertEqual(window.progress_status.text(), "推荐放弃")
            self.assert_results_match_api(window, result)
            self.assertAlmostEqual(result["zero"]["expected_cost"], 404.6)
            unchanged_score = window.score_label.text()

            window.strategy_inputs["tuning_weight"].setValue(10)
            window.strategy_inputs["embryo_weight"].setValue(0)
            self.wait_for_strategy(window)
            second = window._strategy_cache["planner"]
            result = second.analyze(window.current_echo())
            self.assertIsNot(first, second)
            self.assertEqual(second.bins, 1000)
            self.assertTrue(result["current"]["recommend_continue"])
            self.assertEqual(window.progress_status.text(), "推荐继续")
            self.assert_results_match_api(window, result)
            self.assertAlmostEqual(result["zero"]["expected_cost"], 1398.88)
            self.assertEqual(window.score_label.text(), unchanged_score)
            self.assertLess(result["current"]["continue_cost"], result["current"]["restart_cost"])
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
                        # 仅测试注入降低分辨率，仍执行真实构建/概率/累计成本算法。
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
                            self.assertEqual(window.zero_strategy.text().count("无法获得 / ∞"), 1)
                            self.assertFalse(result["current"]["recommend_continue"])
                    finally:
                        # 注入保持有效直到在途任务结束，避免影响后续测试。
                        window.close()
                        window.deleteLater()
                        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()