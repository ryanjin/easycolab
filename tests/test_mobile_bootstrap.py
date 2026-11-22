import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mobile_bootstrap


class MobileBootstrapTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.password_file = Path(tmp, ".easycolab-admin-test")
        self.legacy = Path(tmp, ".easycolab_rclone.conf")
        self.config = Path(tmp, "rclone.conf")
        self.mounts = Path(tmp, "mountinfo")
        self.mounts.write_text("")
        self.enterContext(mock.patch.multiple(
            mobile_bootstrap, LEGACY_STAGE=self.legacy, RCLONE_CONFIG=self.config, MOUNTINFO=self.mounts
        ))
        self.run = self.enterContext(mock.patch("mobile_bootstrap.subprocess.run"))
        self.launch = self.enterContext(mock.patch("mobile_bootstrap.subprocess.Popen"))

    def test_rejects_staged_credentials_config_or_active_mount_before_start(self):
        for path, content in (
            (self.legacy, "old credential"),
            (self.config, "[my_other_remote]\nkeep = yes\n[easycolab_gdrive]\n"),
            (self.mounts, "99 1 0:30 / /content/drive/MyDrive rw - fuse.rclone easycolab_gdrive:EasyColab_Drive rw\n"),
        ):
            with self.subTest(path=path.name):
                path.write_text(content)
                self.password_file.write_text("x" * 32)
                with self.assertRaisesRegex(RuntimeError, "旧版 EasyColab"):
                    mobile_bootstrap.start(self.password_file)
                self.assertFalse(self.password_file.exists())
                self.assertEqual(path.read_text(), content)
                self.run.assert_not_called()
                self.launch.assert_not_called()
                path.unlink()

    def test_invalid_secret_is_removed_before_rejection(self):
        self.password_file.write_text("short")
        with self.assertRaises(ValueError):
            mobile_bootstrap.start(self.password_file)
        self.assertFalse(self.password_file.exists())
        self.run.assert_not_called()
        self.launch.assert_not_called()

    def test_unrelated_config_and_native_drive_allow_startup(self):
        other_config = "[work]\ntype = drive\n"
        self.config.write_text(other_config)
        self.mounts.write_text("99 1 0:30 / /content/drive rw - fuse.drivefs drive rw\n")
        self.password_file.write_text("x" * 32)
        self.run.side_effect = subprocess.CalledProcessError(1, "test stop before install")
        with mock.patch("mobile_bootstrap.shutil.which", return_value=None):
            with self.assertRaises(subprocess.CalledProcessError):
                mobile_bootstrap.start(self.password_file)
        self.run.assert_called_once_with(["apt-get", "update", "-qq"], check=True)
        self.assertFalse(self.password_file.exists())
        self.assertEqual(self.config.read_text(), other_config)
        self.launch.assert_not_called()
