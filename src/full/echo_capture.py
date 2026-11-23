"""单屏截图、逻辑坐标换算与冻结画面框选（仅 GUI 线程访问屏幕）。"""

from __future__ import annotations

import ctypes
import math
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from PySide6.QtCore import QByteArray, QObject, QPoint, QRect, QThread, Qt, Signal
from PySide6.QtGui import (
    QColor, QCloseEvent, QCursor, QGuiApplication, QImage, QKeyEvent, QMouseEvent,
    QPainter, QPaintEvent, QPen, QPixmap, QScreen,
)
from PySide6.QtWidgets import QWidget


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_qrect(cls, rect: QRect) -> Rect:
        return cls(rect.x(), rect.y(), rect.width(), rect.height())

    def qrect(self) -> QRect:
        return QRect(self.x, self.y, self.width, self.height)

    def contains(self, other: Rect) -> bool:
        return (other.width > 0 and other.height > 0 and self.x <= other.x
                and self.y <= other.y and other.x + other.width <= self.x + self.width
                and other.y + other.height <= self.y + self.height)


@dataclass(frozen=True)
class ScreenInfo:
    name: str
    manufacturer: str
    model: str
    serial: str
    geometry: Rect
    scale: float

    @classmethod
    def from_screen(cls, screen: QScreen) -> ScreenInfo:
        return cls(screen.name(), screen.manufacturer(), screen.model(), screen.serialNumber(),
                   Rect.from_qrect(screen.geometry()), screen.devicePixelRatio())

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return self.name, self.manufacturer, self.model, self.serial


@dataclass(frozen=True)
class CaptureRegion:
    screen: ScreenInfo
    rect: Rect  # 屏幕内的半开逻辑坐标，不含桌面原点

    def __post_init__(self) -> None:
        bounds = self.screen.geometry
        if not any(self.screen.identity) or not math.isfinite(self.screen.scale) or self.screen.scale <= 0:
            raise CaptureError("无法保存屏幕标识或缩放信息，请重新框选。")
        if not Rect(0, 0, bounds.width, bounds.height).contains(self.rect):
            raise CaptureError("识别区为空、越界或横跨屏幕。请在同一屏幕内重新框选。")

    @property
    def desktop_rect(self) -> QRect:
        return self.rect.qrect().translated(self.screen.geometry.x, self.screen.geometry.y)


@dataclass(frozen=True)
class CaptureSnapshot:
    rgba: bytes
    width: int
    height: int
    captured_at: float
    region_version: int

    def qimage(self) -> QImage:
        return QImage(self.rgba, self.width, self.height, self.width * 4,
                      QImage.Format.Format_RGBA8888).copy()


def region_from_drag(screen: ScreenInfo, start: QPoint, end: QPoint) -> CaptureRegion:
    bounds = screen.geometry
    screen_rect = bounds.qrect()
    if not screen_rect.contains(start) or not screen_rect.contains(end):
        raise CaptureError("识别区不能横跨屏幕。请在同一屏幕内拖动。")
    return CaptureRegion(screen, Rect(min(start.x(), end.x()) - bounds.x,
                                      min(start.y(), end.y()) - bounds.y,
                                      abs(end.x() - start.x()), abs(end.y() - start.y())))


def screen_index(region: CaptureRegion, screens: Sequence[ScreenInfo]) -> int:
    matches = [index for index, info in enumerate(screens) if info.identity == region.screen.identity]
    if len(matches) != 1:
        raise CaptureError("原识别屏幕已移除或无法唯一定位。请重新框选。")
    current = screens[matches[0]]
    if current.geometry != region.screen.geometry or not math.isclose(
            current.scale, region.screen.scale, rel_tol=0, abs_tol=1e-6):
        raise CaptureError("屏幕位置、尺寸或缩放已改变。请重新框选。")
    return matches[0]


def _check_size(width: int, height: int, scale: float, shot: QPixmap) -> None:
    if shot.isNull():
        raise CaptureError("系统返回空截图。请检查截图权限，并让游戏画面保持可见。")
    # Qt 逻辑几何和裁剪边界可能有一个像素的取整差异。
    if (abs(shot.width() - width * scale) > max(1.0, scale)
            or abs(shot.height() - height * scale) > max(1.0, scale)):
        raise CaptureError("截图像素尺寸与屏幕缩放不符。请检查显示设置并重新框选。")


def pixel_rect(region: CaptureRegion, width: int, height: int) -> QRect:
    bounds, rect = region.screen.geometry, region.rect
    if width <= 0 or height <= 0:
        raise CaptureError("系统返回空截图。")
    # 按实际像素与逻辑尺寸的比例映射两个边界，保留半开矩形的覆盖范围。
    left = rect.x * width // bounds.width
    top = rect.y * height // bounds.height
    right = -(-(rect.x + rect.width) * width // bounds.width)
    bottom = -(-(rect.y + rect.height) * height // bounds.height)
    return QRect(left, top, right - left, bottom - top)


def _require_gui_thread() -> None:
    app = QGuiApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise CaptureError("只能在 GUI 线程截图。")


def _check_permission() -> None:
    if sys.platform != "darwin":
        return
    try:
        graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        preflight = graphics.CGPreflightScreenCaptureAccess
        request = graphics.CGRequestScreenCaptureAccess
    except (OSError, AttributeError) as exc:
        raise CaptureError("无法检查 macOS 屏幕录制权限。") from exc
    for function in (preflight, request):
        function.argtypes = []
        function.restype = ctypes.c_bool
    if not preflight() and not request():
        raise CaptureError("未获得屏幕录制权限。请在 macOS「隐私与安全性 → 屏幕与系统录音」授权后重试；必要时重启应用。")


def resolve_screen(region: CaptureRegion) -> QScreen:
    _require_gui_thread()
    screens = QGuiApplication.screens()
    return screens[screen_index(region, [ScreenInfo.from_screen(screen) for screen in screens])]


def _full_screen(screen: QScreen, info: ScreenInfo) -> QPixmap:
    frame = screen.grabWindow(0)
    _check_size(info.geometry.width, info.geometry.height, info.scale, frame)
    return frame


def capture_region(region: CaptureRegion, region_version: int = 0) -> CaptureSnapshot:
    screen = resolve_screen(region)
    _check_permission()
    rect = region.rect
    if sys.platform == "win32":
        shot = screen.grabWindow(0, rect.x, rect.y, rect.width, rect.height)
        _check_size(rect.width, rect.height, region.screen.scale, shot)
    else:
        # Cocoa 与 Windows 的局部 grabWindow 原点不同；只保留按实际像素裁得的选区。
        frame = _full_screen(screen, region.screen)
        shot = frame.copy(pixel_rect(region, frame.width(), frame.height()))
        del frame
    if shot.isNull():
        raise CaptureError("无法裁出识别区。请重新框选。")
    image = shot.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    if image.isNull():
        raise CaptureError("无法读取截图像素，请重试。")
    rgba = bytes(image.constBits())
    pixels = image.width() * image.height()
    if rgba.count(b"\0\0\0\xff") == pixels or rgba.count(b"\0") == len(rgba):
        raise CaptureError("识别区画面全黑或不可读。请在游戏窗口可见时重试，避免独占全屏。")
    return CaptureSnapshot(rgba, image.width(), image.height(), time.time(), region_version)


@dataclass(frozen=True)
class WindowAppearance:
    geometry: QByteArray
    state: Qt.WindowState
    on_top: bool
    visible: bool

    @classmethod
    def from_window(cls, window: QWidget) -> WindowAppearance:
        return cls(window.saveGeometry(), window.windowState(),
                   bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), window.isVisible())

    def restore(self, window: QWidget, *, on_top: bool | None = None) -> None:
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.on_top if on_top is None else on_top)
        window.restoreGeometry(self.geometry)
        window.setWindowState(self.state)
        window.setVisible(self.visible)


class _ScreenOverlay(QWidget):
    finished = Signal(object, str)

    def __init__(self, screen: QScreen, info: ScreenInfo, frozen: QPixmap) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self._info = info
        self._frozen = frozen
        self._start: QPoint | None = None
        self._end: QPoint | None = None
        self._crossed = False
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.winId()
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.setGeometry(info.geometry.qrect())

    def paintEvent(self, _event: QPaintEvent) -> None:
        with QPainter(self) as painter:
            painter.drawPixmap(self.rect(), self._frozen, self._frozen.rect())
            painter.fillRect(self.rect(), QColor(0, 0, 0, 120))
            if self._start is not None and self._end is not None:
                origin = QPoint(self._info.geometry.x, self._info.geometry.y)
                area = QRect(self._start - origin, self._end - origin).normalized()
                painter.save()
                painter.setClipRect(area)
                painter.drawPixmap(self.rect(), self._frozen, self._frozen.rect())
                painter.restore()
                painter.setPen(QPen(QColor("#60a5fa"), 2))
                painter.drawRect(area)
            hint = self.rect().adjusted(12, 12, -12, 0)
            hint.setHeight(48)
            painter.fillRect(hint, QColor(15, 23, 42, 225))
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(hint, Qt.AlignmentFlag.AlignCenter,
                             "拖动框选完整副词条列表（含未解锁槽位），松开保存 · Esc 取消")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = self._end = event.globalPosition().toPoint()
            self._crossed = False
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._end = event.globalPosition().toPoint()
            self._crossed |= not self._info.geometry.qrect().contains(self._end)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        try:
            end = event.globalPosition().toPoint()
            if self._crossed:
                raise CaptureError("识别区不能横跨屏幕。请在同一屏幕内拖动。")
            region = region_from_drag(self._info, self._start, end)
        except CaptureError as exc:
            self.finished.emit(None, str(exc))
        else:
            self.finished.emit(region, "")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.finished.emit(None, "")
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.finished.emit(None, "")
        super().closeEvent(event)


class RegionPicker(QObject):
    finished = Signal(object, str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._overlays: list[_ScreenOverlay] = []

    def start(self) -> None:
        _require_gui_thread()
        _check_permission()
        screens = QGuiApplication.screens()
        if not screens:
            raise CaptureError("未发现可截图的屏幕。")
        # 必须在显示任何遮罩前冻结所有候选屏幕，避免遮罩进入自身截图。
        for screen in screens:
            info = ScreenInfo.from_screen(screen)
            overlay = _ScreenOverlay(screen, info, _full_screen(screen, info))
            overlay.finished.connect(self.finished)
            self._overlays.append(overlay)
        for overlay in self._overlays:
            overlay.show()
        active = next((overlay for overlay in self._overlays if
                       overlay.geometry().contains(QCursor.pos())), self._overlays[0])
        active.raise_()
        active.activateWindow()
        active.setFocus()

    def dispose(self) -> None:
        for overlay in self._overlays:
            overlay.hide()
            overlay.deleteLater()
        self._overlays.clear()
        self.deleteLater()
