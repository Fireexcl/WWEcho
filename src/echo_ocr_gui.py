"""截图识别的设置、任务生命周期与校正确认界面。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from PySide6.QtCore import QIODevice, QObject, QRunnable, QSaveFile, QSignalBlocker, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QScrollArea, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from src.echo_capture import (
    CaptureError, CaptureRegion, CaptureSnapshot, Rect, RegionPicker, ScreenInfo,
    WindowAppearance, capture_region, resolve_screen,
)
from src.echo_ocr import (
    OCR_MAX_STATS, LocalOcrEngine, OcrError, OcrResult, OcrSelection, OcrToken, SubStatEntry,
    parse_sub_stats, validate_selection,
)


class WheelSafeComboBox(QComboBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


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


class OcrPreview(QWidget):
    finished = Signal(bool)

    def __init__(self, parent: QWidget, snapshot: CaptureSnapshot,
                 result: OcrResult, stats: dict[str, Any], stamp: RequestStamp) -> None:
        super().__init__(parent)
        self._stats = stats
        self.result_data = result
        self.stamp = stamp
        self._invalidated = False
        self._finished = False
        self._applied_selection: OcrSelection | None = None
        layout = QVBoxLayout(self)
        choices = QGridLayout()
        self.cost_choice = WheelSafeComboBox()
        self.cost_choice.addItem("请选择 Cost", None)
        for cost in stats["main_stats"]["max_values_by_cost"]:
            self.cost_choice.addItem(f"Cost {cost}", int(cost))
        if result.cost is not None:
            self.cost_choice.setCurrentIndex(self.cost_choice.findData(result.cost))
        self.main_choice = WheelSafeComboBox()
        self.main_choice.addItem("请选择主词条", None)
        main_ids = dict.fromkeys(
            stat_id for pool in stats["main_stats"]["max_values_by_cost"].values() for stat_id in pool)
        for stat_id in main_ids:
            self.main_choice.addItem(stats["stat_definitions"][stat_id]["name_zh"], stat_id)
        if result.main_stat is not None and result.main_stat.stat_id is not None:
            self.main_choice.setCurrentIndex(self.main_choice.findData(result.main_stat.stat_id))
        self.minor_choice = WheelSafeComboBox()
        self.minor_choice.addItem("请选择固定小词条", None)
        minor_ids = dict.fromkeys(
            stat_id for pool in stats["minor_stats"]["max_values_by_cost"].values() for stat_id in pool)
        for stat_id in minor_ids:
            self.minor_choice.addItem(stats["stat_definitions"][stat_id]["name_zh"], stat_id)
        if result.minor_stat is not None and result.minor_stat.stat_id is not None:
            self.minor_choice.setCurrentIndex(self.minor_choice.findData(result.minor_stat.stat_id))
        for column, (name, widget) in enumerate((
                ("Cost", self.cost_choice), ("主词条", self.main_choice),
                ("固定小词条", self.minor_choice))):
            choices.addWidget(QLabel(name), 0, column)
            choices.addWidget(widget, 1, column)
        layout.addLayout(choices)
        evidence = QHBoxLayout()
        picture = QLabel()
        image = QPixmap.fromImage(snapshot.qimage())
        picture.setPixmap(image.scaled(
            max(1, round(image.width() * 0.5)), max(1, round(image.height() * 0.5)),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
        picture.adjustSize()
        scroll = QScrollArea()
        scroll.setWidget(picture)
        scroll.setMinimumHeight(80)
        evidence.addWidget(scroll, 2)
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlainText("\n".join(result.raw_lines) or "（未识别到文字）")
        evidence.addWidget(self.raw, 3)
        layout.addLayout(evidence, 1)
        self._structural_issues = tuple(issue for issue in result.blocking_issues
                                        if issue not in result.header_issues)
        grid = QGridLayout()
        layout.addLayout(grid)
        self.rows: list[tuple[QLabel, QComboBox, QComboBox]] = []
        if not self._structural_issues:
            for index, row in enumerate(result.rows):
                if row.is_unopened:
                    label = QLabel("未开孔")
                    label.setToolTip(row.raw_text)
                    grid.addWidget(label, index, 0, 1, 3)
                    continue
                editor_index = len(self.rows)
                label = QLabel(" · ".join((row.raw_text, *row.issues)))
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.PlainText)
                stat = WheelSafeComboBox()
                stat.addItem("请选择副词条", None)
                for stat_id in stats["sub_stats"]["tiers"]:
                    stat.addItem(stats["stat_definitions"][stat_id]["name_zh"], stat_id)
                tier = WheelSafeComboBox()
                self.rows.append((label, stat, tier))
                for col, widget in enumerate(self.rows[-1]):
                    grid.addWidget(widget, index, col)
                if row is not None and row.stat_id is not None:
                    stat.setCurrentIndex(stat.findData(row.stat_id))
                self._refresh_tiers(editor_index)
                if row is not None and row.value is not None:
                    tier.setCurrentIndex(tier.findData(row.value))
                stat.currentIndexChanged.connect(lambda *_, i=editor_index: self._change_stat(i))
                tier.currentIndexChanged.connect(self._update_confirm)
        elif result.rows:
            self.raw.appendPlainText("\n解析行（不可应用）：\n" + "\n".join(
                " · ".join(("未开孔", row.raw_text)) if row.is_unopened else
                " · ".join((row.raw_text, *row.issues)) for row in result.rows))

        self.validation = QLabel()
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.select_button = QPushButton("重新设置识别区域")
        self.recognize_button = QPushButton("截图识别")
        for button in (self.select_button, self.recognize_button):
            buttons.addWidget(button)
        self.cancel_button = QPushButton("取消")
        self.confirm = QPushButton("确认并回填")
        self.confirm.setDefault(True)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm)
        layout.addLayout(buttons)
        self.confirm.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.cancel)
        self.cost_choice.currentIndexChanged.connect(self._change_cost)
        self.main_choice.currentIndexChanged.connect(self._change_header_stat)
        self.minor_choice.currentIndexChanged.connect(self._change_header_stat)
        for label in self.findChildren(QLabel):
            if label is not picture:
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                              | Qt.TextInteractionFlag.TextSelectableByKeyboard)
                label.setCursor(Qt.CursorShape.IBeamCursor)
        self._update_confirm()

    def _change_cost(self) -> None:
        cost = self.cost_choice.currentData()
        if cost is not None:
            pool = self._stats["minor_stats"]["max_values_by_cost"][str(cost)]
            if self.minor_choice.currentData() not in pool:
                self.minor_choice.setCurrentIndex(self.minor_choice.findData(next(iter(pool))))
        self._update_confirm()

    def _change_header_stat(self) -> None:
        main, minor = self.main_choice.currentData(), self.minor_choice.currentData()
        if self.cost_choice.currentData() is None and main is not None and minor is not None:
            main_pools = self._stats["main_stats"]["max_values_by_cost"]
            minor_pools = self._stats["minor_stats"]["max_values_by_cost"]
            candidates = [int(cost) for cost, pool in main_pools.items()
                          if main in pool and minor in minor_pools[cost]]
            if len(candidates) == 1:
                self.cost_choice.setCurrentIndex(self.cost_choice.findData(candidates[0]))
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

    def selection(self) -> OcrSelection:
        if self._invalidated:
            raise ValueError("输入或识别区已变更，请取消后重新识别。")
        if self._structural_issues:
            raise ValueError("请重新截图或调整识别区域。")
        cost = self.cost_choice.currentData()
        main = self.main_choice.currentData()
        minor = self.minor_choice.currentData()
        if cost is None or main is None or minor is None:
            raise ValueError("请选择未确定的 Cost、主词条和固定小词条。")
        if main not in self._stats["main_stats"]["max_values_by_cost"][str(cost)] \
                or minor not in self._stats["minor_stats"]["max_values_by_cost"][str(cost)]:
            raise ValueError("主词条或固定小词条与 Cost 不匹配，请重新选择。")
        if not 1 <= len(self.rows) <= OCR_MAX_STATS:
            raise ValueError(f"OCR 必须识别到 1～{OCR_MAX_STATS} 条副词条。")
        entries = []
        for index, (_, stat, tier) in enumerate(self.rows, 1):
            stat_id, value = stat.currentData(), tier.currentData()
            if stat_id is None or value is None:
                raise ValueError(f"请校正第 {index} 条的词条和档位。")
            entries.append(SubStatEntry(stat_id, value))
        entries = validate_selection(entries, self._stats)
        return OcrSelection(cost, main, minor, entries)

    def can_auto_apply(self) -> bool:
        result = self.result_data
        if result.blocking_issues or result.unmatched_text or result.cost is None:
            return False
        if any(item is None or item.stat_id is None or item.value is None
               for item in (result.main_stat, result.minor_stat)):
            return False
        return self.confirm.isEnabled()

    @property
    def automatically_applied(self) -> bool:
        return self._applied_selection is not None

    def mark_applied(self) -> None:
        self._applied_selection = self.selection()
        self._update_confirm()

    def _update_confirm(self) -> None:
        try:
            selection = self.selection()
        except ValueError as exc:
            self.validation.setText(str(exc))
            self.confirm.setEnabled(False)
        else:
            self.validation.clear()
            self.confirm.setEnabled(selection != self._applied_selection)

    def invalidate(self) -> None:
        self._invalidated = True
        self._update_confirm()

    def accept(self) -> None:
        self._update_confirm()
        if self.confirm.isEnabled():
            self._finish(True)

    def cancel(self) -> None:
        self._finish(False)

    def _finish(self, accepted: bool) -> None:
        if not self._finished:
            self._finished = True
            self.finished.emit(accepted)


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
    preview_ready = Signal()

    def __init__(self, window: QMainWindow, stats: dict[str, Any], settings_path: Path,
                 apply_selection: Callable[[OcrSelection], bool]) -> None:
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
        self.pages = QStackedWidget(self.panel)
        layout.addWidget(self.pages)
        self.controls_page = QWidget()
        controls_layout = QVBoxLayout(self.controls_page)
        actions = QHBoxLayout()
        self.select_button = QPushButton("设置识别区域")
        self.recognize_button = QPushButton("截图识别")
        actions.addWidget(self.select_button)
        actions.addWidget(self.recognize_button)
        actions.addStretch()
        controls_layout.addLayout(actions)
        self.appearance_controls = QWidget(window)
        appearance = QHBoxLayout(self.appearance_controls)
        appearance.setContentsMargins(0, 0, 0, 0)
        appearance.setSpacing(8)
        self.opacity_text = QLabel()
        self.opacity_text.setObjectName("muted")
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(50, 100)
        self.opacity.setFixedWidth(96)
        self.opacity.setAccessibleName("窗口透明度")
        self.opacity.setValue(self.settings.opacity)
        self.on_top = QPushButton("置顶")
        self.on_top.setObjectName("onTop")
        self.on_top.setCheckable(True)
        self.on_top.setChecked(self.settings.on_top)
        appearance.addWidget(self.opacity_text)
        appearance.addWidget(self.opacity)
        appearance.addWidget(self.on_top)
        self.status = QLabel()
        self.settings_status = QLabel()
        for label in (self.status, self.settings_status):
            label.setObjectName("muted")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            controls_layout.addWidget(label)
        self.settings_status.setText(self.store.read_error)
        self.settings_status.setVisible(bool(self.store.read_error))
        self.pages.addWidget(self.controls_page)
        self._show_opacity()
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.settings.on_top)
        self._idle("已加载识别区。截图后可在识别标签页校正并回填。" if self.settings.region else
                   "尚未设置识别区。请框选主词条、固定小词条和完整副词条列表。")
        self.select_button.clicked.connect(self.select_region)
        self.recognize_button.clicked.connect(self.recognize)
        self.opacity.valueChanged.connect(self._change_opacity)
        self.on_top.toggled.connect(self._change_on_top)

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

    def _set_state(self, state: _State, message: str) -> None:
        self._state = state
        idle = state in (_State.UNCONFIGURED, _State.READY)
        self.select_button.setEnabled(idle or state in (_State.RECOGNIZING, _State.REVIEW))
        self.recognize_button.setEnabled(state in (_State.READY, _State.REVIEW) and self._task is None)
        if self._preview is not None:
            self._preview.select_button.setEnabled(self.select_button.isEnabled())
            self._preview.recognize_button.setEnabled(self.recognize_button.isEnabled())
        for control in (self.opacity, self.on_top):
            control.setEnabled(idle or state in (_State.RECOGNIZING, _State.REVIEW))
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
        if self._preview is not None:
            self._preview.cancel()
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
        if self._preview is not None and self._state is not _State.APPLYING:
            self._preview.invalidate()

    def recognize(self) -> None:
        region = self.settings.region
        if region is None:
            if self._state is _State.UNCONFIGURED:
                self.status.setText("请先设置识别区域。")
            return
        if self._state not in (_State.READY, _State.REVIEW) or self._task is not None:
            return
        if self._preview is not None:
            self._preview.cancel()
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
        self._preview = OcrPreview(self.pages, snapshot, outcome.payload, self.stats, outcome.stamp)
        preview = self._preview
        preview.finished.connect(self._preview_finished)
        preview.select_button.clicked.connect(self.select_region)
        preview.recognize_button.clicked.connect(self.recognize)
        self.pages.addWidget(preview)
        self.pages.setCurrentWidget(preview)
        self._set_state(_State.REVIEW, "识别完成；请在当前标签页查看或校正结果。")
        if preview.can_auto_apply():
            self._set_state(_State.APPLYING, "正在自动回填词条并更新评分…")
            try:
                changed = self._apply(preview.selection())
            except (ValueError, RuntimeError) as exc:
                preview.validation.setText(f"自动回填失败：{exc}。可校正后重试，或重新截图。")
                self._set_state(_State.REVIEW, f"自动回填失败：{exc}")
            else:
                if self._state is _State.CLOSING or self._preview is not preview:
                    return
                preview.stamp = replace(preview.stamp, input_version=self.input_version)
                self._current = preview.stamp
                preview.mark_applied()
                self._set_state(_State.REVIEW, "已自动回填词条并更新评分，可继续查看或校正。" if changed
                                else "识别结果与当前输入一致，无需重新计算，可继续查看或校正。")
        self.preview_ready.emit()

    @Slot(bool)
    def _preview_finished(self, accepted: bool) -> None:
        preview, self._preview = self._preview, None
        try:
            if self._state is _State.CLOSING or preview is None:
                return
            if not self._is_current(preview.stamp):
                self._idle("识别结果已过期，请重新识别。")
            elif not accepted:
                self._idle("已关闭识别结果，保留已回填词条；未确认的修改未应用。"
                           if preview.automatically_applied else "已取消，原输入未改变。")
            else:
                self._set_state(_State.APPLYING, "正在回填词条并更新评分…")
                try:
                    changed = self._apply(preview.selection())
                except (ValueError, RuntimeError) as exc:
                    self._idle(f"未应用：{exc}")
                else:
                    self._idle("已回填词条并更新评分。" if changed
                               else "识别结果与当前输入一致，无需重新计算。")
        finally:
            self._current = None
            if preview is not None:
                self.pages.setCurrentWidget(self.controls_page)
                self.pages.removeWidget(preview)
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
                self._preview.cancel()
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
