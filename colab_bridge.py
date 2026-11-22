#!/usr/bin/env python3
"""
Local Headless Google Colab Bridge for EasyColab macOS App.
Powered by google-colab-cli (inspired by FireTable/comfy-colab-kit).
Supports:
  - auth_status : Check OAuth login state, real CU balance, hourly burn rate, and active VMs
  - auth_url    : Generate Google OAuth2 authorization URL for 1-click browser login
  - auth_code   : Exchange Google authorization code and save ~/.config/colab-cli/token.json
  - wake        : Provision/reuse Colab GPU VM (G4/H100/A100/L4/T4), deploy app.py, and return gradio.live URL
  - keepalive   : Refresh Colab runtime proxy & kernel activity while active
  - stop_all    : Stop only the runtimes allocated by this EasyColab installation
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
import signal
import argparse
import hashlib
import sqlite3
import tempfile
import threading
import contextlib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from importlib import resources

import requests
import websocket
from websocket import ABNF
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


def _ensure_utf8_stdio():
    """确保 Windows / macOS 子进程管道输出始终采用 UTF-8 编码，避免 emoji 在 GBK 代码页下抛出 UnicodeEncodeError"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_ensure_utf8_stdio()


def _sync_macos_system_proxies_to_env():
    """
    macOS / Linux 原生 GUI 应用启动子进程时默认可能不包含终端的 HTTP_PROXY / HTTPS_PROXY 环境变量，
    而 websocket-client (Jupyter 内核 WebSocket 通道) 仅从 os.environ 读取代理配置。
    此处自动读取 macOS / Windows / Linux 系统级代理 (scutil --proxy / Internet Settings / gsettings / urllib.request.getproxies) 并注入环境变量。
    """
    try:
        proxies = urllib.request.getproxies()
        for k in ("http", "https"):
            if k in proxies and proxies[k]:
                os.environ.setdefault(f"{k}_proxy", proxies[k])
                os.environ.setdefault(f"{k.upper()}_PROXY", proxies[k])
        if sys.platform.startswith("linux") and not (os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")):
            import subprocess
            mode = subprocess.run(
                ["gsettings", "get", "org.gnome.system.proxy", "mode"],
                capture_output=True, text=True, timeout=2
            ).stdout.strip().strip("'")
            if mode == "manual":
                for k in ("http", "https"):
                    host = subprocess.run(
                        ["gsettings", "get", f"org.gnome.system.proxy.{k}", "host"],
                        capture_output=True, text=True, timeout=2
                    ).stdout.strip().strip("'")
                    port_raw = subprocess.run(
                        ["gsettings", "get", f"org.gnome.system.proxy.{k}", "port"],
                        capture_output=True, text=True, timeout=2
                    ).stdout.strip()
                    port = port_raw.split()[-1].strip("'") if port_raw else ""
                    if host and port and port.isdigit() and int(port) > 0:
                        proxy_url = f"http://{host}:{port}"
                        os.environ.setdefault(f"{k}_proxy", proxy_url)
                        os.environ.setdefault(f"{k.upper()}_PROXY", proxy_url)
    except Exception:
        pass


_sync_macos_system_proxies_to_env()
os.environ.setdefault("COLAB_CLI_REQUEST_TIMEOUT", "75")

from colab_cli import auth as colab_auth
from colab_cli.auth import PUBLIC_SCOPES, REMOTE_REDIRECT_URI
from colab_cli.common import state
from colab_cli.client import (
    Accelerator,
    Client,
    Prod,
    Variant,
    Shape,
    resolve_assign_shape,
    Assignment,
    ColabRequestError,
    TooManyAssignmentsError,
)
from colab_cli.utils import get_status_code
from colab_cli.state import SessionState
from colab_cli.runtime import ColabRuntime
from colab_cli.contents import ContentsClient


def _load_client_config() -> dict:
    cfg_path = os.path.expanduser("~/.colab-cli-oauth-config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    res = resources.files("colab_cli").joinpath("oauth_config.json")
    return json.loads(res.read_text(encoding="utf-8"))


def _write_private_file(path, contents: str):
    """Publish a complete, owner-only file, including when replacing older credentials."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# 0.8 moved the CLI's OAuth token to the system credential store. Both
# versions use the same installed-app OAuth client and copy/paste flow.
USES_KEYRING = hasattr(colab_auth, "LEGACY_TOKEN_PATH")
TOKEN_CONFIG_PATH = colab_auth.LEGACY_TOKEN_PATH if USES_KEYRING else colab_auth.TOKEN_CONFIG_PATH


class BridgeAuthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _remove_if_present(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _save_credentials(creds, *, remove_legacy=False):
    try:
        if USES_KEYRING:
            colab_auth._save_token_to_keyring(creds.to_json())
            if remove_legacy:
                _remove_if_present(TOKEN_CONFIG_PATH)
        else:
            _write_private_file(TOKEN_CONFIG_PATH, creds.to_json())
    except Exception as error:
        raise BridgeAuthError("oauth_store", "Could not save Google credentials. Unlock the system credential store and check access to the Colab CLI settings folder.") from error


def _load_credentials():
    """Load existing credentials without running the CLI's interactive login."""
    try:
        stored = colab_auth._get_token_from_keyring() if USES_KEYRING else None
        if stored:
            creds = Credentials.from_authorized_user_info(json.loads(stored), PUBLIC_SCOPES)
        else:
            if not os.path.isfile(TOKEN_CONFIG_PATH):
                return None
            if os.name == "posix" and os.stat(TOKEN_CONFIG_PATH).st_mode & 0o077:
                os.chmod(TOKEN_CONFIG_PATH, 0o600)
            creds = Credentials.from_authorized_user_file(TOKEN_CONFIG_PATH, PUBLIC_SCOPES)
    except Exception as error:
        raise BridgeAuthError("oauth_store", "Could not read Google credentials. Unlock the system credential store or bind the account again.") from error

    refreshed = False
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as error:
            raise BridgeAuthError("oauth_refresh", "Could not refresh Google authorization. Check the network and bind the account again if authorization expired.") from error
        refreshed = True
    if not creds.valid:
        return None
    migrate = USES_KEYRING and not stored
    if refreshed or migrate:
        _save_credentials(creds, remove_legacy=migrate)
    return creds


def _authenticated_credentials():
    creds = _load_credentials()
    if creds is not None:
        # state.client otherwise asks the upstream CLI to authenticate again.
        # It may read terminal input if the store is locked or unavailable.
        state._client = Client(Prod(), AuthorizedSession(creds))
    return creds


def _has_valid_token() -> bool:
    return _authenticated_credentials() is not None


PKCE_VERIFIER_PATH = os.path.expanduser("~/.config/colab-cli/pkce_verifier.txt")
ACCOUNT_PROFILE_CACHE_PATH = os.path.expanduser("~/.config/colab-cli/account_profile.json")


def _get_account_email(creds) -> str:
    """Cache profile metadata by token fingerprint."""
    try:
        token = creds.refresh_token or creds.token
        tok_fp = hashlib.sha256(token.encode()).hexdigest() if token else ""
        if tok_fp and os.path.exists(ACCOUNT_PROFILE_CACHE_PATH):
            try:
                with open(ACCOUNT_PROFILE_CACHE_PATH, "r", encoding="utf-8") as cf:
                    cached = json.load(cf)
                if cached.get("fp") == tok_fp and cached.get("email"):
                    return str(cached["email"])
            except Exception:
                pass
        if creds and creds.token:
            r = requests.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {creds.token}"},
                timeout=5,
            )
            if r.status_code == 200:
                email = str(r.json().get("email") or "").strip()
                if email and tok_fp:
                    _write_private_file(ACCOUNT_PROFILE_CACHE_PATH, json.dumps({"fp": tok_fp, "email": email}))
                return email
    except Exception:
        pass
    return ""


def _oauth_flow():
    try:
        flow = InstalledAppFlow.from_client_config(_load_client_config(), PUBLIC_SCOPES, autogenerate_code_verifier=True)
        flow.redirect_uri = REMOTE_REDIRECT_URI
        return flow
    except Exception as error:
        raise BridgeAuthError("oauth_config", "Could not load the Google OAuth client configuration. Check the custom OAuth JSON or reinstall the Colab CLI dependency.") from error


def cmd_auth_url():
    flow = _oauth_flow()
    try:
        auth_url, _ = flow.authorization_url(prompt="select_account consent", token_usage="remote")
    except Exception as error:
        raise BridgeAuthError("oauth_config", "Could not build the Google authorization link. Check the OAuth client configuration.") from error
    try:
        _write_private_file(PKCE_VERIFIER_PATH, flow.code_verifier)
    except (OSError, TypeError) as error:
        raise BridgeAuthError("oauth_verifier", "Could not save the sign-in verifier. Check write access to the Colab CLI settings folder.") from error
    print(json.dumps({"ok": True, "auth_url": auth_url}))


def cmd_auth_code(code: str):
    flow = _oauth_flow()
    try:
        with open(PKCE_VERIFIER_PATH, encoding="utf-8") as stream:
            flow.code_verifier = stream.read().strip()
        if not flow.code_verifier:
            raise ValueError("empty verifier")
    except (OSError, ValueError) as error:
        raise BridgeAuthError("oauth_verifier", "Start Google account binding again to generate a new sign-in verifier.") from error
    try:
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
    except Exception as error:
        raise BridgeAuthError("oauth_exchange", "Google authorization failed. Check the network and use a fresh authorization code from the most recent sign-in link.") from error

    _save_credentials(creds, remove_legacy=True)
    state._client = None
    _remove_if_present(ACCOUNT_PROFILE_CACHE_PATH)
    _remove_if_present(PKCE_VERIFIER_PATH)
    cmd_auth_status()


def cmd_auth_logout():
    try:
        if USES_KEYRING and colab_auth._get_token_from_keyring() is not None:
            colab_auth._run_with_timeout(
                colab_auth.keyring.delete_password, colab_auth.KEYRING_SERVICE_NAME,
                colab_auth.KEYRING_USERNAME, timeout=colab_auth.KEYRING_TIMEOUT_SECONDS,
            )
        for path in (TOKEN_CONFIG_PATH, PKCE_VERIFIER_PATH, ACCOUNT_PROFILE_CACHE_PATH):
            _remove_if_present(path)
    except Exception as error:
        raise BridgeAuthError("oauth_store", "Could not remove Google credentials. Unlock the system credential store and retry signing out.") from error
    state._client = None
    print(json.dumps({"ok": True, "logged_in": False, "email": ""}))


@contextlib.contextmanager
def _quiet_colab_cli():
    """将 colab_cli 内部 typer.echo (如 Pruned stale local session) 重定向至 stderr，保持 stdout 纯净 JSON 输出"""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# The Colab CLI session store can contain browser and other CLI workloads. A private,
# durable notebook UUID lets Google identify our allocation even after a lost POST reply.
SESSION_DB = os.path.expanduser("~/.config/easycolab/sessions.sqlite3")


@contextlib.contextmanager
def _session_db():
    os.makedirs(os.path.dirname(SESSION_DB), mode=0o700, exist_ok=True)
    with contextlib.closing(sqlite3.connect(SESSION_DB)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS notebooks (
            gpu TEXT PRIMARY KEY, notebook_id TEXT UNIQUE NOT NULL, high_ram INTEGER NOT NULL DEFAULT 0,
            endpoint TEXT, kernel_id TEXT, session_id TEXT
        )""")
        yield db


def _notebook_id(accel: Accelerator) -> uuid.UUID:
    with _session_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO notebooks (gpu, notebook_id) VALUES (?, ?)",
            (accel.value, str(uuid.uuid4())),
        )
        row = db.execute("SELECT notebook_id FROM notebooks WHERE gpu = ?", (accel.value,)).fetchone()
    return uuid.UUID(row[0])


def _session_for_notebook(gpu, assignment, high_ram):
    proxy = assignment.runtime_proxy_info
    with _session_db() as db:
        db.execute(
            """UPDATE notebooks SET endpoint = ?, kernel_id = NULL, session_id = ?
               WHERE gpu = ? AND endpoint IS NOT ?""",
            (assignment.endpoint, uuid.uuid4().hex, gpu, assignment.endpoint),
        )
        kernel_id, session_id = db.execute(
            "SELECT kernel_id, session_id FROM notebooks WHERE gpu = ?", (gpu,)
        ).fetchone()
    return SessionState(
        name="easycolab", endpoint=assignment.endpoint, token=proxy.token, url=proxy.url,
        token_expires_at=proxy.expires_at(), session_id=session_id, kernel_id=kernel_id,
        variant=Variant.GPU.value, accelerator=gpu,
        machine_shape=Shape.HIGH_RAM.name if high_ram else Shape.STANDARD.name,
    )


def _remember_kernel(session):
    # An older process must never overwrite the replacement VM's kernel metadata.
    with _session_db() as db:
        db.execute(
            "UPDATE notebooks SET kernel_id = ?, session_id = ? WHERE gpu = ? AND endpoint = ?",
            (session.kernel_id, session.session_id, session.accelerator, session.endpoint),
        )


def _owned_sessions():
    with _session_db() as db:
        notebooks = list(db.execute("SELECT gpu, notebook_id, high_ram FROM notebooks"))
    if not notebooks:
        return {}
    # A previously used accelerator can become unavailable on another subscription.
    # Skip inactive types before asking Google about a particular notebook.
    active = {a.endpoint: a.accelerator.value for a in state.client.list_assignments()}
    sessions = {}
    for gpu, notebook_id, high_ram in notebooks:
        if gpu not in active.values():
            continue
        # This is a GET-only lookup by our private notebook UUID. The other response
        # type is an unallocated notebook with an XSRF token. Never POST from here.
        assignment = state.client._get_assignment(
            uuid.UUID(notebook_id), Variant.GPU, Accelerator(gpu), Shape.HIGH_RAM if high_ram else None
        )
        if isinstance(assignment, Assignment) and active.get(assignment.endpoint) == gpu:
            sessions[assignment.endpoint] = _session_for_notebook(gpu, assignment, high_ram)
    return sessions


def cmd_auth_status():
    creds = _authenticated_credentials()
    if creds is None:
        print(json.dumps({"ok": True, "logged_in": False}))
        return

    try:
        info = state.client.get_consumption_user_info()
        assignments = state.client.list_assignments()
        sess_list = []
        for a in assignments:
            sess_list.append({
                "name": "colab-vm",
                "endpoint": a.endpoint,
                "accelerator": a.accelerator.value,
                "variant": a.variant.name,
                "shape": a.machine_shape.name,
            })

        email = _get_account_email(creds)
        print(json.dumps({
            "ok": True,
            "logged_in": True,
            "email": email,
            "paid_balance": round(float(info.paid_compute_units_balance), 2),
            "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
            "active_assignments": int(info.assignments_count),
            "sessions": sess_list,
        }))
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "logged_in": True,
            "email": _get_account_email(creds),
            "error": str(e),
        }))


def _unassign_endpoints_and_wait_quota_release(endpoints: list[str], wait_s: float = 8.0) -> int:
    """
    去重释放指定云端 endpoint 列表，并轮询 list_assignments 等待 Google 配额账本真正注销这些实例。
    避免前脚刚调 unassign、后脚立刻调 assign 时因 Google 后端秒级延迟导致 G4/H100 配额仍显示占用而报 503/412。
    """
    unique_eps = []
    seen = set()
    for ep in endpoints:
        if ep and ep not in seen:
            seen.add(ep)
            unique_eps.append(ep)
    if not unique_eps:
        return 0

    stopped_count = 0
    for ep in unique_eps:
        try:
            state.client.unassign(ep)
            stopped_count += 1
        except Exception:
            pass

    if wait_s > 0:
        deadline = time.time() + wait_s
        target_set = set(unique_eps)
        while time.time() < deadline:
            try:
                remaining = {a.endpoint for a in state.client.list_assignments()}
                if not (remaining & target_set):
                    break
            except Exception:
                break
            time.sleep(1.5)
    return stopped_count


def cmd_stop_all():
    if not _has_valid_token():
        print(json.dumps({"ok": False, "error": "尚未登录 Google Colab CLI"}))
        return

    try:
        local_sessions = _owned_sessions()
        for s in local_sessions.values():
            try:
                with _quiet_colab_cli():
                    rt = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
                    try:
                        rt.execute_code(
                            "import os, urllib.request\n"
                            "try:\n"
                            "    urllib.request.urlopen(urllib.request.Request("
                            "'http://127.0.0.1:27860/minimax_api/comfyui/sync_drive', "
                            "data=b'{}', headers={'Content-Type':'application/json'}, method='POST'), timeout=18)\n"
                            "except Exception:\n"
                            "    pass\n"
                            "try:\n"
                            "    os.sync()\n"
                            "except Exception:\n"
                            "    pass\n"
                            "try:\n"
                            "    if os.path.isdir('/content/drive/MyDrive'):\n"
                            "        from google.colab import drive\n"
                            "        drive.flush_and_unmount()\n"
                            "except Exception:\n"
                            "    pass\n",
                            allow_stdin=False,
                            timeout=30,
                        )
                    except Exception:
                        pass
                    rt.stop(shutdown_kernel=True)
            except Exception:
                pass
        stopped_count = _unassign_endpoints_and_wait_quota_release(list(local_sessions), wait_s=8.0)

        info = state.client.get_consumption_user_info()
        print(json.dumps({
            "ok": stopped_count == len(local_sessions),
            "stopped_count": stopped_count,
            "paid_balance": round(float(info.paid_compute_units_balance), 2),
            "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
            "active_assignments": int(info.assignments_count),
            "message": f"已释放 {stopped_count}/{len(local_sessions)} 个 EasyColab 实例。当前账号计费速率: {info.consumption_rate_hourly:.2f} CU/小时",
            "error": None if stopped_count == len(local_sessions) else "部分 EasyColab 实例释放失败，请重试。",
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def _wait_for_runtime_ready(session: SessionState, timeout_s: int = 45) -> bool:
    """
    等待新分配的 Colab 容器内部 Jupyter Server (/api/kernels) 完成启动。
    规避容器刚启动前几秒出现的 SSLEOFError / ConnectionReset / 502 / 503。
    若检测到 404 (实例如已被销毁)，立即返回 False 以便重新分配新实例。
    """
    base_url = session.url.rstrip("/")
    probe_url = f"{base_url}/api/kernels"
    headers = {
        "Accept": "application/json",
        "X-Colab-Client-Agent": "colab-cli",
        "X-Colab-Runtime-Proxy-Token": session.token,
        "Authorization": f"Bearer {session.token}",
    }
    params = {"authuser": "0", "colab-runtime-proxy-token": session.token}
    deadline = time.time() + timeout_s
    not_found_count = 0

    while time.time() < deadline:
        try:
            r = requests.get(probe_url, headers=headers, params=params, timeout=8)
            if r.status_code == 200:
                return True
            if r.status_code in (404, 410):
                not_found_count += 1
                if not_found_count >= 2:
                    return False
        except requests.exceptions.RequestException:
            pass
        time.sleep(2.0)
    return False


GPU_ACCEL_MAP = {
    "h100": Accelerator.H100,
    "g4": Accelerator.G4,
    "a100": Accelerator.A100,
    "l4": Accelerator.L4,
    "t4": Accelerator.T4,
}


def _resolve_gpu_accel(gpu_type: str) -> Accelerator:
    return GPU_ACCEL_MAP.get((gpu_type or "g4").strip().lower(), Accelerator.G4)


def _gpu_display_label(accel: Accelerator) -> str:
    if accel == Accelerator.G4:
        return "G4 (RTX PRO 6000 · 96GB)"
    return accel.value


def _candidate_shapes_for_accel(accel: Accelerator) -> list:
    """
    为各型号 GPU 提供主备机器内存规格 (Shape) 候选序列：
    - G4 / H100：首选不传 shape (默认大内存标准池，避免部分区域对 shape=hm 报 400)，若遇 503 库存不足自动尝试 HIGH_RAM 池；
    - A100 / T4：首选 HIGH_RAM 大内存池，若遇 400/503 自动降级尝试标准内存池；
    - L4：官方固定单规格 (HIGH_MEM_ONLY_ACCELERATORS)，仅使用 None。
    """
    if accel == Accelerator.L4:
        return [None]
    if accel in (Accelerator.G4, Accelerator.H100):
        return [None, Shape.HIGH_RAM]
    primary = resolve_assign_shape(accel, high_mem=True)
    return [primary, None] if primary is not None else [None]


@contextlib.contextmanager
def _single_shot_colab_assign():
    """禁用 colab_cli 内部静默阻塞 70 秒的英文重试循环，交由 EasyColab 透明重试并实时对账云端分配"""
    prev_retries = os.environ.get("COLAB_CLI_ASSIGN_RETRIES")
    os.environ["COLAB_CLI_ASSIGN_RETRIES"] = "0"
    try:
        yield
    finally:
        if prev_retries is None:
            os.environ.pop("COLAB_CLI_ASSIGN_RETRIES", None)
        else:
            os.environ["COLAB_CLI_ASSIGN_RETRIES"] = prev_retries


def _allocate_fresh_session(gpu_type: str) -> SessionState:
    accel = _resolve_gpu_accel(gpu_type)
    gpu_desc = _gpu_display_label(accel)
    shape_candidates = list(_candidate_shapes_for_accel(accel))
    rejected_400_shapes = set()
    notebook_id = _notebook_id(accel)

    print(f"WAKE_LOG: 🚀 正在向 Google Colab 申请分配 {gpu_desc} 实例...", flush=True)

    max_attempts = 3
    retry_delays = [6.0, 10.0]
    attempt = 0
    shape_idx = 0
    last_err_tag = "HTTP 503"
    res = None
    chosen_shape = shape_candidates[0]

    while attempt < max_attempts:
        valid_shapes = [sh for sh in shape_candidates if sh not in rejected_400_shapes] or [None]
        chosen_shape = valid_shapes[shape_idx % len(valid_shapes)]
        with _session_db() as db:
            db.execute("UPDATE notebooks SET high_ram = ? WHERE gpu = ?", (chosen_shape == Shape.HIGH_RAM, accel.value))

        try:
            with _quiet_colab_cli(), _single_shot_colab_assign():
                res = state.client.assign(notebook_id, variant=Variant.GPU, accelerator=accel, shape=chosen_shape)
            break
        except TooManyAssignmentsError as quota_err:
            raise RuntimeError(
                f"Google Colab 拒绝分配 {gpu_desc} (HTTP 412)：当前账号并发实例数已达上限，或该时段高配机型触发临时配额限制。"
                f"请确认未在其他浏览器标签页占用实例，稍后重试或临时切换为 A100 / L4。"
            ) from quota_err
        except (ColabRequestError, requests.exceptions.RequestException) as req_err:
            status = get_status_code(req_err) if isinstance(req_err, ColabRequestError) else None

            if status == 400 and chosen_shape is not None and None not in rejected_400_shapes:
                rejected_400_shapes.add(chosen_shape)
                shape_idx = 0
                continue
            if status == 400:
                raise RuntimeError(
                    f"Google Colab 后端拒绝分配 {gpu_desc} (HTTP 400)：当前账号订阅等级可能暂不支持该机型或可用计算单元不足，"
                    f"建议切换为 A100 / L4 / T4 实例。"
                ) from req_err

            retryable = isinstance(req_err, requests.exceptions.RequestException) or status in (429, 500, 502, 503, 504)
            if not retryable:
                raise

            last_err_tag = f"HTTP {status}" if status else type(req_err).__name__
            attempt += 1
            shape_idx += 1
            if attempt >= max_attempts:
                break

            delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
            print(
                f"WAKE_LOG: ⏳ Google Colab 当前区域 {gpu_desc} 算力池繁忙 ({last_err_tag})，"
                f"{delay:.0f} 秒后进行第 {attempt + 1}/{max_attempts} 次申请...",
                flush=True,
            )
            time.sleep(delay)

    if res is None:
        raise RuntimeError(
            f"Google Colab 云端当前时段 {gpu_desc} 算力池库存不足 ({last_err_tag})，连续 {max_attempts} 次申请均未分到机器"
            f"（属于 Colab 官方高配机型时段性缺货，非本地客户端故障）。建议稍等几分钟再试，或在左侧临时切换为 A100 / L4 实例。"
        )

    s = _session_for_notebook(accel.value, res, chosen_shape == Shape.HIGH_RAM)
    print(f"WAKE_LOG: ✅ 成功分配 {accel.value} 云端虚拟机！正在等待容器内核就绪...", flush=True)
    return s


def cmd_wake(gpu_type: str, app_py_path: str):
    """Reuse or allocate an EasyColab-owned GPU and deploy the bundled server."""
    if not _has_valid_token():
        print("WAKE_ERROR: 尚未授权登录 Google 账号，请先点击「🔑 绑定账号」", flush=True)
        return

    import getpass
    password = os.environ.pop("EASYCOLAB_ADMIN_PASSWORD", "")
    raw_idle_min = os.environ.get("EASYCOLAB_IDLE_TIMEOUT_MINUTES", "").strip()
    try:
        initial_idle_minutes = int(raw_idle_min) if raw_idle_min != "" else None
    except ValueError:
        initial_idle_minutes = None
    if not password and sys.stdin.isatty():
        password = getpass.getpass("Management password (24+ URL-safe characters): ")
    if not password:
        print("WAKE_ERROR: Set a management password before deploying", flush=True)
        return

    try:
        requested_accel = _resolve_gpu_accel(gpu_type)
        print("WAKE_LOG: 正在检查此 EasyColab 安装创建的实例...", flush=True)
        sessions = _owned_sessions()
        target_session = None
        for session in sessions.values():
            if session.accelerator == requested_accel.value and _wait_for_runtime_ready(session, timeout_s=18):
                target_session = session
                break

        old_endpoints = [ep for ep in sessions if target_session is None or ep != target_session.endpoint]
        if old_endpoints:
            released = _unassign_endpoints_and_wait_quota_release(old_endpoints)
            if released != len(old_endpoints):
                raise RuntimeError("部分旧 EasyColab 实例释放失败，请重试。")
        if target_session is None:
            target_session = _allocate_fresh_session(gpu_type)
            if not _wait_for_runtime_ready(target_session, timeout_s=50):
                print("WAKE_LOG: ⏳ 容器仍在初始化，继续尝试连接内核...", flush=True)

        print("WAKE_LOG: 正在上传最新多模型开源服务端代码 (app.py)...", flush=True)
        contents = ContentsClient(target_session)
        for up_attempt in range(4):
            try:
                contents.upload(app_py_path, "content/app.py")
                break
            except Exception as up_err:
                if up_attempt == 3:
                    raise up_err
                time.sleep(2.5)

        contents.upload(os.path.join(os.path.dirname(app_py_path), "runtime_auth.py"), "content/runtime_auth.py")
        print("WAKE_LOG: 正在云端配置 Google Drive 持久化、多模型引擎并启动隧道 (约需 40~90 秒)...", flush=True)
        boot_code = r"""
import os, sys, time, re, shutil, subprocess

with open(ADMIN_STAGE, encoding="ascii") as stream:
    admin_password = stream.read()
os.unlink(ADMIN_STAGE)

# A previous release may have persisted a full Google refresh token. Do not start
# third-party code in that runtime; leave unrelated rclone configuration untouched.
legacy_stage = "/content/.easycolab_rclone.conf"
legacy_config = os.path.expanduser("~/.config/rclone/rclone.conf")
if os.path.lexists(legacy_stage):
    raise RuntimeError("检测到旧版 EasyColab 云盘凭据文件，请撤销旧授权并使用新实例。")
if os.path.isfile(legacy_config):
    with open(legacy_config, encoding="utf-8", errors="replace") as config:
        if any(line.strip().lower() == "[easycolab_gdrive]" for line in config):
            raise RuntimeError("检测到旧版 EasyColab 云盘凭据配置，请撤销旧授权并使用新实例。")

with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
    if any(" - fuse.rclone easycolab_gdrive:" in line for line in mounts):
        raise RuntimeError("检测到旧版 EasyColab 云盘挂载，请撤销旧授权并使用新实例。")

if not shutil.which("nginx"):
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "nginx-light"], check=True)
    # apt may start the distribution's default site.
    subprocess.run(["service", "nginx", "stop"], check=False)

# 1. 如果尚未安装 DiffSynth-Studio，自动快速安装 (含最新 transformers 以支持 Qwen3.8-27B)
if not os.path.exists("/content/DiffSynth-Studio"):
    print("正在克隆 DiffSynth-Studio...", flush=True)
    subprocess.run(["git", "clone", "--depth", "1", "https://github.com/modelscope/DiffSynth-Studio.git", "/content/DiffSynth-Studio"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "transformers", "accelerate", "bitsandbytes", "huggingface_hub", "hf_transfer", "qwen-vl-utils", "einops", "kernels>=0.17.2,<0.18.0", "av", "gradio", "modelscope", "imageio", "imageio-ffmpeg", "scipy", "soundfile", "librosa"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "fla-core", "flash-linear-attention"], check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--only-binary=:all:", "causal-conv1d"], check=False)
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", "/content/DiffSynth-Studio"], check=True)
else:
    try:
        from fla.ops.gated_delta_rule import chunk_gated_delta_rule  # noqa: F401
        import kernels  # noqa: F401
    except Exception:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "einops", "kernels>=0.17.2,<0.18.0"], check=False)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", "fla-core", "flash-linear-attention"], check=False)

# 2. 检查是否已有运行中的 app.py 或 llama-server，先清理旧进程释放 GPU 显存
subprocess.run(["pkill", "-f", "python.*/content/app.py"], check=False)
subprocess.run(["pkill", "-f", "llama-server"], check=False)
time.sleep(1)

# 3. 后台启动 /content/app.py 并等待 gradio.live 链接生成
log_path = "/content/easycolab_server.log"
if os.path.exists(log_path):
    os.remove(log_path)

with open(log_path, "w") as out_f:
    child_env = {**os.environ, "EASYCOLAB_ADMIN_PASSWORD": admin_password}
    if INITIAL_IDLE_MINUTES is not None:
        child_env["EASYCOLAB_IDLE_TIMEOUT_MINUTES"] = str(INITIAL_IDLE_MINUTES)
    subprocess.Popen([sys.executable, "-u", "/content/app.py"], cwd="/content", env=child_env, stdout=out_f, stderr=subprocess.STDOUT)

url_re = re.compile(r"^EasyColab URL: (https://[a-zA-Z0-9-]+\.gradio\.live)$", re.MULTILINE)
found_url = None
for _ in range(90):
    time.sleep(2)
    if os.path.exists(log_path):
        txt = open(log_path, "r", errors="replace").read()
        m = url_re.search(txt)
        if m:
            found_url = m.group(1)
            break

if found_url:
    print("WAKE_URL:" + found_url, flush=True)
else:
    tail = open(log_path, "r", errors="replace").read()[-600:] if os.path.exists(log_path) else "no log"
    print("WAKE_ERROR: 启动超时，日志尾部: " + tail, flush=True)
"""

        def _on_kid(kid):
            target_session.kernel_id = kid
            _remember_kernel(target_session)

        def _on_sid(sid):
            target_session.session_id = sid
            _remember_kernel(target_session)

        def _run_boot_on_runtime(use_saved_kernel: bool):
            rt = ColabRuntime(
                target_session.url,
                target_session.token,
                session_name=target_session.name,
                history=state.history,
                kernel_id=target_session.kernel_id if use_saved_kernel else None,
                session_id=target_session.session_id if use_saved_kernel else None,
                on_kernel_started=_on_kid,
                on_session_started=_on_sid,
            )
            try:
                # Each retry gets its own single-use file. The password never
                # enters the execute_request source or the streamed wake log.
                stage = ".easycolab-management-" + uuid.uuid4().hex
                with tempfile.TemporaryDirectory() as directory:
                    password_file = os.path.join(directory, "management")
                    with open(password_file, "w", encoding="ascii") as stream:
                        stream.write(password)
                    contents.upload(password_file, "content/" + stage)
                code = (
                    "ADMIN_STAGE = " + repr("/content/" + stage) + "\n"
                    + "INITIAL_IDLE_MINUTES = " + repr(initial_idle_minutes) + "\n"
                    + boot_code
                )
                return rt.execute_code(code, allow_stdin=False, timeout=300)
            finally:
                try:
                    rt.stop()
                except Exception:
                    pass

        try:
            outputs = _run_boot_on_runtime(use_saved_kernel=True)
        except Exception:
            target_session.kernel_id = None
            target_session.session_id = None
            outputs = _run_boot_on_runtime(use_saved_kernel=False)

        for out in outputs:
            if "text" in out:
                for line in out["text"].splitlines():
                    print(line, flush=True)
            elif out.get("output_type") == "error":
                print("WAKE_ERROR: " + out.get("evalue", "Execution error"), flush=True)

    except Exception as e:
        print(f"WAKE_ERROR: {str(e)}", flush=True)


class _ProxyAuthExpiredError(RuntimeError):
    """Runtime Proxy Token 已过期或失效 (HTTP 401/403)，需立即通过 list_assignments 刷新"""


def _session_token_ttl_seconds(s: SessionState) -> float:
    exp = getattr(s, "token_expires_at", None)
    if exp is None:
        return 3600.0
    try:
        if isinstance(exp, (int, float)):
            return float(exp) - time.time()
        if hasattr(exp, "timestamp"):
            return float(exp.timestamp()) - time.time()
        if isinstance(exp, str) and exp.strip():
            dt = datetime.fromisoformat(exp.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() - time.time()
    except Exception:
        pass
    return 3600.0


def _touch_colab_tunnel_keepalive(endpoint: str) -> bool:
    if not endpoint:
        return False
    try:
        keepalive_url = f"{state.client.colab_domain.rstrip('/')}/tun/m/{endpoint}/keep-alive/"
        state.client._issue_request(
            keepalive_url,
            method="GET",
            headers={"X-Colab-Tunnel": "Google"},
        )
        return True
    except Exception:
        return False


def _resolve_or_start_remote_kernel(s: SessionState, timeout_s: float = 12.0) -> str:
    """
    通过 Colab 官方 Runtime Proxy 查询或启动远端 Jupyter 内核。
    严禁删除其他已有内核，避免误杀并发调试或子任务内核。
    """
    base_url = (s.url or "").rstrip("/")
    if not base_url or not s.token:
        raise RuntimeError("Session missing runtime proxy URL or token")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Colab-Client-Agent": "vscode",
        "X-Colab-Runtime-Proxy-Token": s.token,
        "Authorization": f"Bearer {s.token}",
    }
    params = {"authuser": "0", "colab-runtime-proxy-token": s.token}

    r = requests.get(f"{base_url}/api/kernels", headers=headers, params=params, timeout=timeout_s)
    if r.status_code in (401, 403):
        raise _ProxyAuthExpiredError(f"Runtime proxy HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"GET /api/kernels returned HTTP {r.status_code}")

    kernels = r.json() if r.text else []
    active_kid = None
    if isinstance(kernels, list) and kernels:
        known_ids = [str(k.get("id")) for k in kernels if isinstance(k, dict) and k.get("id")]
        if known_ids:
            active_kid = s.kernel_id if s.kernel_id in known_ids else None

    if not active_kid:
        pr = requests.post(
            f"{base_url}/api/kernels",
            headers=headers,
            params=params,
            json={"name": "python3"},
            timeout=timeout_s,
        )
        if pr.status_code in (401, 403):
            raise _ProxyAuthExpiredError(f"POST /api/kernels HTTP {pr.status_code}")
        if pr.status_code not in (200, 201):
            raise RuntimeError(f"POST /api/kernels returned HTTP {pr.status_code}")
        pdata = pr.json() if pr.text else {}
        active_kid = str(pdata.get("id") or "")

    if not active_kid:
        raise RuntimeError("Unable to obtain remote Jupyter kernel_id")

    changed = False
    if s.kernel_id != active_kid:
        s.kernel_id = active_kid
        changed = True
    if not s.session_id:
        s.session_id = uuid.uuid4().hex
        changed = True
    if changed:
        _remember_kernel(s)
    return active_kid


def _open_colab_proxy_ws(s: SessionState, kernel_id: str, timeout_s: float = 15.0) -> websocket.WebSocket:
    """
    建立直连 Google Colab 官方外部 Runtime Proxy (*.prod.r.colab.dev) 的常驻 WebSocket 通道，
    显式携带 colab-runtime-proxy-token、token、authuser=0 查询参数与认证请求头，
    确保 Google 外部代理层始终观测到活跃 WebSocket 连接 (connections >= 1)。
    """
    base_url = (s.url or "").rstrip("/")
    if base_url.startswith("https://"):
        ws_base = "wss://" + base_url[len("https://"):]
    elif base_url.startswith("http://"):
        ws_base = "ws://" + base_url[len("http://"):]
    else:
        ws_base = "wss://" + base_url

    sid = s.session_id or uuid.uuid4().hex
    qs = urllib.parse.urlencode({
        "session_id": sid,
        "token": s.token,
        "colab-runtime-proxy-token": s.token,
        "authuser": "0",
    })
    ws_url = f"{ws_base}/api/kernels/{kernel_id}/channels?{qs}"
    headers = [
        f"X-Colab-Runtime-Proxy-Token: {s.token}",
        f"Authorization: Bearer {s.token}",
        "X-Colab-Client-Agent: vscode",
        "User-Agent: EasyColab/2.0",
    ]
    return websocket.create_connection(
        ws_url,
        timeout=timeout_s,
        origin=base_url,
        header=headers,
        enable_multithread=True,
    )


def _drain_proxy_ws_frames(ws: websocket.WebSocket, wait_s: float = 1.0, stop_on_msg_id: str = "") -> bool:
    """排空服务端下发的 Jupyter 消息与控制帧（自动回复 Ping -> Pong，遇到 Close 立即返回 False）"""
    deadline = time.time() + max(0.05, wait_s)
    while True:
        rem = deadline - time.time()
        if rem <= 0:
            return True
        try:
            ws.settimeout(min(rem, 1.0))
            opcode, frame = ws.recv_data(control_frame=True)
            if opcode == ABNF.OPCODE_CLOSE:
                return False
            if stop_on_msg_id and opcode == ABNF.OPCODE_TEXT and frame:
                raw_txt = frame.decode("utf-8", errors="replace") if isinstance(frame, (bytes, bytearray)) else str(frame)
                if stop_on_msg_id in raw_txt and '"execute_reply"' in raw_txt:
                    return True
        except websocket.WebSocketTimeoutException:
            return True
        except Exception:
            return False


def _send_proxy_ws_heartbeat(ws: websocket.WebSocket, session_id: str, touch_client_hb: bool = False) -> bool:
    """在已打开的外部 Runtime Proxy WebSocket 上发送 Ping 与 Jupyter execute_request，刷新代理与内核活跃度"""
    msg_id = uuid.uuid4().hex
    hb_file = "/tmp/.easycolab_client_hb" if touch_client_hb else "/tmp/.easycolab_proxy_hb"
    code = f"import pathlib; pathlib.Path('{hb_file}').touch(); _easycolab_hb = 1"
    msg = {
        "channel": "shell",
        "header": {
            "msg_id": msg_id,
            "username": "easycolab",
            "session": session_id,
            "date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "msg_type": "execute_request",
            "version": "5.3",
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": False,
            "store_history": False,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": False,
        },
    }
    try:
        ws.settimeout(10.0)
        ws.ping("easycolab")
        ws.send(json.dumps(msg))
        return _drain_proxy_ws_frames(ws, wait_s=2.0, stop_on_msg_id=msg_id)
    except Exception:
        return False


def cmd_keepalive():
    """
    单次向活跃的 Colab Headless 实例发送官方 Runtime Proxy 探活与轻量 Jupyter 内核脉冲（兼容 CLI / Windows / Linux 调用）。
    macOS 客户端优先使用长连接常驻子进程 keepalive_daemon。
    """
    if not _has_valid_token():
        print(json.dumps({"ok": True, "logged_in": False}))
        return

    try:
        local_sessions = _owned_sessions()
        touched = 0
        warnings = []

        for s in list(local_sessions.values()):
            _touch_colab_tunnel_keepalive(s.endpoint)
            if not s.url or not s.token:
                continue

            ws = None
            try:
                active_kid = _resolve_or_start_remote_kernel(s, timeout_s=12.0)
                ws = _open_colab_proxy_ws(s, active_kid, timeout_s=15.0)
                if _send_proxy_ws_heartbeat(ws, s.session_id or uuid.uuid4().hex, touch_client_hb=True):
                    touched += 1
                else:
                    warnings.append(f"{s.name}: heartbeat closed")
            except Exception as touch_err:
                warnings.append(f"{s.name}: {touch_err}")
            finally:
                if ws is not None:
                    try:
                        ws.close(timeout=1)
                    except Exception:
                        pass

        info = state.client.get_consumption_user_info()
        payload = {
            "ok": True,
            "logged_in": True,
            "touched_kernels": touched,
            "paid_balance": round(float(info.paid_compute_units_balance), 2),
            "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
            "active_assignments": int(info.assignments_count),
        }
        if warnings:
            payload["warning"] = "; ".join(warnings)
        print(json.dumps(payload))
    except Exception as e:
        print(json.dumps({"ok": False, "logged_in": True, "error": str(e)}))


def cmd_keepalive_daemon():
    """
    macOS 原生客户端专属常驻保活守护进程：
    - 始终保持一条直连 Google Colab 官方外部 Runtime Proxy (wss://*.prod.r.colab.dev/api/kernels/<id>/channels)
      的真实 WebSocket 长连接，使代理层与 Jupyter 内核的 connections 持续 >= 1（杜绝无浏览器模式下 ~20 分钟孤儿实例强杀）；
    - 每 25 秒在长连接上发送一次 WebSocket Ping + Jupyter execute_request 脉冲；
    - 每 45 秒调用 /tun/m/<endpoint>/keep-alive/ 并拉取实时 CU 余额/速率，通过 KEEPALIVE_JSON: 行流式推送给客户端；
    - 在代理 Token 临近过期 (<10 分钟) 或连接断开时自动刷新 runtimeProxyInfo 并无缝重连；
    - 监听父进程 stdin 管道 EOF 与进程生命周期：一旦 EasyColab.app 断开实例或退出，立即关闭 WebSocket 并零残留退出。
    """
    stop_event = threading.Event()
    initial_ppid = os.getppid()

    def _handle_sig(_signum, _frame):
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig_obj = getattr(signal, sig_name, None)
        if sig_obj is not None:
            try:
                signal.signal(sig_obj, _handle_sig)
            except Exception:
                pass

    if not sys.stdin.isatty():
        def _watch_stdin_eof():
            try:
                while not stop_event.is_set():
                    data = sys.stdin.read(1)
                    if data == "":
                        stop_event.set()
                        break
            except Exception:
                stop_event.set()

        threading.Thread(target=_watch_stdin_eof, daemon=True).start()

    def _emit(obj: dict):
        try:
            print("KEEPALIVE_JSON:" + json.dumps(obj, ensure_ascii=False), flush=True)
        except Exception:
            stop_event.set()

    if not _has_valid_token():
        _emit({"ok": True, "logged_in": False, "ws_connected": False})
        return

    ws_map: dict[str, tuple[websocket.WebSocket, str, str]] = {}  # endpoint -> (ws, url, token)

    def _close_ws(ep: str):
        entry = ws_map.pop(ep, None)
        if entry is not None:
            try:
                entry[0].close(timeout=1)
            except Exception:
                pass

    def _close_all_ws():
        for ep in list(ws_map.keys()):
            _close_ws(ep)

    last_sync_ts = 0.0
    last_hb_ts = 0.0
    last_usage_ts = 0.0
    force_resync = True
    local_sessions: dict[str, SessionState] = {}

    try:
        while not stop_event.is_set():
            if os.name != "nt" and os.getppid() != initial_ppid:
                stop_event.set()
                break

            now = time.time()
            min_ttl = min((_session_token_ttl_seconds(s) for s in local_sessions.values()), default=3600.0)
            if force_resync or (now - last_sync_ts) >= 480.0 or min_ttl < 600.0:
                try:
                    local_sessions = _owned_sessions()
                    last_sync_ts = time.time()
                    force_resync = False
                    active_eps = {s.endpoint for s in local_sessions.values() if s.endpoint}
                    for stale_ep in list(ws_map.keys()):
                        if stale_ep not in active_eps:
                            _close_ws(stale_ep)
                except Exception as sync_err:
                    _close_all_ws()
                    local_sessions = {}
                    _emit({"ok": False, "logged_in": True, "ws_connected": False, "warning": f"sync: {sync_err}"})

            if not local_sessions:
                if (now - last_usage_ts) >= 30.0:
                    try:
                        info = state.client.get_consumption_user_info()
                        last_usage_ts = time.time()
                        _emit({
                            "ok": True,
                            "logged_in": True,
                            "ws_connected": False,
                            "paid_balance": round(float(info.paid_compute_units_balance), 2),
                            "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
                            "active_assignments": int(info.assignments_count),
                        })
                    except Exception:
                        pass
                stop_event.wait(6.0)
                force_resync = True
                continue

            # 确保每个活跃会话都持有常驻外部 Runtime Proxy WebSocket
            need_hb = (time.time() - last_hb_ts) >= 25.0
            any_ws_ok = False
            last_warn = ""

            for s in list(local_sessions.values()):
                if stop_event.is_set():
                    break
                ep = s.endpoint or s.name
                existing = ws_map.get(ep)
                if existing is not None:
                    old_ws, old_url, old_tok = existing
                    if not getattr(old_ws, "connected", False) or old_url != s.url or old_tok != s.token:
                        _close_ws(ep)
                        existing = None

                if existing is None:
                    try:
                        kid = _resolve_or_start_remote_kernel(s, timeout_s=12.0)
                        new_ws = _open_colab_proxy_ws(s, kid, timeout_s=15.0)
                        if _send_proxy_ws_heartbeat(new_ws, s.session_id or uuid.uuid4().hex, touch_client_hb=False):
                            ws_map[ep] = (new_ws, s.url, s.token)
                            any_ws_ok = True
                            last_hb_ts = time.time()
                        else:
                            try:
                                new_ws.close(timeout=1)
                            except Exception:
                                pass
                            force_resync = True
                    except _ProxyAuthExpiredError as auth_err:
                        last_warn = str(auth_err)
                        force_resync = True
                    except Exception as conn_err:
                        last_warn = str(conn_err)
                        force_resync = True
                else:
                    cur_ws = existing[0]
                    if need_hb:
                        if _send_proxy_ws_heartbeat(cur_ws, s.session_id or uuid.uuid4().hex, touch_client_hb=False):
                            any_ws_ok = True
                        else:
                            _close_ws(ep)
                            force_resync = True
                            last_warn = "ws heartbeat lost, reconnecting"
                    else:
                        if _drain_proxy_ws_frames(cur_ws, wait_s=1.0):
                            any_ws_ok = True
                        else:
                            _close_ws(ep)
                            force_resync = True
                            last_warn = "ws closed by peer, reconnecting"

            if need_hb and any_ws_ok:
                last_hb_ts = time.time()

            now2 = time.time()
            if (now2 - last_usage_ts) >= 45.0 or (last_usage_ts == 0.0):
                for s in list(local_sessions.values()):
                    _touch_colab_tunnel_keepalive(s.endpoint)
                try:
                    info = state.client.get_consumption_user_info()
                    last_usage_ts = now2
                    payload = {
                        "ok": True,
                        "logged_in": True,
                        "ws_connected": any_ws_ok,
                        "paid_balance": round(float(info.paid_compute_units_balance), 2),
                        "burn_rate_hourly": round(float(info.consumption_rate_hourly), 2),
                        "active_assignments": int(info.assignments_count),
                    }
                    if last_warn:
                        payload["warning"] = last_warn
                    _emit(payload)
                except Exception as usage_err:
                    last_usage_ts = now2
                    _emit({"ok": False, "logged_in": True, "ws_connected": any_ws_ok, "warning": str(usage_err)})

            if not any_ws_ok and not stop_event.is_set():
                stop_event.wait(3.0)
            elif need_hb and not stop_event.is_set():
                stop_event.wait(1.0)
    finally:
        _close_all_ws()


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth_status")
    sub.add_parser("auth_url")
    sub.add_parser("auth_logout")

    p_code = sub.add_parser("auth_code")
    p_code.add_argument("code", type=str)

    p_wake = sub.add_parser("wake")
    p_wake.add_argument("--gpu", type=str, default="G4")
    p_wake.add_argument(
        "--app-py",
        type=str,
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"),
    )

    sub.add_parser("keepalive")
    sub.add_parser("keepalive_daemon")
    sub.add_parser("stop_all")

    args = parser.parse_args(argv)
    if args.cmd == "auth_status":
        cmd_auth_status()
    elif args.cmd == "auth_url":
        cmd_auth_url()
    elif args.cmd == "auth_logout":
        cmd_auth_logout()
    elif args.cmd == "auth_code":
        cmd_auth_code(args.code)
    elif args.cmd == "wake":
        cmd_wake(args.gpu, args.app_py)
    elif args.cmd == "keepalive":
        cmd_keepalive()
    elif args.cmd == "keepalive_daemon":
        cmd_keepalive_daemon()
    elif args.cmd == "stop_all":
        cmd_stop_all()


def run_cli(argv=None):
    """供独立 CLI 或打包后的 Windows exe (--bridge-cli) 子进程模式统一调用"""
    command = (sys.argv[1:] if argv is None else argv)[:1]
    status = 0
    try:
        main(argv)
    except BridgeAuthError as error:
        payload = {"ok": False, "error_code": error.code, "error": str(error)}
        if command == ["wake"]:
            print("WAKE_ERROR: " + str(error))
        elif command == ["keepalive_daemon"]:
            print("KEEPALIVE_JSON:" + json.dumps(payload))
        else:
            print(json.dumps(payload))
        status = 1
    except SystemExit as error:
        status = error.code if isinstance(error.code, int) else 1
    except Exception:
        print(json.dumps({"ok": False, "error": "The Colab bridge could not complete the command."}))
        status = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(status)


if __name__ == "__main__":
    run_cli()
