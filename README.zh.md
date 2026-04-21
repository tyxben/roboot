# Roboot

![CI](https://github.com/tyxben/roboot/actions/workflows/test.yml/badge.svg)

> English: [README.md](README.md)

一个住在你 Mac 上的个人 AI agent hub：对话、语音、摄像头，以及对 iTerm2 里所有 Claude Code session 的直接控制 —— 在笔记本前、在同一 Wi-Fi 的手机上、或通过端到端加密的 relay 从世界任何地方都能用。

为那些同时开着一堆 Claude Code session、想有一个统一入口去观察和指挥它们的人准备。

![demo](docs/demo.gif)

## 环境要求

- **macOS** —— iTerm2 集成只支持 macOS
- **Python 3.11+**
- **[iTerm2](https://iterm2.com/)** 并启用 Python API：*iTerm2 → Settings → General → Magic → Enable Python API*
- 至少一个 LLM 的 API key（推荐 DeepSeek，便宜且工具调用稳定）

## 快速开始

一条命令大多数人就够了:

```bash
git clone https://github.com/tyxben/roboot.git && cd roboot
./scripts/setup.sh                    # 装依赖 + ffmpeg + 预热 Whisper
# 编辑 config.yaml: 填 providers.deepseek，可选 telegram.bot_token
python server.py                      # 打开 http://localhost:8765
```

脚本会检查 Python 版本、装 `telegram` extras（含语音 I/O）、`brew install` ffmpeg（如果没装）、把 `config.example.yaml` 拷成 `config.yaml`、预下载 Whisper 模型(这样首条语音秒响应)。幂等,重跑安全。参数: `--with=core|telegram|voice|vision|all`、`--no-prewarm`。

想手动装:

```bash
pip install -e .                      # 核心：Web 控制台 + 局域网 + relay
cp config.example.yaml config.yaml    # 编辑并填入你的 API key
python server.py                      # 打开 http://localhost:8765
```

WebSocket 连上后会收到欢迎消息。

### 可选 extras

`pyproject.toml` 里定义了四个可选包组，用 `pip install -e '.[<名字>,<名字>]'` 随意组合：

- `telegram` —— Telegram 机器人 + 语音输入（mlx-whisper）+ 语音输出（Edge TTS → OGG/Opus）
- `voice` —— 本地麦克风 STT + macOS `say` TTS（CLI `--voice` 模式；需要先 `brew install portaudio`）
- `vision` —— 摄像头 + 人脸识别（`look` / `enroll_face` 工具）
- `desktop` —— pywebview 独立桌面应用
- `all` —— 以上全部一次装好

### 启用 Telegram 语音

```bash
pip install -e '.[telegram]'           # 拉 mlx-whisper + SpeechRecognition
brew install ffmpeg                    # 语音回复编码成 OGG/Opus 要用
python -m adapters.stt prewarm         # 预先下载 ASR 模型（~3 GB，一次）
python -m adapters.telegram_bot        # 启动 bot
```

预热那一步可选但强烈推荐 —— 不跑的话你**第一条** Telegram 语音要等约 6 分钟等模型下完。装的时候跑一次，以后每次发语音都秒级响应。

Telegram 里能干什么:
- 发语音 → bot 用 Whisper 转写（`large-v3` 中文准确率约 96%）→ Agent 处理 → 3~4 秒回一条语音气泡
- `/voice` —— 从 10 个精选声音里选一个（中文男女声 + 东北话 + 陕西话 + 英文）
- 直接说 "换成女声" / "帮我截个屏" —— Agent 可以主动调用 `switch_tts_voice` / `screenshot` / `list_sessions` / `shell` 等工具，不用记斜杠命令

### 其他入口

```bash
python run.py                     # 纯键盘 CLI
python run.py --voice             # 本地麦克风 + macOS `say` TTS（需要 `.[voice]`）
chainlit run chainlit_app.py -w   # 备选 Chainlit UI
```

## 远程访问

三种远程方式。暴露任何一种之前请先读 [SECURITY.md](SECURITY.md) 里的威胁模型。

- **局域网（零配置）** —— 服务绑定 `0.0.0.0:8765`，启动时打印 QR code。同 Wi-Fi 下手机扫码即用。自签 TLS 证书，首次信任（TOFU）。
- **Telegram 机器人** —— 在 `config.yaml` 填 `telegram.bot_token`，跑 `python -m adapters.telegram_bot`。用 `telegram.allowed_users` 白名单控制访问。
- **Relay** —— 一个 Cloudflare Worker 在 daemon 和浏览器配对页之间转发 WebSocket。流量端到端加密（ECDH P-256 → HKDF → AES-GCM），relay 只看到密文信封。配对 token 每 30 分钟自动轮换，也能在本地控制台一键撤销。

## 架构

```
server.py (FastAPI)              <- 主入口
├── WebSocket /ws                <- 流式对话（LLM_CHUNK 事件）
├── REST /api/sessions/*         <- iTerm2 session 直接控制
├── REST /api/tts                <- Edge TTS（文本 -> mp3）
├── REST /api/relay-*            <- Relay 状态 / 刷新 / 撤销 / QR
├── REST /api/network-info       <- 本机 IP + QR
└── Static /static/console.html  <- 统一 Web 控制台

relay_client.py                  <- 连接 Cloudflare Worker relay
iterm_bridge.py                  <- 持久 iTerm2 Python API 连接
soul.md                          <- 可自改写的助手身份
config.yaml                      <- API keys + 供应商配置（gitignored）

text_utils.py                    <- 共享小工具（extract_spoken_text…）

tools/
├── shell.py                     <- 终端命令执行
├── claude_code.py               <- iTerm2 session 列表/读取/发送/创建
├── vision.py                    <- 摄像头 + 截屏 + 人脸识别
├── face_db.py                   <- 人脸编码存储（.faces/）
├── soul.py                      <- 自我修改 + 用户记忆
└── voice_switch.py              <- Agent 工具：切换 Telegram TTS 声音

adapters/
├── telegram_bot.py              <- 通过 Telegram 远程控制（含语音 I/O）
├── voice.py                     <- 本地麦克风 STT + macOS TTS（CLI --voice）
├── voice_prefs.py               <- 每个 Telegram 用户的 TTS 声音偏好
├── tts_streamer.py              <- Edge TTS 并行合成 OGG/Opus 语音条
├── keyboard.py                  <- 终端文字输入
└── stt/                         <- 可插拔语音识别后端
    ├── mlx.py                   <- mlx-whisper（Apple Silicon 默认）
    ├── google.py                <- speech_recognition → Google Web Speech
    └── noop.py                  <- backend: none

relay/                           <- Cloudflare Worker relay
├── src/index.ts                 <- Worker 入口、路由、限流
├── src/relay-session.ts         <- Durable Object：daemon↔client 会话管理
├── src/pair-page.ts             <- 浏览器配对页
└── wrangler.toml                <- Cloudflare 部署配置
```

更深入的架构说明（agent 框架、TTS 约定、soul 系统、E2EE 握手、流式协议）在 [CLAUDE.md](CLAUDE.md)。

## 添加一个工具

1. 新建 `tools/my_tool.py`
2. 用 `@arcana.tool(when_to_use=..., what_to_expect=...)` 装饰
3. 在 `server.py` 的 `ALL_TOOLS` 里导入并加进去

Arcana 负责注册，其他不用改。约定见 [CLAUDE.md](CLAUDE.md) 的 "Adding a New Tool" 一节。

## 配置

所有选项在 [`config.example.yaml`](config.example.yaml) 里逐项注释。助手还能通过 `soul` 工具改写 `soul.md` 的部分内容。

## 文档

- [docs/USAGE.zh.md](docs/USAGE.zh.md) —— 使用指南：快速开始、配置、各种入口、Claude Code 集成、记忆、自动升级、故障排查（[English](docs/USAGE.md)）
- [docs/REMOTE_VS_LOCAL.md](docs/REMOTE_VS_LOCAL.md) —— 本地 / LAN / Telegram / relay 的能力矩阵，以及和 Claude Code 自带远程的对比（中英文在同一文件）
- [SECURITY.md](SECURITY.md) —— 威胁模型、E2EE 信任链、新功能风险、pairing URL 泄露时的撤销流程
- [CLAUDE.md](CLAUDE.md) —— 贡献者说明：架构、流式协议、soul 系统、如何加工具

## 安全

想让 Roboot 开出 localhost 之前，先读 [SECURITY.md](SECURITY.md)。里面列了什么是被保护的、什么不是、已知的缺口、以及如何报告漏洞。

## 许可

[MIT](LICENSE) —— Copyright (c) 2026 tyxben.

## 致谢

- [Arcana](https://github.com/tyxben/arcana) —— agent 框架
- [DeepSeek](https://platform.deepseek.com) —— 默认 LLM 供应商
- [iTerm2](https://iterm2.com/) Python API —— 终端集成
- [Cloudflare Workers](https://workers.cloudflare.com/) + Durable Objects —— relay 基础设施
- [Edge TTS](https://github.com/rany2/edge-tts) —— 神经网络语音合成
