"""离屏验证屏幕标识、坐标换算与截图失败。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.echo_capture import (
    CaptureError, CaptureRegion, Rect, RegionPicker, ScreenInfo, _check_permission,
    capture_region, pixel_rect, region_from_drag, screen_index,
)


class EchoCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.left = ScreenInfo("left", "maker", "model", "left-serial", Rect(-200, -40, 200, 80), 1.5)
        self.right = ScreenInfo("right", "", "", "", Rect(0, 0, 160, 90), 2)
        self.region = CaptureRegion(self.left, Rect(12, 4, 21, 17))

    def test_normalizes_drag_with_negative_desktop_coordinates(self):
        result = region_from_drag(self.left, QPoint(-167, -19), QPoint(-188, -36))
        self.assertEqual(result, self.region)
        self.assertEqual(result.desktop_rect, QRect(-188, -36, 21, 17))
        self.assertEqual(screen_index(result, [self.right, self.left]), 1)

    def test_rejects_empty_or_cross_screen_rect(self):
        for end in (QPoint(2, 0), QPoint(-201, 0), QPoint(-30, 0), QPoint(-40, 40)):
            with self.subTest(end=end), self.assertRaises(CaptureError):
                region_from_drag(self.left, QPoint(-30, 0), end)

    def test_requires_matching_identity_geometry_and_scale(self):
        for displays in ([self.right], [self.left, self.left],
                         [self.right, replace(self.left, scale=1)],
                         [self.right, replace(self.left, geometry=Rect(-200, 0, 200, 80))]):
            with self.subTest(displays=displays), self.assertRaisesRegex(CaptureError, "重新框选"):
                screen_index(self.region, displays)

    def test_converts_using_actual_pixels_without_global_offset(self):
        region = CaptureRegion(self.left, Rect(191, 71, 9, 9))
        self.assertEqual(pixel_rect(region, 200, 80), QRect(191, 71, 9, 9))
        self.assertEqual(pixel_rect(region, 299, 121), QRect(285, 107, 14, 14))
        self.assertEqual(pixel_rect(region, 400, 160), QRect(382, 142, 18, 18))

    def test_cocoa_crops_and_copies_independent_rgba(self):
        frame = QPixmap(300, 120)
        frame.fill(QColor("#812345"))
        frame.setDevicePixelRatio(1.5)
        screen = Mock()
        screen.grabWindow.return_value = frame
        with patch("src.echo_capture.resolve_screen", return_value=screen), \
                patch("src.echo_capture._check_permission"), \
                patch("src.echo_capture.sys", SimpleNamespace(platform="darwin")):
            snapshot = capture_region(self.region, 12)
        screen.grabWindow.assert_called_once_with(0)
        self.assertEqual((snapshot.width, snapshot.height, snapshot.region_version), (32, 26, 12))
        frame.fill(QColor("black"))
        self.assertEqual(snapshot.qimage().pixelColor(0, 0), QColor("#812345"))

    def test_windows_grabs_region_in_screen_local_coordinates(self):
        frame = QPixmap(32, 26)
        frame.fill(QColor("blue"))
        screen = Mock()
        screen.grabWindow.return_value = frame
        with patch("src.echo_capture.resolve_screen", return_value=screen), \
                patch("src.echo_capture.sys", SimpleNamespace(platform="win32")):
            snapshot = capture_region(self.region)
        screen.grabWindow.assert_called_once_with(0, 12, 4, 21, 17)
        self.assertEqual((snapshot.width, snapshot.height), (32, 26))

    def test_empty_wrong_size_and_black_capture_report_distinct_errors(self):
        black = QPixmap(300, 120)
        black.fill(QColor("black"))
        for frame, error in ((QPixmap(), "空截图"), (QPixmap(400, 100), "尺寸"), (black, "全黑或不可读")):
            with self.subTest(error=error), patch("src.echo_capture._check_permission"), \
                    patch("src.echo_capture.sys", SimpleNamespace(platform="darwin")), \
                    patch("src.echo_capture.resolve_screen") as resolve:
                resolve.return_value.grabWindow.return_value = frame
                with self.assertRaisesRegex(CaptureError, error):
                    capture_region(self.region)

    def test_macos_permission_denial_is_not_reported_as_black(self):
        with patch("src.echo_capture.sys", SimpleNamespace(platform="darwin")), \
                patch("src.echo_capture.ctypes.CDLL") as load:
            graphics = load.return_value
            graphics.CGPreflightScreenCaptureAccess.return_value = False
            graphics.CGRequestScreenCaptureAccess.return_value = False
            with self.assertRaisesRegex(CaptureError, "未获得屏幕录制权限"):
                _check_permission()
            graphics.CGRequestScreenCaptureAccess.assert_called_once_with()

    def test_frozen_overlay_reports_drag_and_escape(self):
        screen = self.app.primaryScreen()
        info = ScreenInfo("test", "", "", "", Rect.from_qrect(screen.geometry()), 1)
        frame = QPixmap(info.geometry.width, info.geometry.height)
        frame.fill(QColor("blue"))
        with patch("src.echo_capture._full_screen", return_value=frame), \
                patch("src.echo_capture.ScreenInfo.from_screen", return_value=info), \
                patch("src.echo_capture._check_permission"):
            for cancel in (False, True):
                picker = RegionPicker(self.app)
                selections = []
                picker.finished.connect(lambda region, error: selections.append((region, error)))
                try:
                    picker.start()
                    self.app.processEvents()
                    overlay = picker._overlays[0]
                    if cancel:
                        QTest.keyClick(overlay, Qt.Key.Key_Escape)
                        self.assertEqual(selections, [(None, "")])
                    else:
                        QTest.mousePress(overlay, Qt.MouseButton.LeftButton, pos=QPoint(120, 115))
                        QTest.mouseMove(overlay, QPoint(10, 20))
                        QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, pos=QPoint(10, 20))
                        self.assertEqual(selections, [(CaptureRegion(info, Rect(10, 20, 110, 95)), "")])
                finally:
                    picker.dispose()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
