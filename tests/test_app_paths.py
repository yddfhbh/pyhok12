import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app_paths


class AppPathsTests(unittest.TestCase):
    def test_get_resource_path_in_dev_mode_uses_repo_directory(self):
        path = app_paths.get_resource_path("tools", "gomen.js")
        self.assertEqual(path.name, "gomen.js")
        self.assertIn("tools", path.parts)

    def test_get_resource_path_in_frozen_mode_uses_meipass(self):
        with mock.patch.object(app_paths.sys, "_MEIPASS", r"C:\bundle", create=True):
            path = app_paths.get_resource_path("runtime", "node.exe")

        self.assertEqual(path, Path(r"C:\bundle") / "runtime" / "node.exe")

    def test_get_user_data_path_uses_localappdata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}, clear=False):
                path = app_paths.get_user_data_path("browser-profile")

        self.assertEqual(path, Path(tmpdir) / "TetrioPcHelper" / "browser-profile")

    def test_resolve_runtime_file_copies_resource_into_user_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "bundle"
            data_root = Path(tmpdir) / "appdata"
            source_root.mkdir(parents=True, exist_ok=True)
            (source_root / "config.json").write_text('{"ok": true}\n', encoding="utf-8")

            with (
                mock.patch.object(app_paths, "get_bundle_base_dir", return_value=source_root),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(data_root)}, clear=False),
            ):
                resolved = Path(app_paths.resolve_runtime_file("config.json"))

            self.assertEqual(resolved, data_root / "TetrioPcHelper" / "config.json")
            self.assertTrue(resolved.exists())
            self.assertEqual(resolved.read_text(encoding="utf-8").strip(), '{"ok": true}')

    def test_resolve_node_executable_prefers_bundled_runtime_node(self):
        bundled = Path(r"C:\bundle\runtime\node.exe")
        with (
            mock.patch.object(app_paths, "get_resource_path", return_value=bundled),
            mock.patch("shutil.which", return_value=r"C:\Program Files\nodejs\node.exe"),
            mock.patch.object(Path, "exists", autospec=True, side_effect=lambda path_obj: path_obj == bundled),
        ):
            resolved = app_paths.resolve_node_executable()

        self.assertEqual(resolved, str(bundled))

    def test_resolve_node_executable_falls_back_to_path(self):
        with (
            mock.patch.object(app_paths, "get_resource_path", return_value=Path(r"C:\missing\runtime\node.exe")),
            mock.patch.object(Path, "exists", autospec=True, return_value=False),
            mock.patch("shutil.which", side_effect=[r"C:\Program Files\nodejs\node.exe", None]),
        ):
            resolved = app_paths.resolve_node_executable()

        self.assertEqual(resolved, r"C:\Program Files\nodejs\node.exe")

    def test_resolve_node_executable_raises_clear_error_when_missing(self):
        with (
            mock.patch.object(app_paths, "get_resource_path", return_value=Path(r"C:\missing\runtime\node.exe")),
            mock.patch.object(Path, "exists", autospec=True, return_value=False),
            mock.patch("shutil.which", return_value=None),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "Node.js 실행 파일을 찾을 수 없습니다."):
                app_paths.resolve_node_executable()


if __name__ == "__main__":
    unittest.main()
