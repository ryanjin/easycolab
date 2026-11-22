"""Unit tests for Colab keepalive daemon, RFC 6455 WS framing, and /dev/shm Cgroup RAM guard."""

import ast
from collections import namedtuple
import io
import os
import socket
import subprocess
import sys
import tempfile
import json
import threading
import time
import unittest
from unittest import mock

import colab_bridge
from colab_cli.state import SessionState

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY_PATH = os.path.join(REPO_ROOT, "app.py")


def _load_app_helpers():
    with open(APP_PY_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=APP_PY_PATH)
    wanted = {
        "_build_ws_masked_text_frame",
        "_build_ws_masked_pong_frame",
        "_recv_ws_exact",
        "_recv_ws_frame",
        "_drain_local_ws_frames",
        "_safe_shm_available_bytes",
    }
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    mod = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {
        "os": os,
        "socket": socket,
        "shutil": mock.MagicMock(),
        "Optional": object,
        "Tuple": tuple,
        "LOADED_RUNTIMES": {},
        "_get_system_ram_gb": lambda: (0.0, 0.0),
    }
    exec(compile(mod, APP_PY_PATH, "exec"), ns)
    return ns


class WebSocketFramingTest(unittest.TestCase):
    def test_fragmented_payload_with_0x08_and_0x09_nibbles_not_misread_as_close(self):
        ns = _load_app_helpers()
        s_client, s_server = socket.socketpair()
        self.addCleanup(s_client.close)
        self.addCleanup(s_server.close)

        # Construct a 260-byte unmasked server text frame (opcode=0x1, len=126 extended)
        # whose payload contains 'h' (0x68 & 0x0F == 0x08) and 'i' (0x69 & 0x0F == 0x09) at chunk boundaries.
        payload = b"A" * 50 + b"h" + b"B" * 80 + b"i" + b"C" * 128
        header = bytes([0x81, 126]) + len(payload).to_bytes(2, "big")
        wire = header + payload

        def _trickle():
            s_server.sendall(wire[:54])  # First chunk ends right before 'h' (0x68)
            time.sleep(0.02)
            s_server.sendall(wire[54:135])  # Second chunk starts with 'h' (low nibble 0x08!)
            time.sleep(0.02)
            s_server.sendall(wire[135:])  # Third chunk starts with 'i' (low nibble 0x09!)

        t = threading.Thread(target=_trickle, daemon=True)
        t.start()
        self.assertTrue(ns["_drain_local_ws_frames"](s_client, max_frames=8, poll_timeout=0.5))
        t.join(timeout=1.0)

    def test_ping_echoes_masked_pong_with_payload_and_close_returns_false(self):
        ns = _load_app_helpers()
        s_client, s_server = socket.socketpair()
        self.addCleanup(s_client.close)
        self.addCleanup(s_server.close)

        ping_payload = b"colab-ping-42"
        ping_frame = bytes([0x89, len(ping_payload)]) + ping_payload
        s_server.sendall(ping_frame)

        self.assertTrue(ns["_drain_local_ws_frames"](s_client, max_frames=4, poll_timeout=0.3))

        pong = ns["_recv_ws_frame"](s_server, poll_timeout=0.5)
        self.assertIsNotNone(pong)
        opcode, echoed = pong
        self.assertEqual(opcode, 0x0A)
        self.assertEqual(echoed, ping_payload)

        # Now send an actual RFC 6455 Close frame (0x88)
        s_server.sendall(bytes([0x88, 0x02, 0x03, 0xE8]))
        self.assertFalse(ns["_drain_local_ws_frames"](s_client, max_frames=4, poll_timeout=0.3))


class SafeShmCgroupGuardTest(unittest.TestCase):
    def test_blocks_shm_staging_when_cgroup_ram_is_pinned_by_hot_standby(self):
        ns = _load_app_helpers()
        DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
        ns["shutil"].disk_usage.return_value = DiskUsage(88 * 1024**3, 0, 88 * 1024**3)
        # Simulate G4 (176GB total) with MiniMax-H3 Tier-1 pinning ~146GB, leaving only 30GB MemAvailable
        ns["_get_system_ram_gb"] = lambda: (176.0, 30.0)
        ns["LOADED_RUNTIMES"] = {"minimax_h3": {"mode_label": "MiniMax-H3 · 双主干内存热备"}}

        with mock.patch("os.path.exists", return_value=True):
            self.assertEqual(ns["_safe_shm_available_bytes"](reserve_ram_gb=34.0), 0)

    def test_allows_shm_staging_when_system_ram_is_plentiful(self):
        ns = _load_app_helpers()
        DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
        ns["shutil"].disk_usage.return_value = DiskUsage(88 * 1024**3, 8 * 1024**3, 80 * 1024**3)
        ns["_get_system_ram_gb"] = lambda: (176.0, 150.0)
        ns["LOADED_RUNTIMES"] = {}

        with mock.patch("os.path.exists", return_value=True):
            avail = ns["_safe_shm_available_bytes"](reserve_ram_gb=34.0)
            self.assertEqual(avail, 80 * 1024**3)


class ColabBridgeKeepaliveTest(unittest.TestCase):
    def test_quiet_colab_cli_keeps_stdout_clean(self):
        fake_out = io.StringIO()
        fake_err = io.StringIO()
        with mock.patch.object(sys, "stdout", fake_out), mock.patch.object(sys, "stderr", fake_err):
            with colab_bridge._quiet_colab_cli():
                print("[colab] Pruned 1 stale local session(s).")
            print('{"ok": true}')
        self.assertEqual(fake_out.getvalue().strip(), '{"ok": true}')
        self.assertIn("Pruned 1 stale", fake_err.getvalue())

    def test_resolve_remote_kernel_never_deletes_other_kernels_and_detects_401(self):
        sess = SessionState(
            name="easycolab",
            token="tok_abc",
            url="https://8080-gpu-test.prod.r.colab.dev",
            endpoint="m-s-test",
            kernel_id="kid-main",
            session_id="sid-fixed",
        )
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = '[{"id":"kid-other"},{"id":"kid-main"}]'
        fake_resp.json.return_value = [{"id": "kid-other"}, {"id": "kid-main"}]

        with mock.patch("colab_bridge.requests.get", return_value=fake_resp) as m_get, \
             mock.patch("colab_bridge.requests.delete") as m_del, \
             mock.patch("colab_bridge._remember_kernel"):
            kid = colab_bridge._resolve_or_start_remote_kernel(sess)
            self.assertEqual(kid, "kid-main")
            m_del.assert_not_called()
            _, kwargs = m_get.call_args
            self.assertEqual(kwargs["params"]["colab-runtime-proxy-token"], "tok_abc")
            self.assertEqual(kwargs["params"]["authuser"], "0")

        expired_resp = mock.MagicMock(status_code=401, text="")
        with mock.patch("colab_bridge.requests.get", return_value=expired_resp):
            with self.assertRaises(colab_bridge._ProxyAuthExpiredError):
                colab_bridge._resolve_or_start_remote_kernel(sess)

    def test_open_colab_proxy_ws_includes_token_query_and_headers(self):
        sess = SessionState(
            name="easycolab",
            token="proxy_tok_123",
            url="https://8080-gpu-test.prod.r.colab.dev/",
            endpoint="m-s-test",
            kernel_id="kid-1",
            session_id="sid-1",
        )
        with mock.patch("colab_bridge.websocket.create_connection") as m_ws:
            colab_bridge._open_colab_proxy_ws(sess, "kid-1", timeout_s=9.0)
            args, kwargs = m_ws.call_args
            ws_url = args[0]
            self.assertTrue(ws_url.startswith("wss://8080-gpu-test.prod.r.colab.dev/api/kernels/kid-1/channels?"))
            self.assertIn("colab-runtime-proxy-token=proxy_tok_123", ws_url)
            self.assertIn("token=proxy_tok_123", ws_url)
            self.assertIn("authuser=0", ws_url)
            self.assertIn("session_id=sid-1", ws_url)
            self.assertIn("X-Colab-Runtime-Proxy-Token: proxy_tok_123", kwargs["header"])

    def test_keepalive_daemon_exits_promptly_when_parent_stdin_closes(self):
        code = (
            "import time, unittest.mock as m, colab_bridge;"
            "m.patch('colab_bridge._has_valid_token', return_value=True).start();"
            "m.patch('colab_bridge._owned_sessions', return_value={}).start();"
            "info = type('I', (), {'paid_compute_units_balance': 100.0, 'consumption_rate_hourly': 0.0, 'assignments_count': 0})();"
            "m.patch.object(colab_bridge.state, '_client', type('C', (), {'get_consumption_user_info': lambda self: info})()).start();"
            "colab_bridge.cmd_keepalive_daemon()"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_line = proc.stdout.readline().strip()
        self.assertTrue(first_line.startswith("KEEPALIVE_JSON:"), msg=first_line)
        # Close parent stdin pipe -> daemon must exit immediately without hanging for 6s+
        t0 = time.time()
        proc.stdin.close()
        rc = proc.wait(timeout=2.5)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 2.0)


def _fake_assignment(endpoint="gpu-owned"):
    from colab_cli.client import Assignment
    return Assignment(endpoint=endpoint, runtimeProxyInfo={
        "token": "proxy-token", "url": "https://8080-gpu.prod.r.colab.dev", "tokenExpiresInSeconds": 3600
    })


def _listed(endpoint):
    return mock.Mock(endpoint=endpoint, accelerator=colab_bridge.Accelerator.G4)


class HighTierAllocationReconciliationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(mock.patch.object(colab_bridge, "SESSION_DB", os.path.join(tmp.name, "sessions.sqlite3")))
        self.client = mock.MagicMock()
        self.enterContext(mock.patch.object(colab_bridge.state, "_client", self.client))
        self.enterContext(mock.patch("colab_bridge._has_valid_token", return_value=True))
        self.enterContext(mock.patch("colab_bridge.time.sleep"))
        self.out = self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))

    def test_allocate_recovers_lost_reply_by_retrying_the_same_private_notebook(self):
        import requests
        self.client.assign.side_effect = [requests.exceptions.ReadTimeout(), _fake_assignment()]
        session = colab_bridge._allocate_fresh_session("G4")
        self.assertEqual(session.endpoint, "gpu-owned")
        notebook_ids = [c.args[0] for c in self.client.assign.call_args_list]
        self.assertEqual(notebook_ids, [colab_bridge._notebook_id(colab_bridge.Accelerator.G4)] * 2)
        self.client.list_assignments.assert_not_called()
        self.client.unassign.assert_not_called()

    def test_allocate_switches_shape_on_503_and_reports_clear_capacity_message(self):
        from colab_cli.client import ColabRequestError, Shape
        error = ColabRequestError("unavailable", None, mock.MagicMock(status_code=503))
        self.client.assign.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            colab_bridge._allocate_fresh_session("H100")
        self.assertEqual([c.kwargs["shape"] for c in self.client.assign.call_args_list],
                         [None, Shape.HIGH_RAM, None])
        self.client.unassign.assert_not_called()

    def test_empty_registry_never_adopts_shared_or_cloud_sessions(self):
        self.client.list_assignments.return_value = [_listed("other-notebook")]
        with mock.patch.object(colab_bridge.state, "sync_sessions", side_effect=AssertionError):
            colab_bridge.cmd_stop_all()
            colab_bridge.cmd_keepalive()
            with mock.patch.dict(os.environ, {"EASYCOLAB_ADMIN_PASSWORD": "x" * 30}):
                self.client.assign.side_effect = colab_bridge.TooManyAssignmentsError()
                colab_bridge.cmd_wake("G4", APP_PY_PATH)
        self.assertIn("HTTP 412", self.out.getvalue())
        self.client._get_assignment.assert_not_called()
        self.client.list_assignments.assert_not_called()
        self.client.unassign.assert_not_called()
        self.client._issue_request.assert_not_called()

    def test_only_google_lookup_by_a_persisted_notebook_id_authorizes_stop(self):
        gpu = colab_bridge.Accelerator.G4
        notebook_id = colab_bridge._notebook_id(gpu)
        self.client._get_assignment.return_value = _fake_assignment()
        self.client.list_assignments.side_effect = [[_listed("gpu-owned"), _listed("foreign")], [_listed("foreign")]]
        self.client.get_consumption_user_info.return_value = mock.Mock(
            consumption_rate_hourly=1.0, paid_compute_units_balance=10.0, assignments_count=1)
        with mock.patch("colab_bridge.ColabRuntime") as runtime:
            colab_bridge.cmd_stop_all()
        self.client._get_assignment.assert_called_once_with(notebook_id, colab_bridge.Variant.GPU, gpu, None)
        self.client.unassign.assert_called_once_with("gpu-owned")
        self.assertEqual(runtime.call_count, 1)
        self.assertEqual(json.loads(self.out.getvalue())["stopped_count"], 1)

    def test_stale_session_does_not_control_a_recycled_endpoint(self):
        self.client.assign.return_value = _fake_assignment()
        previous = colab_bridge._allocate_fresh_session("G4")
        previous.kernel_id = "first-kernel"
        colab_bridge._remember_kernel(previous)
        self.client._get_assignment.return_value = None  # unallocated or another account
        self.client.list_assignments.return_value = [_listed(previous.endpoint)]
        self.assertEqual(colab_bridge._owned_sessions(), {})
        self.client._get_assignment.return_value = _fake_assignment("replacement")
        self.client.list_assignments.return_value = [_listed("replacement")]
        replacement = colab_bridge._owned_sessions()["replacement"]
        colab_bridge._remember_kernel(previous)
        self.assertIsNone(colab_bridge._owned_sessions()[replacement.endpoint].kernel_id)


def _load_minimax_h3_image_helpers(tmp_root: str):
    import json
    import re
    import shutil
    from typing import Any, Dict, List, Optional, Tuple

    with open(APP_PY_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=APP_PY_PATH)
    wanted_assigns = {
        "MINIMAX_H3_BF16_FILES",
        "MODEL_CATALOG",
        "MINIMAX_H3_IMAGE_FILES",
        "BUILTIN_MODEL_IDS",
        "MINIMAX_H3_IMAGE_CONFIG_PATTERNS",
        "MINIMAX_H3_IMAGE_REQUIRED_CONFIG_MARKERS",
    }
    wanted_funcs = {
        "_normalize_builtin_model_alias",
        "_normalize_hf_repo_and_pattern",
        "inspect_and_resolve_hf_repo",
        "_get_model_dirs",
        "_find_existing_model_file",
        "prepare_minimax_h3_image_assembled_dir",
        "run_minimax_h3_image_inference",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in wanted_assigns for t in node.targets):
                selected.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in wanted_assigns:
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            selected.append(node)

    mod = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(mod)

    class _FakeOOMError(RuntimeError):
        pass

    class _FakeGenerator:
        def __init__(self, device="cpu"):
            self.device = device
            self.seed = None

        def manual_seed(self, s):
            self.seed = s
            return self

    fake_torch = mock.MagicMock()
    fake_torch.Generator = _FakeGenerator
    fake_torch.cuda.OutOfMemoryError = _FakeOOMError

    ns = {
        "os": os,
        "re": re,
        "json": json,
        "shutil": shutil,
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "Tuple": Tuple,
        "torch": fake_torch,
        "DRIVE_MOUNTED": False,
        "MODEL_BASE_ROOT": os.path.join(tmp_root, "drive_models"),
        "LEGACY_DRIVE_MODEL_ROOTS": [],
        "_safe_shm_available_bytes": lambda reserve_ram_gb=34.0: 0,
        "_stage_model_files_to_local_nvme": lambda mid, paths, rels, lbl: paths,
        "_reclaim_transient_gpu_memory": mock.MagicMock(),
    }
    exec(compile(mod, APP_PY_PATH, "exec"), ns)
    return ns, _FakeOOMError


class MiniMaxH3ImageEngineTest(unittest.TestCase):
    def test_catalog_and_custom_import_resolution_excludes_sglang_duplicates(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ns, _ = _load_minimax_h3_image_helpers(tmp)
            spec = ns["MODEL_CATALOG"]["minimax_h3_image"]
            files = [fn for fn, _ in spec["files"]]
            self.assertEqual(len(files), 27)
            self.assertTrue(all(not fn.startswith("transformer/model-") for fn in files))
            self.assertNotIn("vae/diffusion_pytorch_model.safetensors", files)
            self.assertIn("transformer_diffusers/diffusion_pytorch_model-00001-of-00012.safetensors", files)
            self.assertIn("vae_diffusers/diffusion_pytorch_model.safetensors", files)

            resolved_img = ns["inspect_and_resolve_hf_repo"]("https://huggingface.co/MiniMaxAI/MiniMax-H3-Image")
            self.assertEqual(resolved_img["modality"], "image")
            self.assertEqual(resolved_img["pipeline_arch"], "minimax_h3_image")
            self.assertEqual(len(resolved_img["files"]), 27)

            resolved_vid = ns["inspect_and_resolve_hf_repo"]("https://huggingface.co/MiniMaxAI/MiniMax-H3")
            self.assertEqual(resolved_vid["modality"], "video_audio")
            self.assertEqual(resolved_vid["pipeline_arch"], "minimax_h3")

            self.assertEqual(
                ns["_normalize_builtin_model_alias"]("custom_minimaxai_minimax_h3_image"),
                "minimax_h3_image",
            )

    def test_shared_text_encoder_zero_copy_and_local_symlink_assembly(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ns, _ = _load_minimax_h3_image_helpers(tmp)
            with mock.patch("os.getcwd", return_value=tmp):
                # Populate 14 text_encoder shards ONLY under MiniMax-H3's FL2VA/text_encoder
                h3_drive = os.path.join(tmp, "drive_models", "MiniMaxAI/MiniMax-H3")
                for i in range(1, 15):
                    p = os.path.join(h3_drive, f"FL2VA/text_encoder/model-{i:05d}-of-00014.safetensors")
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "wb") as wf:
                        wf.write(b"T" * (80 * 1024))

                # Populate 12 transformer_diffusers + 1 vae_diffusers under MiniMax-H3-Image
                img_drive = os.path.join(tmp, "drive_models", "MiniMaxAI/MiniMax-H3-Image")
                for i in range(1, 13):
                    p = os.path.join(
                        img_drive,
                        f"transformer_diffusers/diffusion_pytorch_model-{i:05d}-of-00012.safetensors",
                    )
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "wb") as wf:
                        wf.write(b"D" * (80 * 1024))
                vae_p = os.path.join(img_drive, "vae_diffusers/diffusion_pytorch_model.safetensors")
                os.makedirs(os.path.dirname(vae_p), exist_ok=True)
                with open(vae_p, "wb") as wf:
                    wf.write(b"V" * (80 * 1024))

                # Pre-populate lightweight config markers so no network call is needed
                assembled_expected = os.path.join(tmp, "models", "_assembled", "minimax_h3_image")
                for marker in ns["MINIMAX_H3_IMAGE_REQUIRED_CONFIG_MARKERS"]:
                    mp = os.path.join(assembled_expected, marker)
                    os.makedirs(os.path.dirname(mp), exist_ok=True)
                    with open(mp, "w", encoding="utf-8") as mf:
                        mf.write("{}")

                # Verify cross-model lookup finds MiniMax-H3's FL2VA/text_encoder shards
                shared_hit = ns["_find_existing_model_file"](
                    "minimax_h3_image", "text_encoder/model-00001-of-00014.safetensors"
                )
                self.assertIsNotNone(shared_hit)
                self.assertIn("MiniMax-H3/FL2VA/text_encoder", shared_hit)

                out_dir = ns["prepare_minimax_h3_image_assembled_dir"](
                    target_id="minimax_h3_image",
                    repo_id="MiniMaxAI/MiniMax-H3-Image",
                    link_weights=True,
                )
                self.assertEqual(out_dir, assembled_expected)
                for fn, _ in ns["MINIMAX_H3_IMAGE_FILES"]:
                    linked = os.path.join(out_dir, fn)
                    self.assertTrue(os.path.islink(linked), f"Expected symlink for {fn}")
                    self.assertTrue(os.path.exists(linked), f"Broken symlink for {fn}")

    def test_inference_signature_and_oom_tiling_retry(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ns, FakeOOM = _load_minimax_h3_image_helpers(tmp)
            calls = []

            class _FakeImg:
                def convert(self, mode):
                    return f"RGB({mode})"

            fake_vae = mock.MagicMock()

            def _pipe_side_effect(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise FakeOOM("CUDA out of memory during 4K decode")
                return ["FINAL_4K_PIL_IMAGE"]

            fake_pipe = mock.MagicMock(side_effect=_pipe_side_effect)
            fake_pipe.vae = fake_vae

            res = ns["run_minimax_h3_image_inference"](
                active_pipe=fake_pipe,
                prompt="Put the cat from <Picture 1> onto <Picture 2>",
                width=4096,
                height=3072,
                seed=12345,
                ref_images=[_FakeImg(), _FakeImg()],
            )
            self.assertEqual(res, "FINAL_4K_PIL_IMAGE")
            self.assertEqual(len(calls), 2)
            fake_vae.enable_tiling.assert_called_once()
            ns["_reclaim_transient_gpu_memory"].assert_called_once()

            first_kw = calls[0]
            self.assertEqual(first_kw["output"], "images")
            self.assertEqual(first_kw["width"], 4096)
            self.assertEqual(first_kw["height"], 3072)
            self.assertEqual(first_kw["generator"].device, "cpu")
            self.assertEqual(first_kw["generator"].seed, 12345)
            self.assertEqual(len(first_kw["image"]), 2)
            self.assertNotIn("num_inference_steps", first_kw)
            self.assertNotIn("guidance_scale", first_kw)
            self.assertNotIn("negative_prompt", first_kw)


if __name__ == "__main__":
    unittest.main()
