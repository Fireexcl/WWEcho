"""截图识别的设置、任务生命周期与校正确认界面。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

from PySide6.QtCore import QIODevice, QObject, QRunnable, QSaveFile, QSignalBlocker, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from src.echo_capture import (
    CaptureError, CaptureRegion, CaptureSnapshot, Rect, RegionPicker, ScreenInfo,
    WindowAppearance, capture_region, resolve_screen,
)
from src.echo_ocr import (
    OCR_MAX_STATS, LocalOcrEngine, OcrError, OcrResult, OcrToken, SubStatEntry,
    parse_sub_stats, validate_selection,
)

if TYPE_CHECKING:
    from numpy import ndarray


@dataclass(frozen=True)
class OcrSettings:
    region: CaptureRegion | None = None
    opacity: int = 100
    on_top: bool = False


def _read_rect(data: dict[str, Any]) -> Rect:
    keys = ("x", "y", "width", "height")
    if not isinstance(data, dict) or any(type(data.get(key)) is not int for key in keys):
        raise ValueError("坐标矩形无效")
    return Rect(data["x"], data["y"], data["width"], data["height"])


class OcrSettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.read_error = ""

    def load(self) -> OcrSettings:
        self.read_error = ""
        try:
            with self.path.open(encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
                raise ValueError("不支持的识别设置版本")
            opacity, on_top, saved = data["opacity"], data["on_top"], data["region"]
            if type(opacity) is not int or not 50 <= opacity <= 100 or type(on_top) is not bool:
                raise ValueError("透明度或置顶设置无效")
            region = None
            if saved is not None:
                screen = saved["screen"]
                name, manufacturer, model, serial, scale = (
                    screen[key] for key in ("name", "manufacturer", "model", "serial", "scale"))
                if not all(isinstance(item, str) for item in (name, manufacturer, model, serial)):
                    raise ValueError("屏幕标识无效")
                if type(scale) not in (int, float):
                    raise ValueError("屏幕缩放无效")
                region = CaptureRegion(
                    ScreenInfo(name, manufacturer, model, serial, _read_rect(screen["geometry"]), scale),
                    _read_rect(saved["rect"]),
                )
            return OcrSettings(region, opacity, on_top)
        except FileNotFoundError:
            return OcrSettings()
        except (OSError, ValueError, KeyError, TypeError, OverflowError, CaptureError) as exc:
            self.read_error = f"识别设置无法读取（{exc}）。请移走原文件后重启并重新配置：{self.path}"
            return OcrSettings()

    def save(self, settings: OcrSettings) -> None:
        if self.read_error:
            raise OSError(self.read_error)
        payload = json.dumps({"schema_version": 1, **asdict(settings)},
                             ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
        file = QSaveFile(str(self.path))
        file.setDirectWriteFallback(False)
        if not file.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(file.errorString())
        if file.write(payload) != len(payload):
            file.cancelWriting()
            raise OSError(file.errorString())
        if not file.commit():
            raise OSError(file.errorString())


@dataclass(frozen=True)
class RequestStamp:
    number: int
    input_version: int
    region_version: int


@dataclass(frozen=True)
class OcrRequest:
    stamp: RequestStamp
    snapshot: CaptureSnapshot


@dataclass(frozen=True)
class OcrFailure:
    category: str
    message: str


@dataclass(frozen=True)
class OcrOutcome:
    stamp: RequestStamp
    payload: OcrResult | OcrFailure


class OcrEngine(Protocol):
    def recognize(self, image_array: ndarray) -> list[OcrToken]: ...


class _TaskSignals(QObject):
    finished = Signal(object)


class _OcrTask(QRunnable):
    def __init__(self, request: OcrRequest, engine: OcrEngine, stats: dict[str, Any]) -> None:
        super().__init__()
        self.stamp = request.stamp
        self.snapshot: CaptureSnapshot | None = request.snapshot
        self.engine = engine
        self.stats = stats
        self.signals = _TaskSignals()

    def run(self) -> None:
        snapshot, self.snapshot = self.snapshot, None
        assert snapshot is not None
        payload: OcrResult | OcrFailure
        try:
            import numpy as np

            rgba = np.frombuffer(snapshot.rgba, dtype=np.uint8).reshape(snapshot.height, snapshot.width, 4)
            image = np.ascontiguousarray(rgba[:, :, 2::-1])  # OpenCV BGR
            payload = parse_sub_stats(self.engine.recognize(image), self.stats)
        except OcrError as exc:
            payload = OcrFailure(exc.category, str(exc))
        except Exception as exc:
            payload = OcrFailure("识别任务失败", f"{type(exc).__name__}：{exc}")
        self.signals.finished.emit(OcrOutcome(self.stamp, payload))


class OcrPreview(QDialog):
    def __init__(self, parent: QWidget, snapshot: CaptureSnapshot,
                 result: OcrResult, stats: dict[str, Any], stamp: RequestStamp) -> None:
        super().__init__(parent)
        self._stats = stats
        self.result_data = result
        self.stamp = stamp
        self._invalidated = False
        self.setWindowTitle("核对截图识别结果")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        hint = QLabel("仅替换副词条；沿用当前 Cost 和主词条，不判断是否为同一只声骸。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        evidence = QHBoxLayout()
        picture = QLabel()
        picture.setPixmap(QPixmap.fromImage(snapshot.qimage()))
        picture.adjustSize()
        scroll = QScrollArea()
        scroll.setWidget(picture)
        scroll.setMinimumHeight(140)
        evidence.addWidget(scroll, 3)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlainText("\n".join(result.raw_lines) or "（未识别到文字）")
        evidence.addWidget(self.raw, 2)
        layout.addLayout(evidence, 1)
        notice = QLabel("\n".join((result.completeness.value,) + result.blocking_issues + result.review_issues))
        notice.setWordWrap(True)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(notice)

        self.actual_count = QComboBox()
        self.actual_count.addItem("请选择原图已开条数", 0)
        min_count = max(1, len(result.rows))
        for count in range(min_count, OCR_MAX_STATS + 1):
            self.actual_count.addItem(f"原图已开 {count} 条", count)
        self.actual_count.setEnabled(not result.blocking_issues)
        layout.addWidget(self.actual_count)
        grid = QGridLayout()
        layout.addLayout(grid)
        self.rows: list[tuple[QLabel, QComboBox, QComboBox]] = []
        if not result.blocking_issues:
            for index in range(OCR_MAX_STATS):
                row = result.rows[index] if index < len(result.rows) else None
                label = QLabel(" · ".join((row.raw_text, *row.issues)) if row else "补录漏识别项")
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.PlainText)
                stat = QComboBox()
                stat.addItem("请选择副词条", None)
                for stat_id in stats["sub_stats"]["tiers"]:
                    stat.addItem(stats["stat_definitions"][stat_id]["name_zh"], stat_id)
                tier = QComboBox()
                self.rows.append((label, stat, tier))
                for col, widget in enumerate(self.rows[-1]):
                    grid.addWidget(widget, index, col)
                if row is not None and row.stat_id is not None:
                    stat.setCurrentIndex(stat.findData(row.stat_id))
                self._refresh_tiers(index)
                if row is not None and row.value is not None:
                    tier.setCurrentIndex(tier.findData(row.value))
                stat.currentIndexChanged.connect(lambda _index, i=index: self._change_stat(i))
                tier.currentIndexChanged.connect(self._update_confirm)
        elif result.rows:
            self.raw.appendPlainText("\n解析行（不可应用）：\n" + "\n".join(
                " · ".join((row.raw_text, *row.issues)) for row in result.rows))

        self.checked = QCheckBox("我已对照截图核对：这是指定开孔页面，全部已开副词条都已录入，且未开满五条。")
        self.checked.setEnabled(not result.blocking_issues)
        layout.addWidget(self.checked)
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.confirm = buttons.addButton("确认替换", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("取消")
        cancel.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.actual_count.currentIndexChanged.connect(self._update_confirm)
        self.checked.toggled.connect(self._update_confirm)
        self._update_confirm()

    def _refresh_tiers(self, index: int) -> None:
        _, stat, tier = self.rows[index]
        stat_id = stat.currentData()
        with QSignalBlocker(tier):
            tier.clear()
            tier.addItem("请选择档位", None)
            if stat_id is not None:
                suffix = "%" if self._stats["stat_definitions"][stat_id]["unit"] == "percentage_points" else ""
                for rank, value in enumerate(self._stats["sub_stats"]["tiers"][stat_id]["values"], 1):
                    tier.addItem(f"{rank}档 · {value:g}{suffix}", value)
        tier.setEnabled(stat_id is not None)

    def _change_stat(self, index: int) -> None:
        self._refresh_tiers(index)
        self._update_confirm()

    def selection(self) -> tuple[SubStatEntry, ...]:
        if self._invalidated:
            raise ValueError("输入或识别区已变更，请取消后重新识别。")
        if self.result_data.blocking_issues:
            raise ValueError("请重新截图或调整识别区域。")
        count = self.actual_count.currentData()
        if not max(1, len(self.result_data.rows)) <= count <= OCR_MAX_STATS:
            raise ValueError("请先选择截图中实际已开的条数；不能删减识别到的可能词条行。")
        entries = []
        for index, (_, stat, tier) in enumerate(self.rows[:count], 1):
            stat_id, value = stat.currentData(), tier.currentData()
            if stat_id is None or value is None:
                raise ValueError(f"请补全第 {index} 条的词条和档位。")
            entries.append(SubStatEntry(stat_id, value))
        entries = validate_selection(entries, self._stats)
        if not self.checked.isChecked():
            raise ValueError("请核对截图并勾选确认。")
        return entries

    def _update_confirm(self) -> None:
        count = max(len(self.result_data.rows), self.actual_count.currentData())
        for index, widgets in enumerate(self.rows):
            for widget in widgets:
                widget.setVisible(index < count)
        try:
            self.selection()
        except ValueError as exc:
            self.validation.setText(str(exc))
            self.confirm.setEnabled(False)
        else:
            self.validation.clear()
            self.confirm.setEnabled(True)

    def invalidate(self) -> None:
        self._invalidated = True
        self._update_confirm()

    def accept(self) -> None:
        self._update_confirm()
        if self.confirm.isEnabled():
            super().accept()


class _State(Enum):
    UNCONFIGURED = auto()
    READY = auto()
    SELECTING = auto()
    CAPTURING = auto()
    RECOGNIZING = auto()
    REVIEW = auto()
    APPLYING = auto()
    CLOSING = auto()


class OcrController(QObject):
    close_ready = Signal()

    def __init__(self, window: QMainWindow, stats: dict[str, Any], settings_path: Path,
                 apply_selection: Callable[[Sequence[SubStatEntry]], bool]) -> None:
        super().__init__(window)
        self.window = window
        self.stats = deepcopy(stats)
        self._apply = apply_selection
        self.store = OcrSettingsStore(settings_path)
        self.settings = self.store.load()
        self.input_version = 0
        self.region_version = 0
        self._request_number = 0
        self._current: RequestStamp | None = None
        self._task: _OcrTask | None = None
        self._snapshot: CaptureSnapshot | None = None
        self._preview: OcrPreview | None = None
        self._picker: RegionPicker | None = None
        self._appearance: WindowAppearance | None = None
        self._engine: OcrEngine | None = LocalOcrEngine()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._repaint_timer = self._timer(250, self._after_desktop_repaint)
        self._save_timer = self._timer(250, self._save_settings)
        self._close_timer = self._timer(20, self._poll_close)

        self.panel = QWidget(window)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.select_button = QPushButton("设置识别区域")
        self.recognize_button = QPushButton("截图识别")
        actions.addWidget(self.select_button)
        actions.addWidget(self.recognize_button)
        actions.addStretch()
        layout.addLayout(actions)
        appearance = QHBoxLayout()
        self.opacity_text = QLabel()
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(50, 100)
        self.opacity.setAccessibleName("窗口透明度")
        self.opacity.setValue(self.settings.opacity)
        self.on_top = QCheckBox("置顶")
        self.on_top.setChecked(self.settings.on_top)
        self.reset = QPushButton("恢复窗口默认")
        appearance.addWidget(self.opacity_text)
        appearance.addWidget(self.opacity, 1)
        appearance.addWidget(self.on_top)
        appearance.addWidget(self.reset)
        layout.addLayout(appearance)
        self.status = QLabel()
        self.settings_status = QLabel()
        for label in (self.status, self.settings_status):
            label.setObjectName("muted")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        self.settings_status.setText(self.store.read_error)
        self.settings_status.setVisible(bool(self.store.read_error))
        self._show_opacity()
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.settings.on_top)
        self._idle("已加载识别区。截图后需核对并确认替换。" if self.settings.region else
                   "尚未设置识别区。请框选可见游戏中的完整副词条列表。")
        self.select_button.clicked.connect(self.select_region)
        self.recognize_button.clicked.connect(self.recognize)
        self.opacity.valueChanged.connect(self._change_opacity)
        self.on_top.toggled.connect(self._change_on_top)
        self.reset.clicked.connect(self._reset_appearance)

    def _timer(self, interval: int, callback: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(callback)
        return timer

    def _save_settings(self) -> None:
        self._save_timer.stop()
        try:
            self.store.save(self.settings)
        except OSError as exc:
            self.settings_status.setText(f"当前会话设置未保存：{exc}")
            self.settings_status.show()
        else:
            self.settings_status.hide()

    def _show_opacity(self) -> None:
        self.opacity_text.setText(f"透明度 {self.settings.opacity}%")
        self.window.setWindowOpacity(self.settings.opacity / 100)

    def _change_opacity(self, value: int) -> None:
        self.settings = replace(self.settings, opacity=value)
        self._show_opacity()
        self._save_timer.start()

    def _change_on_top(self, checked: bool) -> None:
        self.settings = replace(self.settings, on_top=checked)
        WindowAppearance.from_window(self.window).restore(self.window, on_top=checked)
        self._save_settings()

    def _reset_appearance(self) -> None:
        with QSignalBlocker(self.opacity), QSignalBlocker(self.on_top):
            self.opacity.setValue(100)
            self.on_top.setChecked(False)
        self._change_opacity(100)
        self._change_on_top(False)

    def _set_state(self, state: _State, message: str) -> None:
        self._state = state
        idle = state in (_State.UNCONFIGURED, _State.READY)
        self.select_button.setEnabled(idle or state is _State.RECOGNIZING)
        self.recognize_button.setEnabled(state is _State.READY and self._task is None)
        for control in (self.opacity, self.on_top, self.reset):
            control.setEnabled(idle or state is _State.RECOGNIZING)
        self.status.setText(message)

    def _idle(self, message: str) -> None:
        state = _State.UNCONFIGURED if self.settings.region is None else _State.READY
        self._set_state(state, message)

    def _hide_window(self) -> None:
        self._appearance = WindowAppearance.from_window(self.window)
        self.window.showMinimized()
        self._repaint_timer.start()

    def _restore_window(self) -> None:
        if self._appearance is not None:
            appearance, self._appearance = self._appearance, None
            appearance.restore(self.window)

    def select_region(self) -> None:
        if not self.select_button.isEnabled():
            return
        self._current = None
        self._snapshot = None
        self._set_state(_State.SELECTING, "正在准备框选…")
        self._hide_window()

    def _after_desktop_repaint(self) -> None:
        if self._state is _State.SELECTING:
            self._picker = RegionPicker(self)
            self._picker.finished.connect(self._finish_selection)
            try:
                self._picker.start()
            except (RuntimeError, OSError, ValueError) as exc:
                self._finish_selection(None, str(exc))
        elif self._state is _State.CAPTURING:
            self._capture_and_recognize()

    @Slot(object, str)
    def _finish_selection(self, region: CaptureRegion | None, error: str) -> None:
        if self._picker is not None:
            picker, self._picker = self._picker, None
            picker.dispose()
        self._restore_window()
        if self._state is not _State.SELECTING:
            return
        if region is not None:
            try:
                resolve_screen(region)
            except CaptureError as exc:
                error = str(exc)
            else:
                self.region_version += 1
                self.settings = replace(self.settings, region=region)
                self._save_settings()
                self._idle("识别区已设置。请选择开孔过程中的可见画面再截图。")
                return
        self._idle(f"框选失败：{error}" if error else "已取消框选，原设置未改变。")

    def input_changed(self) -> None:
        self.input_version += 1
        if self._state is _State.RECOGNIZING:
            self._snapshot = None
            self.status.setText("录入已变更，正在执行的识别结果将丢弃。")
        if self._preview is not None:
            self._preview.invalidate()

    def recognize(self) -> None:
        region = self.settings.region
        if region is None:
            if self._state is _State.UNCONFIGURED:
                self.status.setText("请先设置识别区域。")
            return
        if self._state is not _State.READY or self._task is not None:
            return
        try:
            resolve_screen(region)
        except CaptureError as exc:
            self._idle(f"截图失败：{exc}")
            return
        self._request_number += 1
        self._current = RequestStamp(self._request_number, self.input_version, self.region_version)
        self._set_state(_State.CAPTURING, "正在截图…")
        if self.window.isVisible() and self.window.frameGeometry().intersects(region.desktop_rect):
            self._hide_window()
        else:
            self._capture_and_recognize()

    def _is_current(self, stamp: RequestStamp) -> bool:
        return (self._state is not _State.CLOSING and self._current == stamp
                and stamp.input_version == self.input_version and stamp.region_version == self.region_version)

    def _capture_and_recognize(self) -> None:
        stamp, region, engine = self._current, self.settings.region, self._engine
        assert stamp is not None and region is not None and engine is not None
        error = ""
        snapshot = None
        try:
            if self._is_current(stamp):
                snapshot = capture_region(region, stamp.region_version)
        except (RuntimeError, OSError, ValueError) as exc:
            error = str(exc)
        finally:
            self._restore_window()
        if self._state is _State.CLOSING:
            return
        if error or snapshot is None or not self._is_current(stamp) or snapshot.region_version != stamp.region_version:
            self._current = None
            self._idle(f"截图失败：{error}" if error else "截图已过期，请重新识别。")
            return
        self._snapshot = snapshot
        task = _OcrTask(OcrRequest(stamp, snapshot), engine, self.stats)
        task.signals.finished.connect(self._recognized, Qt.ConnectionType.QueuedConnection)
        self._task = task
        self._set_state(_State.RECOGNIZING, "正在本机识别…首次加载模型可能较慢。")
        self._pool.start(task)

    @Slot(object)
    def _recognized(self, outcome: OcrOutcome) -> None:
        if self._task is None or self._task.stamp != outcome.stamp:
            return
        self._task = None
        snapshot, self._snapshot = self._snapshot, None
        if self._state in (_State.CLOSING, _State.SELECTING):
            return
        if not self._is_current(outcome.stamp) or snapshot is None:
            if self._state in (_State.RECOGNIZING, _State.READY):
                self._current = None
                self._idle("识别结果已过期，请重新识别。")
            return
        if isinstance(outcome.payload, OcrFailure):
            self._current = None
            self._idle(f"{outcome.payload.category}：{outcome.payload.message}")
            return
        self._preview = OcrPreview(self.window, snapshot, outcome.payload, self.stats, outcome.stamp)
        self._preview.finished.connect(self._preview_finished)
        self._set_state(_State.REVIEW, "请核对识别截图；未确认不会修改录入。")
        self._preview.open()

    @Slot(int)
    def _preview_finished(self, result_code: int) -> None:
        preview, self._preview = self._preview, None
        try:
            if self._state is _State.CLOSING or preview is None:
                return
            if not self._is_current(preview.stamp):
                self._idle("识别结果已过期，请重新识别。")
            elif result_code != QDialog.DialogCode.Accepted:
                self._idle("识别无法导入，请重新截图。"
                           if preview.result_data.blocking_issues else "已取消，原输入未改变。")
            else:
                self._set_state(_State.APPLYING, "正在替换副词条…")
                try:
                    changed = self._apply(preview.selection())
                except (ValueError, RuntimeError) as exc:
                    self._idle(f"未应用：{exc}")
                else:
                    self._idle("已替换副词条并更新评分。" if changed else "识别结果与当前输入一致，无需替换。")
        finally:
            self._current = None
            if preview is not None:
                preview.deleteLater()

    def begin_close(self) -> bool:
        if self._state is not _State.CLOSING:
            self._set_state(_State.CLOSING, "正在结束识别…")
            self._current = None
            self._snapshot = None
            self._repaint_timer.stop()
            if self._save_timer.isActive():
                self._save_settings()
            if self._picker is not None:
                self._picker.dispose()
                self._picker = None
            self._restore_window()
            if self._preview is not None:
                self._preview.reject()
        if not self._pool.waitForDone(0):
            self.window.statusBar().showMessage("正在结束识别，请稍候…")
            self._close_timer.start()
            return False
        self._task = None
        self._engine = None
        return True

    def _poll_close(self) -> None:
        if self.begin_close():
            self.close_ready.emit()
