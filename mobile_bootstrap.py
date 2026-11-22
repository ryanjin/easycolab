"""Start the authenticated EasyColab service inside a mobile-provisioned Colab runtime."""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


LEGACY_STAGE = Path("/content/.easycolab_rclone.conf")
RCLONE_CONFIG = Path("~/.config/rclone/rclone.conf").expanduser()
MOUNTINFO = Path("/proc/self/mountinfo")


def _check_legacy_drive():
    message = "检测到旧版 EasyColab 云盘凭据或挂载，请撤销旧授权并使用新实例。"
    if os.path.lexists(LEGACY_STAGE):
        raise RuntimeError(message)
    if RCLONE_CONFIG.is_file():
        with RCLONE_CONFIG.open(encoding="utf-8", errors="replace") as config:
            if any(line.strip().lower() == "[easycolab_gdrive]" for line in config):
                raise RuntimeError(message)
    with MOUNTINFO.open(encoding="utf-8") as mounts:
        if any(" - fuse.rclone easycolab_gdrive:" in line for line in mounts):
            raise RuntimeError(message)


def start(password_file):
    stage = Path(password_file)
    try:
        password = stage.read_text(encoding="ascii")
    finally:
        stage.unlink(missing_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", password):
        raise ValueError("A management password is required")
    _check_legacy_drive()
    if not shutil.which("nginx"):
        subprocess.run(["apt-get", "update", "-qq"], check=True)
        subprocess.run(["apt-get", "install", "-y", "-qq", "nginx-light", "openssl"], check=True)
        subprocess.run(["service", "nginx", "stop"], check=False)

    if not os.path.exists("/content/DiffSynth-Studio"):
        print("正在克隆 DiffSynth-Studio...", flush=True)
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/modelscope/DiffSynth-Studio.git", "/content/DiffSynth-Studio"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "transformers", "accelerate", "bitsandbytes", "huggingface_hub", "hf_transfer", "qwen-vl-utils", "einops", "kernels>=0.17.2,<0.18.0", "av", "gradio", "modelscope", "imageio", "imageio-ffmpeg", "scipy", "soundfile", "librosa"], check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "fla-core", "flash-linear-attention"], check=False)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--only-binary=:all:", "causal-conv1d"], check=False)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", "/content/DiffSynth-Studio"], check=True)
    else:
        try:
            from fla.ops.gated_delta_rule import chunk_gated_delta_rule
            import kernels
        except Exception:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "einops", "kernels>=0.17.2,<0.18.0"], check=False)
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", "fla-core", "flash-linear-attention"], check=False)

    subprocess.run(["pkill", "-f", "python.*/content/app.py"], check=False)
    subprocess.run(["pkill", "-f", "llama-server"], check=False)
    time.sleep(1)

    log_path = "/content/easycolab_server.log"
    if os.path.exists(log_path):
        os.remove(log_path)

    with open(log_path, "w") as out_f:
        subprocess.Popen([sys.executable, "-u", "/content/app.py"], cwd="/content", stdout=out_f, stderr=subprocess.STDOUT, env={**os.environ, "EASYCOLAB_ADMIN_PASSWORD": password})

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
