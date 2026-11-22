"""Offline checks for credential handling and the service shipped in the notebook."""

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

from runtime_auth import deployment_password


ROOT = Path(__file__).resolve().parents[1]


def load_app_helpers():
    # Importing app.py would install ML dependencies. Use the project's AST fixture pattern.
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    names = {"_is_hf_origin", "_hf_metadata_hosts", "_HFRedirectHandler", "_open_hf_url",
             "_restore_comfyui_manager_security"}
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    nodes.extend(node for node in tree.body if isinstance(node, ast.Assign)
                 and any(getattr(target, "id", None) == "_HF_ORIGIN" for target in node.targets))
    ns = {"urllib": urllib, "Path": Path, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), ns)
    return ns


class BackendSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_app_helpers()

    def test_headless_service_requires_password_before_loading_ml_dependencies(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "app.py")], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, env={**os.environ, "EASYCOLAB_ADMIN_PASSWORD": ""},
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Management password must contain", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_deployment_password_removed_from_environment(self):
        with mock.patch.dict(os.environ, {"EASYCOLAB_ADMIN_PASSWORD": "a" * 32}):
            self.assertEqual(deployment_password(), "a" * 32)
            self.assertNotIn("EASYCOLAB_ADMIN_PASSWORD", os.environ)

    def test_embedded_services_are_written_before_launch(self):
        notebook = json.loads((ROOT / "EasyColab_Server.ipynb").read_text(encoding="utf-8"))
        cells = {c["metadata"].get("id"): "".join(c["source"]) for c in notebook["cells"]}
        for cell, filename in (("runtime_auth", "runtime_auth.py"), ("write_webui", "app.py")):
            self.assertEqual(cells[cell], f"%%writefile {filename}\n" + (ROOT / filename).read_text(encoding="utf-8"))
        self.assertIn("validate_management_password(", cells["launch_webui"])

    def test_authenticated_metadata_uses_only_official_host(self):
        hosts = self.helpers["_hf_metadata_hosts"]
        self.assertEqual(hosts("test"), ("https://huggingface.co",))
        self.assertIn("https://hf-mirror.com", hosts(""))

    def test_bearer_header_is_scoped_to_https_hub_on_initial_and_redirected_requests(self):
        official = "https://huggingface.co/model"
        destinations = {
            official: "Bearer sample", "https://huggingface.co:443/another": "Bearer sample",
            "https://cdn.example/download": None, "http://huggingface.co/model": None,
            "https://huggingface.co:444/model": None,
        }
        with mock.patch("urllib.request.build_opener") as builder:
            open_url = self.helpers["_open_hf_url"]
            open_url(official, "sample", timeout=5)
            original = builder.return_value.open.call_args.args[0]
            handler = builder.call_args.args[0]
            for url, expected in destinations.items():
                with self.subTest(url=url):
                    open_url(url, "sample", timeout=5)
                    request = builder.return_value.open.call_args.args[0]
                    self.assertEqual(request.get_header("Authorization"), expected)
                    redirected = handler.redirect_request(original, None, 302, "Found", {}, url)
                    self.assertEqual(redirected.get_header("Authorization"), expected)

    def test_comfy_manager_restores_legacy_weak_setting_and_keeps_stronger_config(self):
        restore = self.helpers["_restore_comfyui_manager_security"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "user/__manager/config.ini"
            restore(root, root)
            self.assertFalse(config.exists())
            config.parent.mkdir(parents=True)
            for before, after in [("weak", "normal"), ("normal", "normal"), ("strong", "strong")]:
                template = "[default]\nsecurity_level = {}\npreview_method = auto\n"
                config.write_text(template.format(before), encoding="utf-8")
                restore(root, root)
                self.assertEqual(config.read_text(encoding="utf-8"), template.format(after))
