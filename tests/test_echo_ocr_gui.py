"""假截图与可控引擎驱动的离屏 OCR 工作流测试。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QObject, QSaveFile, Qt, Signal
from PySide6.QtWidgets import QApplication

from src.echo_capture import CaptureError, CaptureRegion, CaptureSnapshot, Rect, ScreenInfo
from src.echo_gui import EchoWindow
from src.echo_ocr import OcrError, OcrToken, SubStatEntry
from src.echo_ocr_gui import OcrSettings, OcrSettingsStore


REGION = CaptureRegion(ScreenInfo("test", "", "", "", Rect(-3000, 0, 1440, 900), 1.5),
                       Rect(500, 300, 240, 160))
EXAMPLE = (SubStatEntry("crit_rate", 10.5), SubStatEntry("crit_dmg", 21))


def tokens(*lines):
    return [OcrToken(text, (1, index * 24, 150, index * 24 + 14), .98)
            for index, text in enumerate(lines)]


class ControlledEngine:
    def __init__(self):
        self.started = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.calls = []
        self.output = tokens("暴击率 +10.5%", "固定攻击 +40",
                             "暴击率 +10.5%", "暴击伤害 +21%")
        self.error = None

    def recognize(self, image):
        self.calls.append((threading.get_ident(), image.shape, list(image[0, 0])))
        self.started.set()
        if not self.gate.wait(5):
            raise RuntimeError("test OCR timeout")
        if self.error:
            raise self.error
        return self.output


class ControlledPicker(QObject):
    finished = Signal(object, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.disposed = False

    def start(self):
        pass

    def dispose(self):
        self.disposed = True
        self.deleteLater()


class EchoOcrGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.presets_path = self.directory / "weight-presets.json"
        self.settings_path = self.directory / "ocr-settings.json"
        OcrSettingsStore(self.settings_path).save(OcrSettings(REGION))
        self.engine = ControlledEngine()
        self.start_patch("src.echo_ocr_gui.LocalOcrEngine", return_value=self.engine)
        self.start_patch("src.echo_ocr_gui.resolve_screen")
        self.capture = self.start_patch("src.echo_ocr_gui.capture_region", side_effect=self.snapshot)
        self.start_patch("src.echo_ocr_gui.RegionPicker", side_effect=ControlledPicker)
        self.start_patch("src.echo_gui.EchoWindow._start_strategy", lambda _window: None)
        self.window = EchoWindow(presets_path=self.presets_path)
        self.window.show()
        self.app.processEvents()
        self.ocr = self.window.ocr

    def start_patch(self, *args, **kwargs):
        patcher = patch(*args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.engine.gate.set()
        self.window._weights_dirty = False
        self.window.close()
        self.wait_until(lambda: not self.window.isVisible())
        self.window.deleteLater()
        self.app.processEvents()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate(), self.ocr.status.text())

    def snapshot(self, region, version):
        self.assertEqual(threading.get_ident(), threading.main_thread().ident)
        return CaptureSnapshot(bytes([23, 100, 200, 255]) * 60, 10, 6, time.time(), version)

    def recognize(self):
        self.ocr.recognize_button.click()
        self.wait_until(lambda: self.ocr._preview is not None)
        return self.ocr._preview

    def confirm(self, preview):
        self.assertTrue(preview.confirm.isEnabled(), preview.validation.text())
        preview.confirm.click()

    def test_require_configuration_and_keep_manual_available(self):
        other = self.directory / "other.json"
        window = EchoWindow(presets_path=self.presets_path, ocr_settings_path=other)
        try:
            self.assertFalse(window.ocr.recognize_button.isEnabled())
            window.ocr.recognize()
            self.capture.assert_not_called()
            self.assertIn("先设置", window.ocr.status.text())
            window.sub_stat_buttons["def_flat"].setChecked(True)
            self.assertEqual(window.current_echo()["sub_stats"], [{"name": "def_flat", "value": 40}])
        finally:
            window.close()
            window.deleteLater()

    def test_preview_confirmation_atomically_replaces_and_recalculates_once(self):
        old = [SubStatEntry("atk_flat", 40), SubStatEntry("hp_flat", 320)]
        self.window.apply_ocr_sub_stats(old)
        previous = self.window.current_echo()
        self.window.reconstruction_budget.setValue(3)
        preview = self.recognize()
        self.assertIs(self.ocr.pages.currentWidget(), preview)
        self.assertIs(self.window.sub_stat_tabs.currentWidget(), self.ocr.panel)
        self.assertFalse(hasattr(preview, "actual_count"))
        self.assertFalse(hasattr(preview, "checked"))
        self.assertEqual(self.window.current_echo(), previous)
        self.assertIn("暴击伤害", preview.raw.toPlainText())
        with patch.object(self.window, "calculate", wraps=self.window.calculate) as calculate, \
                patch.object(self.window.sub_tier_inputs["crit_rate"], "showPopup") as popup:
            self.confirm(preview)
            self.app.processEvents()
            calculate.assert_called_once_with()
            popup.assert_not_called()
        imported = self.window.current_echo()
        self.assertEqual(imported["sub_stats"], [{"name": "crit_rate", "value": 10.5},
                                                 {"name": "crit_dmg", "value": 21}])
        self.assertEqual(imported["main_stat"], previous["main_stat"])
        self.assertEqual(imported["cost"], previous["cost"])
        self.assertEqual(self.window.selected_count.text(), "2 / 5")
        self.assertFalse(self.window.sub_stat_buttons["atk_flat"].isChecked())
        self.assertFalse(self.window.sub_tier_inputs["hp_flat"].isEnabled())
        self.assertTrue(self.window.sub_tier_inputs["crit_rate"].isEnabled())
        self.assertEqual(self.window.reconstruction_budget.value(), 3)
        self.assertTrue(all(not button.isEnabled() for button in self.window.reconstruction_lock_buttons))
        score = self.window._score_result
        pending = self.window._strategy_pending[1]
        self.window.clear_sub_stats()
        for entry in EXAMPLE:
            self.window.sub_stat_buttons[entry.stat_id].setChecked(True)
            tier = self.window.sub_tier_inputs[entry.stat_id]
            tier.setCurrentIndex(tier.findData(entry.value))
        self.assertEqual(self.window.current_echo(), imported)
        self.assertEqual(self.window._score_result, score)
        self.assertEqual(self.window._strategy_pending[1], pending)

    def test_can_correct_invalid_unit_without_manually_selecting_open_count(self):
        self.engine.output = tokens("暴击率 +10.5%", "固定攻击 +40", "暴击率 +105%",
                                    "暴击伤害 +21%", "共鸣效率 +12.4%")
        preview = self.recognize()
        self.assertFalse(preview.confirm.isEnabled())
        first_tier = preview.rows[0][2]
        first_tier.setCurrentIndex(first_tier.findData(10.5))
        _, third_stat, third_tier = preview.rows[2]
        third_stat.setCurrentIndex(third_stat.findData("crit_rate"))
        third_tier.setCurrentIndex(third_tier.findData(10.5))
        self.assertIn("重复", preview.validation.text())
        third_stat.setCurrentIndex(third_stat.findData("atk_flat"))
        third_tier.setCurrentIndex(third_tier.findData(50))
        self.confirm(preview)
        self.assertEqual(self.window.selected_sub_stats, ["crit_rate", "crit_dmg", "atk_flat"])
        self.assertEqual(self.window.sub_tier_inputs["atk_flat"].currentData(), 50)

    def test_five_recognized_sub_stats_auto_fill_header_and_recalculate_once(self):
        self.engine.output = tokens(
            "攻击百分比 +33%", "固定攻击 +150",
            "暴击率 +10.5%", "暴击伤害 +21%", "共鸣效率 +12.4%",
            "固定生命 +320", "固定防御 +50",
        )
        preview = self.recognize()
        self.assertEqual(len(preview.rows), 5)
        self.assertIn("Cost 4", preview.summary.text())
        self.assertTrue(preview.confirm.isEnabled(), preview.validation.text())
        with patch.object(self.window, "calculate", wraps=self.window.calculate) as calculate:
            self.confirm(preview)
            self.app.processEvents()
            calculate.assert_called_once_with()
        echo = self.window.current_echo()
        self.assertEqual(echo["cost"], 4)
        self.assertEqual(echo["main_stat"]["name"], "atk_pct")
        self.assertEqual(self.window.selected_count.text(), "5 / 5")
        self.assertEqual(len(echo["sub_stats"]), 5)
        self.assertIn("已回填词条", self.ocr.status.text())

    def test_game_names_and_unique_missing_character_apply_cost_and_main(self):
        examples = (
            ("攻击 18%", "生命 2280", 1, "atk_pct"),
            ("灭伤害加成 30%", "攻击 100", 3, "havoc_dmg_bonus"),
            ("暴击伤害 44%", "攻击 150", 4, "crit_dmg"),
        )
        for main, minor, cost, main_id in examples:
            with self.subTest(main=main, minor=minor):
                self.engine.output = tokens(main, minor, "暴击率 10.5%")
                preview = self.recognize()
                self.confirm(preview)
                self.assertEqual(self.window.current_echo()["cost"], cost)
                self.assertEqual(self.window.current_echo()["main_stat"]["name"], main_id)

    def test_same_selection_does_not_reorder_or_recalculate(self):
        self.window.apply_ocr_sub_stats(EXAMPLE[::-1])
        version = self.ocr.input_version
        preview = self.recognize()
        with patch.object(self.window, "calculate") as calculate:
            self.confirm(preview)
            calculate.assert_not_called()
        self.assertEqual(self.ocr.input_version, version)
        self.assertEqual(self.window.selected_sub_stats, ["crit_dmg", "crit_rate"])
        self.assertIn("一致", self.ocr.status.text())

    def test_cancel_and_structural_failures_never_modify_input(self):
        self.window.apply_ocr_sub_stats([SubStatEntry("def_flat", 50)])
        before = self.window.current_echo()
        self.recognize().cancel()
        self.assertIn("已取消", self.ocr.status.text())
        for output in ([], tokens("暴击率 +10.5%", "固定攻击 +40", "暴击率 +10.5%",
                                  "暴击伤害 +21%", "共鸣效率 +12.4%", "固定生命 +320",
                                  "固定攻击 +40", "固定防御 +50"),
                       tokens("主词条", "暴击率 +10.5%")):
            with self.subTest(output=output):
                self.engine.output = output
                preview = self.recognize()
                self.assertFalse(preview.confirm.isEnabled())
                preview.accept()
                self.assertEqual(self.window.current_echo(), before)

    def test_ambiguous_cost_result_cannot_be_applied(self):
        self.engine.output = tokens(
            "攻击百分比", "固定攻击",
            "暴击率 +10.5%", "暴击伤害 +21%",
        )
        before = self.window.current_echo()
        preview = self.recognize()
        self.assertIn("Cost 无法", preview.notice.text())
        self.assertFalse(preview.confirm.isEnabled())
        preview.cancel()
        self.assertEqual(self.window.current_echo(), before)

    def test_task_error_reports_category_and_preserves_input(self):
        self.window.apply_ocr_sub_stats(EXAMPLE)
        self.engine.error = OcrError("OCR 模型缺失", "model not found")
        self.ocr.recognize()
        self.wait_until(self.ocr.recognize_button.isEnabled)
        self.assertIn("模型缺失", self.ocr.status.text())
        self.assertIsNone(self.ocr._preview)
        self.assertEqual(self.window.selected_sub_stats, ["crit_rate", "crit_dmg"])

    def test_one_task_and_input_changes_discard_old_result(self):
        changes = {
            "Cost": lambda: self.window.cost.setCurrentIndex(0),
            "main": lambda: self.window.main_stat.setCurrentIndex(0),
            "tier": lambda: self.window.sub_tier_inputs["crit_rate"].setCurrentIndex(0),
            "clear": self.window.clear_sub_stats,
        }
        self.window.apply_ocr_sub_stats(EXAMPLE)
        for field, edit in changes.items():
            with self.subTest(field=field):
                self.engine.started.clear()
                self.engine.gate.clear()
                self.ocr.recognize()
                self.wait_until(self.engine.started.is_set)
                self.assertFalse(self.ocr.recognize_button.isEnabled())
                self.ocr.recognize()
                edit()
                changed = self.window.current_echo()
                self.engine.gate.set()
                self.wait_until(self.ocr.recognize_button.isEnabled)
                self.assertIsNone(self.ocr._preview)
                self.assertIn("过期", self.ocr.status.text())
                self.assertEqual(self.window.current_echo(), changed)
        self.assertEqual(self.capture.call_count, len(changes))
        self.assertEqual(len(self.engine.calls), len(changes))
        self.assertTrue(all(call[0] != threading.get_ident() for call in self.engine.calls))
        self.assertEqual(self.engine.calls[0][1:], ((6, 10, 3), [200, 100, 23]))

    def test_confirmation_rechecks_version(self):
        preview = self.recognize()
        self.assertTrue(preview.confirm.isEnabled())
        self.window.clear_sub_stats()
        self.assertFalse(preview.confirm.isEnabled())
        preview.cancel()
        self.assertIn("过期", self.ocr.status.text())
        self.assertFalse(self.window.selected_sub_stats)

    def test_reselect_invalidates_inflight_request_and_persists_new_region(self):
        self.engine.gate.clear()
        self.ocr.recognize()
        self.wait_until(self.engine.started.is_set)
        self.ocr.select_button.click()
        self.wait_until(lambda: self.ocr._picker is not None)
        picker = self.ocr._picker
        new_region = CaptureRegion(REGION.screen, Rect(100, 100, 180, 90))
        picker.finished.emit(new_region, "")
        self.assertTrue(picker.disposed)
        self.assertEqual(self.ocr.region_version, 1)
        self.assertEqual(OcrSettingsStore(self.settings_path).load().region, new_region)
        self.assertFalse(self.ocr.recognize_button.isEnabled())
        self.engine.gate.set()
        self.wait_until(self.ocr.recognize_button.isEnabled)
        self.assertIsNone(self.ocr._preview)
        self.assertFalse(self.window.selected_sub_stats)

    def test_cancel_and_failed_selection_restore_maximized_top_window(self):
        self.window.showMaximized()
        self.ocr.on_top.setChecked(True)
        normal = self.window.normalGeometry()
        for error in ("", "截图权限拒绝"):
            with self.subTest(error=error):
                self.ocr.select_region()
                self.assertTrue(self.window.isMinimized())
                self.wait_until(lambda: self.ocr._picker is not None)
                self.ocr._picker.finished.emit(None, error)
                self.assertTrue(self.window.isMaximized())
                self.assertEqual(self.window.normalGeometry(), normal)
                self.assertTrue(self.window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
                self.assertEqual(self.ocr.settings.region, REGION)
                self.assertEqual(OcrSettingsStore(self.settings_path).load().region, REGION)

    def test_overlapping_capture_minimizes_even_at_reduced_opacity_and_restores_on_error(self):
        screen = ScreenInfo("test", "", "", "", Rect(0, 0, 2000, 2000), 1)
        self.ocr.select_region()
        self.wait_until(lambda: self.ocr._picker is not None)
        x, y = self.window.normalGeometry().x() + 200, self.window.normalGeometry().y() + 200
        self.ocr._picker.finished.emit(CaptureRegion(screen, Rect(x, y, 100, 120)), "")
        self.ocr.opacity.setValue(75)
        geometry = self.window.geometry()
        def capture_error(*_args):
            self.assertTrue(self.window.isMinimized())
            raise CaptureError("cannot capture")
        self.capture.side_effect = capture_error
        self.ocr.recognize()
        self.assertTrue(self.window.isMinimized())
        self.wait_until(self.ocr.recognize_button.isEnabled)
        self.assertFalse(self.window.isMinimized())
        self.assertEqual(self.window.geometry(), geometry)
        self.assertIn("cannot capture", self.ocr.status.text())

    def test_close_cancellation_and_event_driven_exit_while_engine_is_busy(self):
        self.engine.gate.clear()
        self.ocr.recognize()
        self.wait_until(self.engine.started.is_set)
        self.window._weights_dirty = True
        with patch.object(self.window, "_confirm_unsaved_weights", return_value=False) as confirm:
            self.assertFalse(self.window.close())
            confirm.assert_called_once()
            self.assertFalse(self.window._closing)
        with patch.object(self.window, "_confirm_unsaved_weights", return_value=True) as confirm:
            self.assertFalse(self.window.close())
            self.assertTrue(self.window._closing)
            self.assertTrue(self.window.isVisible())
            self.assertIn("正在结束识别", self.window.statusBar().currentMessage())
            self.engine.gate.set()
            self.wait_until(lambda: not self.window.isVisible())
            confirm.assert_called_once()
        self.assertIsNone(self.ocr._preview)
        self.assertIsNone(self.ocr._task)
        self.assertFalse(self.window.selected_sub_stats)

    def test_apply_prevalidates_and_rolls_back_widget_changes(self):
        self.window.apply_ocr_sub_stats(EXAMPLE)
        before = self.window.current_echo()
        version = self.ocr.input_version
        button = self.window.sub_stat_buttons["crit_rate"]
        button.blockSignals(True)
        with patch.object(self.window, "calculate") as calculate:
            with self.assertRaises(ValueError):
                self.window.apply_ocr_sub_stats([SubStatEntry("atk_flat", 41)])
            tier = self.window.sub_tier_inputs["atk_flat"]
            original = tier.setCurrentIndex
            def reject_target(index):
                if index == tier.findData(60):
                    raise RuntimeError("test")
                original(index)
            with patch.object(tier, "setCurrentIndex", side_effect=reject_target):
                with self.assertRaisesRegex(RuntimeError, "test"):
                    self.window.apply_ocr_sub_stats([SubStatEntry("atk_flat", 60)])
            calculate.assert_not_called()
        self.assertEqual(self.window.current_echo(), before)
        self.assertEqual(self.window.selected_count.text(), "2 / 5")
        self.assertEqual(self.ocr.input_version, version)
        self.assertTrue(button.signalsBlocked())
        self.assertFalse(self.window.updating)
        self.assertFalse(self.window.sub_stat_buttons["atk_flat"].isChecked())

    def test_window_settings_round_trip_reset_and_unwritable_feedback(self):
        self.ocr.opacity.setValue(60)
        self.ocr.on_top.setChecked(True)
        self.assertEqual(OcrSettingsStore(self.settings_path).load(), OcrSettings(REGION, 60, True))
        self.ocr.reset.click()
        self.assertEqual(OcrSettingsStore(self.settings_path).load(), OcrSettings(REGION))
        self.ocr.store.path = self.directory / "missing-dir" / "ocr-settings.json"
        self.ocr.on_top.setChecked(True)
        self.assertIn("未保存", self.ocr.settings_status.text())
        self.assertTrue(self.ocr.settings.on_top)


class OcrSettingsTests(unittest.TestCase):
    def test_missing_malformed_unknown_version_and_atomic_failure(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "ocr-settings.json"
            self.assertEqual(OcrSettingsStore(path).load(), OcrSettings())
            for content in ("invalid json", json.dumps({"schema_version": 2})):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    store = OcrSettingsStore(path)
                    self.assertEqual(store.load(), OcrSettings())
                    self.assertIn("重新配置", store.read_error)
                    with self.assertRaises(OSError):
                        store.save(OcrSettings(REGION))
                    self.assertEqual(path.read_text(encoding="utf-8"), content)
            store = OcrSettingsStore(path)
            store.save(OcrSettings(REGION))
            before = path.read_bytes()

            class CancelledSave(QSaveFile):
                def commit(self):
                    self.cancelWriting()
                    return super().commit()

            with patch("src.echo_ocr_gui.QSaveFile", CancelledSave), self.assertRaises(OSError):
                store.save(OcrSettings(REGION, 65, True))
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
