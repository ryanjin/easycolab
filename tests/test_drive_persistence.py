import ast
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict
from unittest import mock

import colab_bridge

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY_PATH = os.path.join(REPO_ROOT, "app.py")


def _load_app_drive_helpers(tmp_root: str):
    with open(APP_PY_PATH, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=APP_PY_PATH)

    wanted_funcs = {
        "_is_drive_mounted_live",
        "_refresh_drive_mount_state",
        "_flush_drive_file",
        "_persist_generated_output_to_drive",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            selected.append(node)

    mod = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(mod)

    ns: Dict[str, Any] = {
        "os": os,
        "shutil": shutil,
        "subprocess": subprocess,
        "time": time,
        "Any": Any,
        "Dict": Dict,
        "DRIVE_MOUNTED": False,
        "MODEL_BASE_ROOT": os.path.join(tmp_root, "models"),
        "DEFAULT_OUTPUT_DIR": os.path.join(tmp_root, "outputs"),
    }
    exec(compile(mod, APP_PY_PATH, "exec"), ns)
    return ns


class ColabBridgeDriveMountTest(unittest.TestCase):
    def test_wake_uploads_only_server_sources_and_single_use_management_password(self):
        fake_session = mock.Mock(accelerator="G4", kernel_id="own-kernel", endpoint="owned")
        uploaded = []
        def inspect_upload(source, destination):
            if destination.startswith("content/.easycolab-management-"):
                with open(source, encoding="ascii") as f:
                    self.assertEqual(f.read(), "x" * 30)
            uploaded.append(destination)

        with (mock.patch("colab_bridge._has_valid_token", return_value=True),
              mock.patch("colab_bridge._owned_sessions", return_value={"owned": fake_session}),
              mock.patch("colab_bridge._wait_for_runtime_ready", return_value=True),
              mock.patch("colab_bridge.ContentsClient") as contents,
              mock.patch("colab_bridge.ColabRuntime") as runtime,
              mock.patch.object(colab_bridge.state, "_history", mock.Mock()),
              mock.patch.dict(os.environ, {"EASYCOLAB_ADMIN_PASSWORD": "x" * 30}),
              redirect_stdout(io.StringIO())):
            contents.return_value.upload.side_effect = inspect_upload
            runtime.return_value.execute_code.return_value = []
            colab_bridge.cmd_wake("G4", APP_PY_PATH)
        self.assertEqual(uploaded[:2], ["content/app.py", "content/runtime_auth.py"])
        self.assertEqual(len(uploaded), 3)
        self.assertTrue(uploaded[2].startswith("content/.easycolab-management-"))
        code = runtime.return_value.execute_code.call_args.args[0]
        self.assertNotIn("x" * 30, code)
        self.assertNotIn("refresh_token", code)
        self.assertNotIn('"rclone", "mount"', code)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_credential_writes_replace_existing_permissive_files_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "oauth.json")
            with open(path, "w") as stream:
                stream.write("old")
            os.chmod(path, 0o644)
            colab_bridge._write_private_file(path, "replacement")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path) as stream:
                self.assertEqual(stream.read(), "replacement")


class AppDrivePersistenceHelpersTest(unittest.TestCase):
    def test_is_drive_mounted_live_distinguishes_mount_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = _load_app_drive_helpers(tmp)

            def fake_isdir(p):
                return p == "/content/drive/MyDrive"

            def fake_exists(p):
                return p in ("/content", "/content/drive/MyDrive")

            # Same st_dev -> unmounted local overlay directory -> False
            with (
                mock.patch("os.path.isdir", side_effect=fake_isdir),
                mock.patch("os.path.exists", side_effect=fake_exists),
                mock.patch("os.stat", side_effect=lambda p: types.SimpleNamespace(st_dev=100)),
            ):
                self.assertFalse(ns["_is_drive_mounted_live"]())

            # Different st_dev -> real FUSE DriveFS mount -> True
            def fake_stat_mounted(p):
                return types.SimpleNamespace(st_dev=200 if p == "/content/drive/MyDrive" else 100)

            with (
                mock.patch("os.path.isdir", side_effect=fake_isdir),
                mock.patch("os.path.exists", side_effect=fake_exists),
                mock.patch("os.stat", side_effect=fake_stat_mounted),
            ):
                self.assertTrue(ns["_is_drive_mounted_live"]())

    def test_unmounted_outputs_stay_local_without_external_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = _load_app_drive_helpers(tmp)
            path = os.path.join(tmp, "output.png")
            with open(path, "wb") as stream:
                stream.write(b"image")
            with (mock.patch.dict(ns, {"_is_drive_mounted_live": lambda: False}),
                  mock.patch("subprocess.run") as run):
                self.assertEqual(ns["_persist_generated_output_to_drive"](path), path)
            run.assert_not_called()
            self.assertTrue(os.path.isfile(path))

    def test_refresh_drive_mount_state_upgrades_output_dir_and_flushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = _load_app_drive_helpers(tmp)
            fake_drive_outputs = os.path.join(tmp, "mounted_drive_outputs")
            local_video = os.path.join(tmp, "outputs", "clip.mp4")
            os.makedirs(os.path.dirname(local_video), exist_ok=True)
            with open(local_video, "wb") as wf:
                wf.write(b"MP4DATA" * 128)

            flushed = []
            with (
                mock.patch.dict(ns, {"_is_drive_mounted_live": lambda: True}),
                mock.patch("os.makedirs"),
            ):
                self.assertTrue(ns["_refresh_drive_mount_state"]())
                self.assertEqual(ns["DEFAULT_OUTPUT_DIR"], "/content/drive/MyDrive/EasyColab_Outputs")
                self.assertEqual(ns["MODEL_BASE_ROOT"], "/content/drive/MyDrive/EasyColab_Models")

            drive_file = os.path.join(fake_drive_outputs, "clip.mp4")
            os.makedirs(fake_drive_outputs, exist_ok=True)
            shutil.copy2(local_video, drive_file)
            with mock.patch("os.fsync", side_effect=lambda fd: flushed.append(fd)):
                ns["_flush_drive_file"](drive_file)
            self.assertEqual(len(flushed), 1)


if __name__ == "__main__":
    unittest.main()
