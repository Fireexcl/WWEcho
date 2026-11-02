"""方案库持久化、名称校验、旧格式兼容和写入失败保护。"""

import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QLockFile

from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, load_json
from src.echo_weight_presets import DEFAULT_PRESET_NAME, WeightPresetStore, normalize_weights


class WeightPresetPathTests(unittest.TestCase):
    def test_source_path_uses_project_root_not_python_or_working_directory(self):
        from src import echo_gui

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "源码 项目"
            with patch.object(sys, "frozen", False, create=True), \
                    patch.object(sys, "executable", str(root / ".venv" / "bin" / "python")), \
                    patch.object(echo_gui, "__file__", str(root / "src" / "echo_gui.py")), \
                    patch("os.getcwd", return_value=str(root.parent / "启动目录")), \
                    patch.object(echo_gui.QStandardPaths, "writableLocation") as user_location:
                self.assertEqual(echo_gui.weight_presets_path(), root / "weight-presets.json")
            user_location.assert_not_called()
            self.assertFalse(root.exists())

    def test_frozen_path_uses_executable_directory_not_unpack_or_working_directory(self):
        from src import echo_gui

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "EXE 程序"
            unpack = root.parent / "_MEI12345"
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(root / "WWEcho声骸工作台.exe")), \
                    patch.object(sys, "_MEIPASS", str(unpack), create=True), \
                    patch.object(echo_gui, "__file__", str(unpack / "src" / "echo_gui.py")), \
                    patch("os.getcwd", return_value=str(root.parent / "启动目录")), \
                    patch.object(echo_gui.QStandardPaths, "writableLocation") as user_location:
                self.assertEqual(echo_gui.weight_presets_path(), root / "weight-presets.json")
            user_location.assert_not_called()
            self.assertFalse(root.exists())
            self.assertFalse(unpack.exists())


class WeightPresetStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "program" / "weight-presets.json"
        self.stats = load_json(DEFAULT_STATS)
        self.defaults = load_json(DEFAULT_WEIGHTS)
        self.original_defaults = DEFAULT_WEIGHTS.read_bytes()
        self.store = WeightPresetStore(self.path, self.stats, self.defaults)

    def tearDown(self):
        self.assertEqual(DEFAULT_WEIGHTS.read_bytes(), self.original_defaults)

    def reopen(self):
        return WeightPresetStore(self.path, self.stats, self.defaults)

    def test_seed_exact_default_values_and_name(self):
        self.assertEqual(len(self.store.presets), 1)
        self.assertEqual(self.store.current["name"], DEFAULT_PRESET_NAME)
        for category in ("main_stats", "minor_stats", "sub_stats"):
            self.assertEqual(self.store.current["weights"][category], self.defaults[category])
        self.assertEqual(self.reopen().current, self.store.current)

    def test_migration_preserves_all_presets_and_selection_without_writing_legacy(self):
        legacy = self.path
        source = self.store
        weights = copy.deepcopy(source.current["weights"])
        weights["sub_stats"]["crit_rate"] = 7
        source.add("已有方案", weights)
        original = legacy.read_bytes()
        destination = self.path.parent.parent / "portable" / "weight-presets.json"
        with patch("src.echo_weight_presets.QLockFile", wraps=QLockFile) as lock:
            migrated = WeightPresetStore(destination, self.stats, self.defaults, legacy_path=legacy)
        lock.assert_called_once_with(str(destination) + ".lock")
        self.assertEqual(migrated.migrated_from, legacy)
        self.assertTrue(migrated.persisted)
        self.assertEqual(migrated.presets, source.presets)
        self.assertEqual(migrated.selected_id, source.selected_id)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(list(legacy.parent.iterdir()), [legacy])
        self.assertEqual(load_json(destination), load_json(legacy))

    def test_existing_local_library_is_never_overwritten_or_merged_from_legacy(self):
        legacy = self.path.with_name("legacy.json")
        legacy.write_text("invalid legacy JSON", encoding="utf-8")
        original = self.path.read_bytes()
        store = WeightPresetStore(self.path, self.stats, self.defaults, legacy_path=legacy)
        self.assertIsNone(store.migrated_from)
        self.assertEqual(store.presets, self.store.presets)
        self.assertEqual(self.path.read_bytes(), original)

    def test_migration_is_not_repeated_after_local_library_exists(self):
        destination = self.path.with_name("portable.json")
        store = WeightPresetStore(destination, self.stats, self.defaults, legacy_path=self.path)
        original = store.presets
        self.store.add("旧目录后来新增", self.store.current["weights"])
        reopened = WeightPresetStore(destination, self.stats, self.defaults, legacy_path=self.path)
        self.assertEqual(reopened.presets, original)
        self.assertIsNone(reopened.migrated_from)

    def test_missing_legacy_directory_is_not_created(self):
        destination = self.path.with_name("portable.json")
        legacy = self.path.parent / "never-created-user-dir" / "weight-presets.json"
        store = WeightPresetStore(destination, self.stats, self.defaults, legacy_path=legacy)
        self.assertIsNone(store.migrated_from)
        self.assertTrue(destination.is_file())
        self.assertFalse(legacy.parent.exists())

    def test_migration_failure_keeps_legacy_values_and_does_not_fall_back_to_user_directory(self):
        self.store.add("已有方案", self.store.current["weights"])
        original = self.path.read_bytes()
        destination = self.path.parent.parent / "read-only-program" / "weight-presets.json"
        with patch("src.echo_weight_presets.os.replace", side_effect=PermissionError("read-only directory")), \
                patch("src.echo_weight_presets.QLockFile", wraps=QLockFile) as lock:
            store = WeightPresetStore(destination, self.stats, self.defaults, legacy_path=self.path)
        lock.assert_called_once_with(str(destination) + ".lock")
        self.assertFalse(store.persisted)
        self.assertFalse(destination.exists())
        self.assertEqual(store.presets, self.store.presets)
        self.assertEqual(store.selected_id, self.store.selected_id)
        self.assertEqual(self.path.read_bytes(), original)
        store.save_current(store.current["weights"])
        self.assertTrue(destination.is_file())
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_legacy_does_not_create_new_defaults_or_overwrite_source(self):
        legacy = self.path.with_name("legacy.json")
        legacy.write_text("invalid legacy JSON", encoding="utf-8")
        destination = self.path.with_name("portable.json")
        with self.assertRaises(ValueError):
            WeightPresetStore(destination, self.stats, self.defaults, legacy_path=legacy)
        self.assertFalse(destination.exists())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "invalid legacy JSON")

    def test_save_rename_add_select_delete_and_reopen(self):
        original = self.store.current
        weights = copy.deepcopy(original["weights"])
        weights["sub_stats"]["crit_rate"] = 3.1256
        self.store.save_current(weights)
        self.assertEqual(self.reopen().current["weights"], weights)
        self.store.add("  队伍甲 / 输出  ", original["weights"])
        new_id = self.store.selected_id
        self.assertNotEqual(new_id, original["id"])
        self.assertEqual(self.reopen().selected_id, new_id)
        self.assertEqual(self.store.current["name"], "队伍甲 / 输出")
        self.store.rename_current("角色乙")
        self.assertEqual(self.store.selected_id, new_id)
        self.assertEqual(self.reopen().current["name"], "角色乙")
        self.store.select(original["id"])
        self.assertEqual(self.reopen().current["weights"], weights)
        self.store.delete_current()
        self.assertEqual(self.store.selected_id, new_id)
        self.assertEqual(self.reopen().current["name"], "角色乙")

    def test_default_name_is_editable_like_other_presets(self):
        self.store.rename_current("我的默认")
        self.assertEqual(self.reopen().current["name"], "我的默认")

    def test_reject_empty_duplicate_and_invalid_names_without_changing_file(self):
        self.store.add("Team", self.store.current["weights"])
        before = self.path.read_bytes()
        for name in ("", "  ", "x" * 81, "a\nb", "a\tb", "team", DEFAULT_PRESET_NAME):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.add(name, self.store.current["weights"])
            self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.store.rename_current(DEFAULT_PRESET_NAME)
        self.store.rename_current("TEAM")
        self.assertEqual(self.store.current["name"], "TEAM")

    def test_delete_last_and_select_unknown_are_rejected(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.delete_current()
        with self.assertRaises(ValueError):
            self.store.select("missing")
        self.assertEqual(self.path.read_bytes(), before)

    def test_returned_values_do_not_mutate_store(self):
        original = self.store.current
        self.store.current["weights"]["sub_stats"]["crit_rate"] = 999
        self.store.presets[0]["name"] = "mutated"
        self.assertEqual(self.store.current, original)

    def test_other_instance_change_is_not_silently_overwritten(self):
        second = self.reopen()
        self.store.add("另一个窗口新建", self.store.current["weights"])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "其他窗口修改"):
            second.rename_current("旧快照")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.reopen().presets), 2)

    def test_busy_store_lock_prevents_writes(self):
        lock = QLockFile(str(self.path) + ".lock")
        self.assertTrue(lock.tryLock(0))
        before = self.path.read_bytes()
        try:
            with self.assertRaisesRegex(OSError, "其他窗口保存"):
                self.store.save_current(self.store.current["weights"])
        finally:
            lock.unlock()
        self.assertEqual(self.path.read_bytes(), before)
        self.store.save_current(self.store.current["weights"])

    def test_first_write_failure_can_be_retried_without_losing_defaults(self):
        path = self.path.with_name("first-run.json")
        with patch("src.echo_weight_presets.os.replace", side_effect=OSError("disk full")):
            store = WeightPresetStore(path, self.stats, self.defaults)
        self.assertFalse(store.persisted)
        self.assertIn("disk full", store.initial_save_error)
        self.assertEqual(store.current["name"], DEFAULT_PRESET_NAME)
        store.save_current(store.current["weights"])
        self.assertTrue(store.persisted)
        self.assertEqual(store.initial_save_error, "")

    def test_legacy_flat_main_and_missing_minor_are_normalized(self):
        weights = copy.deepcopy(self.defaults)
        weights["main_stats"] = {name: value for pool in weights["main_stats"].values()
                                 for name, value in pool.items()}
        del weights["minor_stats"]
        self.store.add("旧版", weights)
        saved = self.reopen().current["weights"]
        for pool in saved["main_stats"].values():
            for name, value in pool.items():
                self.assertEqual(value, weights["main_stats"][name])
        self.assertTrue(all(value == 0 for pool in saved["minor_stats"].values()
                            for value in pool.values()))

    def test_invalid_weights_are_transactional(self):
        before = self.path.read_bytes()
        for value in (-1, float("nan"), float("inf"), 1_000_001, .123456, True):
            weights = copy.deepcopy(self.defaults)
            weights["sub_stats"]["crit_rate"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.save_current(weights)
            self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            normalize_weights(self.stats, None)

    def test_normalization_matches_spinbox_precision(self):
        weights = copy.deepcopy(self.defaults)
        weights["sub_stats"]["crit_rate"] = .1234000001
        self.assertEqual(normalize_weights(self.stats, weights)["sub_stats"]["crit_rate"], .1234)

    def test_failed_replace_preserves_disk_and_memory_and_cleans_temporary_file(self):
        original = self.store.current
        weights = copy.deepcopy(original["weights"])
        weights["sub_stats"]["crit_rate"] += 1
        self.store.add("另一个", original["weights"])
        before_disk, before_presets = self.path.read_bytes(), self.store.presets
        before_id = self.store.selected_id
        operations = [lambda: self.store.save_current(weights),
                      lambda: self.store.add("新方案", weights),
                      lambda: self.store.rename_current("新名字"),
                      lambda: self.store.select(original["id"]),
                      self.store.delete_current]
        with patch("src.echo_weight_presets.os.replace", side_effect=OSError("disk full")):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(OSError):
                    operation()
                self.assertEqual(self.path.read_bytes(), before_disk)
                self.assertEqual(self.store.presets, before_presets)
                self.assertEqual(self.store.selected_id, before_id)
                self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_store_is_reported_without_overwriting_user_data(self):
        document = load_json(self.path)
        invalid = ["not-json", json.dumps({**document, "presets": []}),
                   json.dumps({**document, "selected_id": "missing"}),
                   json.dumps({**document, "presets": document["presets"] * 2}),
                   json.dumps({**document, "schema_version": 99})]
        for contents in invalid:
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.reopen()
                self.assertEqual(self.path.read_text(encoding="utf-8"), contents)


if __name__ == "__main__":
    unittest.main()