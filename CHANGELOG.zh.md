_[English](CHANGELOG.md)_

# 更新日志

Roboot 的所有重要变更都记录在这里。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增
- **FileVault 未开启警告条**（`filevault_status.py`、`/api/filevault-status`）—— 本地控制台加载时调用 `fdesetup status` 探测磁盘加密状态；如果启动卷未加密，顶部显示红色 sticky 警告条。Roboot 的整套 at-rest 安全模型（明文的 `config.yaml` API keys、`.identity/daemon.ed25519.key`、`soul.md`、`.chat_history.db`）都是以"FileVault 开着"为前提，警告条把这个前提显式化。每个 tab 可 `sessionStorage` 关闭当次会话。
- **"擦除所有聊天"按钮**（`chat_store.wipe_all`、`POST /api/chat-history-wipe`）—— 移动访问面板里的一键隐私卫生按钮，清掉所有 session + message 行并 VACUUM 数据库，删掉的页从文件层面不可恢复。这是放弃 at-rest DB 加密（被判定为 theater，详见 `SECURITY.md`）后的轻量替代方案。
- **Soul 写入审查闸**（`soul_review.py`）—— 所有对 `soul.md` 的写入（`update_self` / `remember_user` / `add_note` / 自我反馈蒸馏）都走这条闸。通过 `ROBOOT_SOUL_REVIEW` 选模式：`off`（默认，向后兼容）、`log`（允许落盘但把 diff 审计到 `.soul/pending/`）、`confirm`（本地控制台 + 移动配对页弹窗，每次写入都要用户确认；超时或 diff 超过 2KB 直接拒绝）。堵死 prompt 注入的 agent 偷偷持久化恶意内容。
- **`CONTRIBUTING.md`** + `CONTRIBUTING.zh.md`、`.github/ISSUE_TEMPLATE/`（bug_report / feature_request / config.yml）、`.github/PULL_REQUEST_TEMPLATE.md` —— v0.4.0 之后的开源项目基础设施。
- **移动端 TTS 代理** —— relay pair-page 现在通过加密 WS（`tts_request` / `tts_audio` 帧）从 daemon 的 Edge TTS 拉 MP3，让移动端 JARVIS 音色和本地一致；daemon 不可达时回退到浏览器 `speechSynthesis`。共享助手 `tts_synth.synthesize_spoken()` 避免两条路分叉。

## [0.4.0] - 2026-04-22

仓库开源后的首个版本。本轮大改集中在 Telegram 语音 I/O、局域网安全、自升级链路、对话记忆四块。

### 新增
- **Telegram 语音 I/O** —— mlx-whisper STT + 并行 Edge TTS，合成为原生 OGG/Opus 语音气泡（`adapters/telegram_bot.py`、`adapters/tts_streamer.py`）。
- **可插拔 STT 后端** —— `mlx_whisper`（默认，Apple Silicon）/ `google`（Web Speech）/ `none`；通过 `voice.stt.backend` 选择（`adapters/stt/`）。
- **Telegram 每用户音色偏好** —— `/voice` 选择器或 `switch_tts_voice` 工具调用；持久化到 `.voice_prefs/prefs.json`。
- **Telegram `/help`** —— 可发现的命令列表。
- **一键安装脚本** —— `scripts/setup.sh`（优先用 `uv`，自动处理 ffmpeg + Whisper 模型预热）。
- **LAN API bearer-token 鉴权** —— `/api/*` 要求 `Authorization: Bearer <token>`，`/ws` 要求 `Sec-WebSocket-Protocol: bearer.<token>`；token 自动生成到 `.auth/lan_token`（0600），嵌入 LAN QR 二维码。
- **自签名 TLS 证书工具** —— `tools/generate_cert.py`（走 `cryptography` 的 ECDSA P-256）。
- **进程内自升级循环** —— 通过 `ROBOOT_AUTO_UPGRADE=1` 开启；每小时 fetch + 冒烟测试 + re-exec + 回滚。`ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1` 可打开签名 tag 闸。
- **对话记忆** —— Layer A 基于 `resume_session_id` 的 replay（daemon 端）；Layer B 周期性蒸馏写入 `soul.md`。
- **自我反馈蒸馏** —— 每 20 轮对话，agent 在 `soul.md ## 自我反馈` 中写入具体的自我反馈。
- **Claude Code session 主动通知** —— `session_watcher.py` 在 session 从 idle → waiting 时广播 `type:"notify"` 帧。
- **前端通知处理** —— `static/console.html` 右上角 toast + 历史抽屉；移动端 `pair-page.ts` 底部固定 toast + 震动 + 历史。
- **频道 + session 列表上下文注入** agent 系统提示（`tools/soul.py::build_personality`）。
- **GitHub Actions CI** —— push/PR 上跑 pytest。
- **用户向文档** —— `docs/USAGE.{zh,}.md`、`docs/REMOTE_VS_LOCAL.md`、`README.zh.md`；`SECURITY.md` 扩展了威胁模型和 E2EE 信任链。
- **`.allowed_signers`** —— 提交入库的 SSH 签名白名单，`git verify-tag` 开箱即用。

### 变更
- STT 模型改为启动时预热（首次转写更快）。
- `voice.tts_voice` 配置项端到端生效。
- README 简化，安装 quickstart 置顶。
- 提交 `uv.lock`，确保可复现安装。

### 安全
- LAN API 改为必须持有自动生成的 bearer token（默认拒绝）。
- `session_watcher.py` 加固：`TAIL_LINES` 20 → 10；`_sanitize_prompt_line()` 剥离 ANSI、HTML 转义、截断到 200 字（防止通知面板的 prompt 注入）。
- 自升级可要求已验证的签名 tag（`ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1`）。
- 本版本是**第一个带可验证签名 tag 的版本**（SSH 签名；本地 `.allowed_signers` 就位后 `git verify-tag v0.4.0` 会通过）。

### 修复
- 合并遗留的重复 `_active_ws_clients` 定义。
- 去掉 Telegram 转写结果回显（用户听 TTS，不需要再看到自己说的话）。
- CI：限制 `setuptools` 的 package discovery，避免误扫模块。

---

## [0.3.0] - 2026-04-19

### 安全
- **签名 relay 握手** —— daemon 持有长期 ed25519 身份密钥 `.identity/daemon.ed25519.key`；指纹嵌入配对 URL fragment（`#fp=…`，浏览器永远不会把 fragment 发给 relay）。浏览器在派生会话密钥前，验证 daemon 对 `daemon_pub ‖ client_pub ‖ client_id` 的签名。堵上了 v0.2.0 匿名 ECDH 的 MITM 缺口。
- **client_id 绑定** —— `client_id = SHA256(client_ephemeral_pub)[:16].hex`；持有 token 的客户端不再能冒充其他客户端的 slot。

### 不兼容
- 协议层：daemon 和 Cloudflare Worker 必须同时重新部署。
- 浏览器依赖：WebCrypto Ed25519（Chrome 113+、Safari 17+、Firefox 129+）。

---

## [0.2.0] - 2026-04-18

### 新增
- **Relay 路径 E2EE** —— ECDH P-256 + HKDF-SHA256 + AES-GCM；Cloudflare Worker 只看到密文信封。
- Relay Durable Object：可转发消息类型白名单（`e2ee_handshake`、`encrypted`、`ping`、`pong`、`error`）。
- **Revoke-all 按钮** —— 踢掉所有远程客户端并轮换配对 token。
- **WebSocket 心跳** —— 30s ping / 60s 超时；daemon 端 alarm 在 90s 无活动后关闭死连接。
- 终端历史增量拉取（本地控制台 + relay）。
- Web 终端视图的 ANSI 颜色渲染。
- 懒加载 session 列表 + 确认栏 + 并发 relay handler。
- SQLite 聊天历史持久化（`chat_store`）。
- `soul.md` 写入：覆盖前快照 + append 打日期戳。
- 基础 pytest 测试（32 项）。
- LICENSE + README + SECURITY.md + `config.example.yaml`（开源文档轮）。

### 修复
- 每次 reconnect 都自动轮换 relay token 的行为（停掉）。
- tab 切换后 chat send 按钮卡在 disabled 的 bug。
- Telegram bot：send-command 和 session-view bug。

### 基建
- Relay Durable Object 固定在 APAC，削减太平洋来回。

---

## [0.1.0] - 2026-04-18

初次公开预览。

### 新增
- Roboot 个人 AI agent hub 基础结构。
- Web 控制台 + iTerm2 桥 + Chainlit 集成。
- Telegram bot + 浏览器语音输入。
- 流式响应 + JARVIS 句级 TTS。
- Edge TTS + 节流渲染。
- **Soul 系统** —— 通过 `soul.md` 自我修改身份。
- Relay 架构 + PWA 移动端 + 人脸识别基线。

[未发布]: https://github.com/tyxben/roboot/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/tyxben/roboot/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/tyxben/roboot/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/tyxben/roboot/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/tyxben/roboot/releases/tag/v0.1.0
