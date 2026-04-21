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

```bash
git clone https://github.com/tyxben/roboot.git && cd roboot
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2 "qrcode[pil]" cryptography
cp config.example.yaml config.yaml    # 编辑并填入你的 API key
python server.py                      # 打开 http://localhost:8765
```

WebSocket 连上后会收到欢迎消息。

### 其他入口

```bash
python run.py                     # 纯键盘 CLI
python run.py --voice             # 本地麦克风 + macOS `say` TTS
python -m adapters.telegram_bot   # Telegram 机器人（需要 telegram.bot_token）
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

tools/
├── shell.py                     <- 终端命令执行
├── claude_code.py               <- iTerm2 session 列表/读取/发送/创建
├── vision.py                    <- 摄像头 + 截屏 + 人脸识别
├── face_db.py                   <- 人脸编码存储（.faces/）
└── soul.py                      <- 自我修改 + 用户记忆

adapters/
├── telegram_bot.py              <- 通过 Telegram 远程控制
├── voice.py                     <- 本地麦克风 STT + macOS TTS
└── keyboard.py                  <- 终端文字输入

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
