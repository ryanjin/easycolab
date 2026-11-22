# EasyColab

**简体中文** | [English](./README_EN.md)

**EasyColab** 是一款专为 **macOS、Windows、Linux、iOS / iPadOS 与 Android** 原生打造的 Google Colab 云端大显存多模态 AI 创作工作台（macOS SwiftUI 原生客户端 + Windows / Linux PySide6/Qt6 桌面客户端 + iOS/iPadOS 纯 SwiftUI 原生客户端 + Android 纯 Kotlin Jetpack Compose 原生客户端 + 云端多模型加速引擎）。

默认适配 **Google Colab G4（NVIDIA RTX PRO 6000 Blackwell · `96 GB` 显存 + `176 GB` 系统内存）** 以及 `H100 / A100 / L4` 实例，支持免开网页一键唤醒部署、Google Drive 云盘权重永久复用、多模型显存共驻、实时算力折线图监控、中英文双语界面一键切换与闲置自动释放省额度。

![EasyColab 工作台界面预览](./shot.png)

### 使用经过鉴权的公网入口

点击「唤醒并部署」时，客户端会生成管理密码。连接手动启动的 Notebook 时，请在隐藏输入框设置 24 至 256 位密码（英文字母、数字、下划线或连字符），然后在客户端填写 Notebook 输出的 **HTTPS 服务地址和同一密码**。在浏览器打开 Gradio 或 ComfyUI 时，以 `easycolab` 为用户名登录。OpenAI 和 Anthropic 兼容接口使用「API 网关」中的独立推理密钥。Linux 服务端使用 nginx 和 OpenSSL 保护两个公网入口，直接运行 `app.py` 时须安装这两个程序。

如果手动重用同一台 VM，旧版 ComfyUI 占用端口 `8188` 时，请先停止该进程再启动新版服务。之后可在同一台 VM 上重新部署并接回已有的 EasyColab 画布。

**已有实例与升级。** 客户端只会自动接管、保活或释放本次安装创建、且已向 Google 核实归属的实例。「释放实例」后显示的余额和计费速率涵盖整个 Google 账号。旧版创建的实例缺少新版归属记录，其他设备和 Notebook 的实例也可能继续计费。请在 Colab 网页端查看这些会话，在完成工作后手动释放。

**Drive 与生成文件。** 需要持久保存模型、ComfyUI 节点或作品时，请在官方 Notebook 中授权并使用 Colab 原生 Drive 挂载。免网页部署将 Google OAuth 刷新令牌留在设备上。未挂载 Drive 时，服务端文件位于临时 VM 磁盘，客户端仍可下载生成结果。升级前若曾启用旧版 EasyColab 的 rclone 云盘挂载，请先保存需要保留的文件，再撤销旧的 Google 授权并换用新 VM。新版客户端在部署时检测到旧版凭据配置或挂载会停止启动。

---

## 核心特性

- **全平台桌面级创作体验 + 中英文双语切换 + 新手交互引导（macOS / Windows / Linux 1:1 全功能对齐）**
  - **macOS 端**采用纯原生 SwiftUI + AppKit 构建，**Windows 与 Linux 端**采用 PySide6 (Qt6 + FFmpeg 多媒体管线) 构建并适配高分屏缩放；首次启动均自带 **4 步交互式新手引导**，零配置自动初始化连接组件。
  - 支持 **中英文（简体中文 / English）界面实时一键切换**（默认显示中文），状态与偏好自动记忆，无需重启应用。
  - 实时绘制云端 **GPU 显存 / 系统内存 / 磁盘空间** 平滑曲线走势图（支持分项与同屏叠加模式），同步显示官方真实剩余 **CU 计算单元** 与每小时消耗速率。
  - 内置 **30 分钟闲置自动休眠看门狗** 与一键「断开并释放实例」，杜绝忘记关机空耗算力额度。

- **四大旗舰开源多模态引擎开箱即用**
  1. **`MiniMax-H3 (BF16 原生满血)` · 4～15 秒电影级有声视频**
     - 支持文生有声视频、首帧图生视频、首尾帧过渡控镜，原生输出 `24 FPS` 画面 + `32 kHz` 立体声音轨。
     - **G4 176GB 双主干内存热备加速**：自动将 `32B 文本编码器` 与 `33B DiT 主干` 常驻高速 DDR5 内存，同提示词连抽 `0 ms` 瞬回，修改新提示词整块 PCIe 换入解析仅需约 `8～12 秒`。
  2. **`ComfyUI 节点工作台` · 云盘持久化 + 4 套零基础预置工作流包**
     - 一键部署 ComfyUI + Manager 并开通免费 Cloudflare HTTPS 加密隧道，挂载原生 Google Drive 后，`custom_nodes`、`models` 与 `workflows` 可自动软链或同步到云盘。
     - ComfyUI-Manager 启动时会将旧版 EasyColab 设置的 `security_level = weak` 恢复为 `normal`。公网画布中的节点安装遵循 Manager 的安全级别。
     - 内置 **万相 Wan2.2 14B**、**MiniMax-H3 Turbo**、**LTX-2.3 (22B)**、**混元 HunyuanVideo 1.5** 四套「一键配齐模型 + 节点指引」的图生视频与首尾帧运镜工作流。
  3. **`Qwen3.8-27B (UD-Q4_K_M + MTP 投机解码 / BF16)` · 100 tok/s 视觉对话与双协议网关**
     - 支持最高 `256K` 上下文、长思维链推理、多模态看图反推提示词。
     - 内置兼容 **OpenAI (`/v1/chat/completions`)** 与 **Anthropic (`/v1/messages`)** 的公网 API 网关（支持 Bearer / `x-api-key` 鉴权），可直接接入 **Claude Code**、**Cursor** 等开发工具。
  4. **`Qwen-Image-2.1` & `Z-Image-Turbo` · 2K 海报生图与多图指令修图**
     - 支持高质感中英文排版直出、双图自然语言指令修图与 8 步极速出图；所有生成的 MP4 / PNG 自动回传至本地目录（macOS: `~/Movies/EasyColab`，Windows: `%USERPROFILE%\Videos\EasyColab`，Linux: `~/Videos/EasyColab`）。

---

## 快速开始

### 系统要求
- **操作系统**：
  - **macOS**：macOS 13.0 (Ventura) 或更高版本（推荐 Apple Silicon / macOS Sonoma & Sequoia）
  - **Windows**：Windows 10 (1903+) 或 Windows 11 64 位 (x64)
  - **Linux**：主流 64 位桌面发行版（Ubuntu 22.04+ / Debian 12+ / Arch Linux / Fedora 等，需 Python 3.12+ 与 FFmpeg）
  - **iOS / iPadOS**：iOS 16.0 或更高版本（支持 Mac 本地源码真机签名安装或通过 AltStore / SideStore / Sideloadly / TrollStore 侧载 `.ipa`）
  - **Android**：Android 8.0 (API 26) 或更高版本（支持从 GitHub Releases 下载签名 `.apk` 直接安装，或从 `android_app/` 源码一键编译）
- **云端账号**：开通了 **Google Colab Pro / Pro+ / Google AI Ultra**（拥有可用 CU 计算单元）的 Google 账号；免费版用户请使用下方「Google Colab 官方网页端运行 `.ipynb`」。

---

### macOS 客户端

#### 方式一：本地一行命令编译运行（推荐，无 Gatekeeper 拦截）
编译需要 Xcode Command Line Tools（可通过 `xcode-select --install` 安装）。免网页绑定和部署另需 Python 3.12 或更高版本。

```bash
git clone https://github.com/wy51ai/easycolab.git
cd easycolab
bash macos_app/build_macos_app.sh
open EasyColab.app
```

> 编译脚本会自动将 `app.py`、`colab_bridge.py`、`EasyColab_Server.ipynb` 打包进 `EasyColab.app/Contents/Resources/`。你可以将生成的 `EasyColab.app` 直接拖入 `/Applications` 独立使用；首次点击「绑定」时，应用会在 `~/Library/Application Support/EasyColab/venv` 自动创建隔离 Python 环境并安装连接组件。

#### 方式二：从 GitHub Releases 下载预编译 `.app`
若下载的是未经过 Apple Developer ID 公证的预编译 `.app`，首次打开若遇到 macOS 提示 *“无法验证开发者 / 已损坏”*，请在终端执行一次解除隔离命令：

```bash
xattr -cr /Applications/EasyColab.app
```

---

### Windows 客户端

#### 方式一：下载免安装绿色版 `.zip`（零 Python 依赖，推荐）
1. 从 GitHub Releases（或仓库 **Actions -> Build EasyColab Windows App** 构建产物）下载 `EasyColab-Windows-x64.zip`。
2. 解压到任意目录后，直接双击运行 `EasyColab\EasyColab.exe` 即可使用（已内置 Python 运行时、Qt6 界面库、FFmpeg 音视频解码器与 `colab_bridge` 云端唤醒组件，无需在电脑上安装 Python）。
3. 如遇 Windows SmartScreen 提示 *“Windows 已保护你的电脑”*，点击 **「更多信息」 -> 「仍要运行」** 即可。

#### 方式二：Windows 本地一键打包独立 `.exe`
若本机已安装 Python 3.12～3.13（64 位），在仓库根目录可直接一键创建隔离虚拟环境并编译生成 `dist\EasyColab\EasyColab.exe` 与 `EasyColab-Windows-x64.zip`：

```bat
REM 双击或在 CMD 中运行：
windows_app\build_windows_app.bat
```

或使用 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\windows_app\build_windows_app.ps1
```

#### 方式三：在 macOS 上直接交叉打包 Windows 独立 `.exe`
若当前在 macOS 开发机上（已安装 `brew install mingw-w64`），无需 Windows 虚拟机即可直接交叉编译原生 Win32 GUI 启动器并组装完整的 `EasyColab-Windows-x64.zip`：

```bash
python3 windows_app/build_windows_from_mac.py
```

#### 方式四：源码开发模式直接运行（Windows / Linux / macOS 通用预览）

```bash
python -m venv .venv
# Windows:       .venv\Scripts\activate
# Linux / macOS: source .venv/bin/activate
pip install -r windows_app/requirements-windows.txt
python windows_app/easycolab_win.py
```

---

### Linux 客户端

只需一键执行仓库根目录下的 `run_linux.sh`（首次运行会自动创建 `.venv` 隔离环境并安装 PySide6 与 Colab 桥接依赖）：

```bash
git clone https://github.com/wy51ai/easycolab.git
cd easycolab
./run_linux.sh
```

- 本地配置自动保存在 `~/.config/EasyColab/settings.json`，生成的视频与图片默认保存在 `~/Videos/EasyColab`（优先遵循 `XDG_VIDEOS_DIR`）。
- 支持自动读取环境变量代理（`HTTP_PROXY` / `HTTPS_PROXY`）以及 GNOME 系统网络代理（`gsettings`）。

---

### iOS / iPadOS 客户端（开源源码自编译 & 侧载 `.ipa`）

iOS / iPadOS 端采用纯原生 SwiftUI 构建（源码位于 [`ios_app/`](./ios_app)），内置零 Python 依赖的纯 Swift 云端唤醒桥接引擎，支持一键 Google OAuth 授权唤醒 G4 (96GB) 实例、锁屏/切后台推理进度自动同步、高清有声 MP4 与 PNG 自动存入系统相册、自定义 HuggingFace 仓库导入以及云端闲置自动休眠保护。

<p align="center">
  <img src="./app_1.PNG" alt="EasyColab 移动端创作工作台与 ComfyUI 预设工作流包" width="45%" />
  &nbsp;&nbsp;&nbsp;
  <img src="./app_2.PNG" alt="EasyColab 移动端云算力中心与 G4 一键唤醒" width="45%" />
</p>

#### 方式一：Mac 本地一行命令编译并安装至 iPhone / iPad 真机（推荐）
支持**免费个人 Apple ID**（7 天自签）或付费 Apple Developer 账号（365 天签名）。将 iPhone / iPad 连接至安装了 Xcode 的 Mac（首次连接请在设备上开启「开发者模式」并信任电脑）：

```bash
# 自动生成临时隔离工程（位于被 Git 忽略的 build/ 目录，绝不写入或泄露你的 Team ID）并直装真机
bash ios_app/install_release_iphone.sh

# 或者在 Xcode 图形界面打开工程手动选择个人签名团队运行：
bash ios_app/install_release_iphone.sh --open-xcode
```

#### 方式二：通过侧载工具安装预编译 `EasyColab-iOS.ipa`
1. 从 **[GitHub Releases](../../releases)** 下载 `EasyColab-iOS.ipa`（或在 Mac 本地执行 `bash ios_app/build_ios_ipa.sh` 生成无开发者签名信息的纯净 `EasyColab-iOS.ipa`）。
2. 使用 **AltStore**、**SideStore**、**Sideloadly** 或 **TrollStore** 导入 `EasyColab-iOS.ipa` 自签安装即可。

#### 方式三：macOS 本地一键启动 iOS 模拟器预览

```bash
bash ios_app/run_ios_simulator.sh
```

---

### Android 客户端（独立安装包 `.apk` & 开源自编译）

Android 端采用纯原生 Kotlin + Jetpack Compose Material 3 构建（源码位于 [`android_app/`](./android_app)），内置零 Python 依赖的纯 Kotlin 云端唤醒与 Jupyter WebSocket 桥接引擎，支持一键 Google OAuth 授权跳回、免网页唤醒云端 G4 (96GB) 实例、高清有声 MP4 与 PNG 自动写入系统相册（`Movies/EasyColab` 与 `Pictures/EasyColab`）以及锁屏/切后台断点恢复。

#### 方式一：直接下载预编译 `.apk` 安装（推荐）
1. 前往本仓库 **[GitHub Releases](../../releases)** 页面下载最新签名安装包 **`EasyColab-Android.apk`**。
2. 在 Android 手机或平板（Android 8.0+）上直接点击安装即可使用。

#### 方式二：从源码本地一键编译签名 `.apk` 或直装手机
确保本机装有 JDK 17+ 与 Android SDK（如已安装 Android Studio 则自动识别，首次构建若无签名密钥会自动在被 Git 忽略的 `android_app/.signing/` 下生成本地证书）：

```bash
# 仅编译生成根目录 EasyColab-Android.apk
bash android_app/build_android_apk.sh

# 编译并通过 ADB 一键安装启动至已连接 USB / 无线调试的 Android 手机
bash android_app/install_release_android.sh
```

---

### Google Colab 官方网页端运行（适合 Colab 免费版 / 浏览器用户）
1. 打开 [Google Colab](https://colab.research.google.com/)，上传本仓库根目录下的 `EasyColab_Server.ipynb`（或在客户端引导页点击「导出 / 定位 `.ipynb` 笔记本」）。
2. 在 Colab 菜单栏选择 `代码执行程序 -> 更改运行时类型`，选择 `G4` / `A100` / `L4` GPU 并按顺序运行单元格。
3. 将输出的 `https://xxxx.gradio.live` 链接填入客户端的服务地址栏，再填写本次 Notebook 设置的管理密码并点击「连接」。

---

## 隐私与安全建议

- **本机凭据。** macOS 原生客户端把各服务地址的管理密码、推理密钥和 Hugging Face 令牌保存在「钥匙串」中。升级时，旧设置中的对应条目会在成功迁移后移除。桌面桥接使用的 `google-colab-cli` 0.8.0 将 Google OAuth 凭据保存在操作系统的凭据库中。0.7.5 使用仅当前用户可读写的令牌文件。iOS / iPadOS 使用钥匙串，Android 使用 Android Keystore 中的密钥加密保存应用凭据。
- **Windows 与 Linux。** Qt 客户端的管理密码、推理密钥以及填写的 Hugging Face 令牌以明文保存在用户的 `settings.json` 中。Windows 路径为 `%LOCALAPPDATA%\EasyColab\settings.json`，Linux 路径为 `~/.config/EasyColab/settings.json`。请把备份和访问权限限定在可信用户范围内。
- **模型目录查询。** 客户端查询已连接的 EasyColab 服务时，将 Hugging Face 令牌放入 HTTPS 请求头。向上游查询需要令牌的模型时，客户端和服务端只向 `https://huggingface.co` 提交认证。第三方镜像仅用于匿名目录查询。
- **桌面媒体下载。** 客户端只接受允许的媒体扩展名，并把文件保存在本地输出目录中。服务端返回的带目录成分的文件名会被拒绝。
- **分享屏幕。** 服务地址旁的打码开关可隐藏临时隧道地址和账号邮箱。管理密码仅供管理员使用，外部推理工具应使用网关推理密钥。

---

## 免责声明与合规说明 (Disclaimer)

- **非官方独立项目**：EasyColab 是社区开发的独立开源客户端与辅助工具，**与 Google LLC、Google Colab、MiniMax、阿里巴巴通义千问（Qwen）等官方机构无任何隶属、赞助或背书关系**。
- **平台服务条款（ToS）提示**：
  - 客户端内的「免网页一键唤醒」功能底层基于开源社区库 [`google-colab-cli`](https://pypi.org/project/google-colab-cli/) 与自购付费计算单元（Paid Compute Units）交互，**强烈建议仅在已订阅 Colab Pro / Pro+ / Google AI Ultra 的自有付费账号上合理自用**。
  - 请勿在 Google Colab 免费层（Free Tier）上滥用无网页后台连接，严禁用于加密货币挖矿、批量多号滥用或违规算力转售。使用第三方客户端连接云端运行时可能受平台策略变更影响，相关账号风险由使用者自行评估并承担；若追求 100% 官方网页交互体验，请使用仓库内置的 `EasyColab_Server.ipynb`。
- **模型与上游组件许可证**：
  - 本仓库**不包含也不分发任何模型权重或 GPL 源码**。所有模型权重均由用户在运行时从 Hugging Face 官方仓库自行下载，须遵守各模型原始许可证（如 *MiniMax-H3 Community License Agreement*、*Apache-2.0*、*Tongyi Qianwen License* 等）。
  - 云端 ComfyUI 工作台在用户个人虚拟机内按需拉取官方仓库并通过独立进程 HTTP API 通信，ComfyUI 本体遵循其 **GPL-3.0** 协议。

---

## 致谢 (Acknowledgements)

感谢以下优秀的开源项目与模型团队：
- [DiffSynth-Studio](https://github.com/modelscope/DiffSynth-Studio)
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) & [ComfyUI-Manager](https://github.com/ltdrdata/ComfyUI-Manager)
- [MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3)
- [Qwen](https://github.com/QwenLM) & [Unsloth](https://github.com/unslothai/unsloth)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
- [google-colab-cli](https://pypi.org/project/google-colab-cli/)

---

## License

本项目源代码基于 [Apache License 2.0](./LICENSE) 开源发布。
