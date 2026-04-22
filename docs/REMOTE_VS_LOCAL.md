# Roboot: 远程 vs 本地能力对照 / Remote vs Local Capability Comparison

## 中文

你想从手机上干什么？坐在 Mac 前和带着手机在外面，能做的事情不完全一样。下面这张表告诉你每条通道实际支持什么。

### 能力对照表

| 能力 | 本地 Web 控制台 | LAN 其他设备 | Telegram Bot | Relay 远程 Web |
|---|---|---|---|---|
| 与 Agent 文字聊天 | ✅ | ✅ | ✅ | ✅ |
| 语音输入（麦克风） | ✅[^1] | ⚠️[^2] | ✅[^3] | ⚠️[^2] |
| TTS 语音播报 | ✅[^4] | ✅[^4] | ✅[^5] | ⚠️[^6] |
| 列出 Claude Code 会话 | ✅ | ✅ | ✅ | ✅ |
| 查看会话终端内容 | ✅ | ✅ | ✅ | ✅ |
| 向会话发送按键 | ✅ | ✅ | ✅[^7] | ✅ |
| 批准/拒绝 (y/n) 确认 | ✅ | ✅ | ✅ | ✅ |
| 新建 Claude Code 会话 | ⚠️[^8] | ⚠️[^8] | ⚠️[^8] | ⚠️[^8] |
| 主动 🔔 通知 | ⚠️[^9] | ⚠️[^9] | ✅ | ⚠️[^10] |
| 摄像头 / 人脸识别（`look`） | ✅[^11] | ✅[^11] | ❌[^12] | ✅[^11] |
| Shell 命令执行（`shell` 工具） | ✅ | ✅ | ✅ | ✅ |
| 自修改 soul.md | ✅ | ✅ | ❌[^13] | ✅ |
| 自动升级提示可见 | ⚠️[^9] | ⚠️[^9] | ❌ | ⚠️[^10] |

[^1]: localhost 是安全上下文，Web Speech API 可用。
[^2]: 浏览器 Web Speech API 需要 HTTPS；LAN 要先生成证书并信任（见 `docs/ssl-trust-guide.md`），Relay 永远走 HTTPS 所以可用，但都受浏览器支持度限制（主要是 Chrome）。
[^3]: Telegram 发语音消息，Bot 默认用 mlx-whisper (`large-v3`, ~96% 中文准确率) 离线转写；Intel Mac / Linux 可在 config.yaml 切 `voice.stt.backend: google` 走 speech_recognition。不是实时麦克风但体验接近。
[^4]: 浏览器调用服务器 `/api/tts`（Edge TTS），高质量中文语音。
[^5]: Bot 抽取模型回复的 `> ` 朗读行,经 `adapters/tts_streamer.py` 分段并行合成(Edge TTS → ffmpeg → OGG/Opus),回 1-3 条 Telegram 原生语音气泡。`/voice` 命令切声音。
[^6]: pair-page 用浏览器自带的 `speechSynthesis`（本机 OS 声音），而不是服务器的 Edge TTS —— pair-page 尚未代理 `/api/tts`。
[^7]: 通过 inline keyboard 上 "✏️ 发送命令" 按钮弹出命令输入态，下一条消息作为命令发到会话。
[^8]: 所有通道都只能通过对 Agent 发话触发（例如"在 ~/proj 开一个新的 Claude Code 会话"），任何前端都没有显式的"新建会话"按钮。
[^9]: `session_watcher` 和 `self_upgrade` 会广播 `{"type":"notify", ...}` 给本地控制台 WebSocket，但 `static/console.html` 目前没有 `notify` 处理器 —— 守护进程发了，前端没显示。
[^10]: 守护进程通过 `_relay_broadcast` 把 `notify` 帧加密推给每个 relay 客户端，但 `pair-page.ts` 里也没有 `notify` 处理器，所以手机上不会有 🔔。
[^11]: 任何通道跟 Agent 说"看看摄像头"都会触发 `look` 工具在 Mac 上采图；图片回传到聊天气泡。远程端自己不控制摄像头，是 Agent 在 Mac 本地调用。
[^12]: Telegram Bot 的 `ALL_TOOLS` 里没有 `look`、`enroll_face`，只有 `screenshot`（`/screenshot` 命令）。
[^13]: Telegram Bot 的 `ALL_TOOLS` 里没有 `update_self` / `remember_user` / `add_note`。

### 为什么会有差异

两条结构性原因，不是偷懒：

**沙盒隔离**。iTerm2 控制、摄像头采集、本地 shell 这些都必须在守护进程所在的 Mac 上执行；远程前端无法直接驱动本地硬件。所有"远程能做"的都必须绕回 Agent 的工具调用（由守护进程来跑），这是个特性不是缺陷 —— 远程客户端被拖库也拿不到本地 shell 控制权。

**UI 表面刻意精简**。pair-page 是个刻意精简的子集 —— 按"Relay Load Optimization"计划，会话列表懒加载、隐藏标签页时暂停轮询、不跑本机没必要的面板。这样才能在 4G/手机电池下保持可用。Telegram 走 Bot API，界面受限于 inline keyboard 和消息。实时 JARVIS 模式用不上(语音条是 OGG 文件,不是流式),但转写 + 分段合成的体验已经接近。

### 已知远程缺口（待修）

- **Remote `notify` 帧**：守护进程已经通过 `_relay_broadcast` 加密推 `{"type":"notify",...}`；`relay/src/pair-page.ts` 没有对应的 handler。需要一个小 patch：在 `ws.onmessage` 里加一个 `data.type === 'notify'` 分支，插入一条 toast 或聊天气泡。
- **Layer A 历史回放**：`relay_client.py::_on_client_hello` 已经处理 `resume_session_id`；但 pair-page 从来不发这个字段，所以每次重连都是新会话。需要浏览器把上次的 `history_session_id` 存进 `localStorage`，连上时作为 `client_hello` 的一部分发出。
- **远程新建会话按钮**：`create_claude_session` 工具在守护进程已注册，但 pair-page 没 UI 按钮。目前只能通过自然语言触发。加个 UI 或让模型在 `tool_start` 时清晰露出进度即可。
- **远程 TTS**：pair-page 没代理 `/api/tts`，JARVIS 模式走浏览器原生 `speechSynthesis`，中文发音明显不如服务端 Edge TTS。补一个通过加密通道让 pair-page 拉 mp3 的小端点就能闭环。

### 安全模型对照

- **LAN**：TOFU 风格 —— 用自签证书，首次需要在手机上"信任"那张证书。TLS 之后相当于明文信任机器和同 Wi-Fi 网络。没有 E2EE 概念。
- **Relay 远程 Web**：带签名的守护进程身份 + 端到端加密。ed25519 指纹走 URL fragment 旁路送达，ECDH + HKDF + AES-GCM 在两端协商，Cloudflare Worker 只看得到密文。即使 Worker 被拿下也 MITM 不了。
- **Telegram**：走 Telegram 自己的 TLS + bot token + `allowed_users` 白名单。**不享受 Roboot 的 E2EE 设计** —— 你信的是 Telegram 这家公司和它的基础设施，消息在他们的服务器上以明文可见。

详细威胁模型、密钥生命周期、撤销流程见 [`SECURITY.md`](../SECURITY.md)。

### vs Claude Code 自带远程 / vs Claude Code Native Remote

Anthropic 自己也提供若干远程 Claude Code 入口（本文撰写时，`claude.ai/code` 网页端、Claude iOS App、以及 `claude` CLI 的远程特性都在演进中，具体支持哪些功能可能随版本变动 —— 以本仓库相关代码为准，疑点按"不在讨论范围"处理）。

- **谁在跑代码**：Claude Code 自带远程 = Anthropic 托管的容器；Roboot = 你自己的 Mac。
- **谁拿着密钥/仓库权限**：前者通过 OAuth 委托给 Anthropic；后者全在本机（`config.yaml` gitignored，`.identity/` 0600）。
- **能控什么**：前者是 Anthropic 沙盒里的一个 Claude Code 会话；后者是你 iTerm2 里所有 Claude Code 会话 + shell + 摄像头 + 语音 + Telegram，共用一个 Agent。
- **威胁模型**：前者 = 信任 Anthropic 的云；后者 = 信任自己的机器 + 一条哑的 Cloudflare 管道（E2EE 到端）。
- **Provider 灵活度**：前者只用 Anthropic；后者通过 `config.yaml` 可切 DeepSeek / GLM / Anthropic 等。

选哪个：Claude Code 自带远程零配置、最简单；当你需要继续掌控自己的机器 + 统一多个 Claude Code 会话 + 再叠上非 CC 的工具（shell / vision / voice）时选 Roboot。

---

## English

What do you actually want to do from your phone? Sitting at the Mac and being out with your phone are not interchangeable. The table below says what each channel really supports.

### Capability Matrix

| Capability | Local Web | LAN device | Telegram Bot | Remote Web (Relay) |
|---|---|---|---|---|
| Text chat with agent | ✅ | ✅ | ✅ | ✅ |
| Voice input (mic) | ✅[^e1] | ⚠️[^e2] | ✅[^e3] | ⚠️[^e2] |
| TTS audio playback | ✅[^e4] | ✅[^e4] | ✅[^e5] | ⚠️[^e6] |
| List Claude Code sessions | ✅ | ✅ | ✅ | ✅ |
| Read session terminal contents | ✅ | ✅ | ✅ | ✅ |
| Send keystrokes to a session | ✅ | ✅ | ✅[^e7] | ✅ |
| Approve/Deny (y/n) confirmations | ✅ | ✅ | ✅ | ✅ |
| Create new Claude Code session | ⚠️[^e8] | ⚠️[^e8] | ⚠️[^e8] | ⚠️[^e8] |
| Proactive 🔔 notifications | ⚠️[^e9] | ⚠️[^e9] | ✅ | ⚠️[^e10] |
| Camera / face recognition (`look`) | ✅[^e11] | ✅[^e11] | ❌[^e12] | ✅[^e11] |
| Shell command execution (`shell` tool) | ✅ | ✅ | ✅ | ✅ |
| Self-modify soul.md | ✅ | ✅ | ❌[^e13] | ✅ |
| Auto-upgrade trigger visibility | ⚠️[^e9] | ⚠️[^e9] | ❌ | ⚠️[^e10] |

[^e1]: localhost is a secure context; Web Speech API works.
[^e2]: Browser Web Speech API requires HTTPS. LAN needs a generated+trusted cert (see `docs/ssl-trust-guide.md`); relay is always HTTPS so it works — but browser support is uneven (mostly Chrome).
[^e3]: Telegram supports voice-message upload; the bot uses mlx-whisper (`large-v3`, ~96% Chinese accuracy) offline by default. Intel Mac / Linux users can flip `voice.stt.backend: google` in config.yaml for the `speech_recognition` fallback. Not a realtime mic but close to it.
[^e4]: Browser calls server `/api/tts` (Edge TTS) for high-quality Chinese voices.
[^e5]: Bot extracts the `> ` blockquote "spoken" lines and synthesizes them in parallel via `adapters/tts_streamer.py` (Edge TTS → ffmpeg → OGG/Opus), replying as 1-3 native Telegram voice bubbles. `/voice` switches voice per-user.
[^e6]: pair-page uses the browser's built-in `speechSynthesis` (local OS voice), not server Edge TTS — pair-page does not proxy `/api/tts` yet.
[^e7]: Via the "✏️ 发送命令" inline-keyboard button, which puts the user into command-input mode; the next message is sent to the session.
[^e8]: Only invocable by talking to the agent (e.g., "open a new Claude Code session in ~/proj"). No frontend exposes an explicit "new session" button on any channel.
[^e9]: `session_watcher` and `self_upgrade` broadcast `{"type":"notify", ...}` to local console WebSockets, but `static/console.html` has no `notify` handler — daemon sends, browser ignores.
[^e10]: Daemon encrypts and pushes `notify` frames via `_relay_broadcast`, but `pair-page.ts` lacks a matching handler, so no 🔔 surfaces on mobile.
[^e11]: Any channel can tell the agent "check the camera" and trigger the `look` tool, which runs on the Mac. The remote end doesn't drive the camera directly.
[^e12]: Telegram Bot's `ALL_TOOLS` includes only `screenshot` (via `/screenshot`), not `look` or `enroll_face`.
[^e13]: Telegram Bot's `ALL_TOOLS` does not include `update_self` / `remember_user` / `add_note`.

### Why things differ

Two structural reasons, not laziness:

**Sandboxing.** iTerm2 control, camera capture, and local shell all have to run on the Mac where the daemon lives. The remote frontend cannot directly drive local hardware — everything "remote can do" routes back through an agent tool call the daemon gates. That's a feature, not a bug: a compromised remote client still can't grab a local shell handle.

**Deliberately-trimmed UI surface.** The pair-page is an intentional subset per the "Relay Load Optimization" plan — lazy-load the session list, pause polling when hidden, skip panels that don't apply remotely. That's what keeps it usable over 4G on battery. Telegram goes through the Bot API and is naturally limited to inline keyboards and messages. Real-time JARVIS-style streaming voice doesn't fit (voice notes are complete OGG files), but transcription + parallel synthesis gets close.

### Known remote gaps (fixes pending)

- **Remote `notify` frame**: daemon already encrypts and pushes `{"type":"notify",...}` via `_relay_broadcast`; `relay/src/pair-page.ts` has no matching handler. Small patch: add a `data.type === 'notify'` branch in `ws.onmessage` that renders a toast or chat bubble.
- **Layer A history replay**: `relay_client.py::_on_client_hello` handles `resume_session_id`; the browser never sends it, so every reconnect feels amnesic. Fix: persist the prior `history_session_id` in `localStorage` and include it in `client_hello`.
- **Remote create-new-session**: the `create_claude_session` tool is registered on the daemon; pair-page has no UI button. Today it only triggers via natural-language chat. Adding a UI shortcut (or clearer tool-start UX) would close the loop.
- **Remote TTS**: pair-page does not proxy `/api/tts`; JARVIS mode falls back to browser `speechSynthesis`, which sounds noticeably worse in Chinese than server Edge TTS. A small encrypted mp3-fetch path would close the gap.

### Security posture comparison

- **LAN**: TOFU-style — self-signed cert, trust once on the phone. After TLS you effectively trust the machine and the Wi-Fi network. No E2EE layer.
- **Remote Web (relay)**: signed daemon identity + end-to-end encryption. The ed25519 fingerprint rides the URL fragment (out-of-band vs. the relay); ECDH + HKDF + AES-GCM between endpoints; the Cloudflare Worker only sees ciphertext. Even a compromised Worker cannot MITM.
- **Telegram**: Telegram's own TLS + bot token + `allowed_users` allowlist. **Does NOT benefit from Roboot's E2EE design** — you're trusting Telegram the company; your messages are plaintext on their servers.

Threat model, key lifecycle, and revoke flow in [`SECURITY.md`](../SECURITY.md).

### vs Claude Code Native Remote

Anthropic also offers remote Claude Code entrypoints of its own — as of this writing, `claude.ai/code` (web), the Claude iOS app, and remote-session features in the `claude` CLI are all evolving, so exact capabilities shift between releases. Treat anything not covered below as "not in scope" rather than assuming.

- **Who hosts execution**: Claude Code native remote = Anthropic-hosted containers; Roboot = your own Mac.
- **Who holds your secrets / repo access**: delegated to Anthropic via OAuth vs. local only (`config.yaml` gitignored, `.identity/` 0600).
- **What you control**: a single Claude Code session inside Anthropic's sandbox vs. every Claude Code session running in your iTerm2 plus shell, camera, voice, and Telegram — all on one agent.
- **Threat model**: trust Anthropic's cloud vs. trust-your-own-machine + a dumb Cloudflare pipe (E2EE to the endpoints).
- **Provider flexibility**: Anthropic-only vs. swap DeepSeek / GLM / Anthropic via `config.yaml`.

Choose Claude Code native remote when you want zero setup and the simplest path; choose Roboot when you need to stay in control of the machine, unify multiple Claude Code sessions, and layer non-CC tools (shell / vision / voice) onto the same agent.
