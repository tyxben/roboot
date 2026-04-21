# Roboot 使用指南

> English version: [USAGE.md](USAGE.md)

本文面向刚 clone 完仓库、想真正把 Roboot 跑起来用的人。不讲怎么给它加代码，只讲怎么用它。

---

## 1. 快速开始

前置条件：macOS、Python 3.11+、装好的 [iTerm2](https://iterm2.com/)、至少一个 LLM 的 API key（推荐 [DeepSeek](https://platform.deepseek.com)，便宜好用）。

```bash
git clone https://github.com/tyxben/roboot.git && cd roboot
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2 "qrcode[pil]" cryptography
cp config.example.yaml config.yaml    # 然后编辑它，填入你的 API key
python server.py                      # 打开 http://localhost:8765
```

启动 banner 会显示本机 IP、二维码和（如果启用了）relay 配对地址。看到 `🤖 Roboot - Personal AI Agent Hub` 就说明进程起来了；浏览器打开页面后 WebSocket 一连上，就会出现 `Hey，我是 <名字>。有什么事？` 的欢迎气泡。

---

## 2. 第一次对话

打开 [http://localhost:8765](http://localhost:8765)，你会看到一个聊天面板，右侧是 iTerm2 会话栏（首次空着）。

**感受一下频道感知。** Roboot 的系统提示里注入了当前频道（`web` / `cli` / `voice` / `telegram`）以及正在运行的 Claude Code 会话列表，所以它知道你从哪里连上来、你现在看得见什么窗口。Telegram 上它会自动说得更短（“用户可能不在电脑前”），语音模式下它会避开代码块。你不需要额外告诉它。

**典型的工具调用。** 输入：

```
列出我当前的 Claude Code session
```

你会看到：思考气泡 → 流式回复 → 消息下方一个 `1 tool call` 的小徽章（对应 `list_sessions`）→ 右边侧栏填出每个 session 的卡片。点任意一张卡可以直接查看终端输出，从 Web 里给那个会话发指令。

**`> ` 开头的朗读行。** 模型所有“该说出口”的句子都用 `> ` blockquote 前缀开头，剩下的是屏幕阅读内容（代码、表格、长列表）。开了语音后台时，服务器只读 `> ` 行。页面上 blockquote 依然会渲染成引用样式，不影响阅读。不理解 soul.md 里的说话风格提示时，这就是它在做什么。

**语音输入（STT）后端。** Telegram 语音消息和 CLI `--voice` 模式共享同一套可插拔 STT 后端，通过 `voice.stt.backend` 三选一：

- `mlx_whisper`（默认）— Apple Silicon 原生，首次下载模型后离线运行。`whisper-large-v3-mlx`（~3 GB）中文识别率约 96%。如果 RAM 或磁盘吃紧，可以把 `voice.stt.model` 换成 `whisper-medium-mlx`（~1.5 GB，约 93%）或 `whisper-small-mlx`（~500 MB，约 88%）。环境变量 `ROBOOT_WHISPER_MODEL`、`ROBOOT_WHISPER_LANGUAGE` 可以临时覆盖配置。
- `google` — 使用 `speech_recognition` 调用 Google 的免费（非官方）Web Speech 接口。本地不吃 RAM、不占磁盘，需要联网，中文识别率约 85%–90%，有速率限制。适合 Intel Mac 或不想加载几 GB 模型的环境。
- `none` — 关闭语音输入。Telegram 收到语音消息时会回复 "STT disabled"；如果你只想用文字聊，选这个。

```yaml
voice:
  stt:
    backend: mlx_whisper        # mlx_whisper | google | none
    model: whisper-large-v3-mlx # 仅 mlx_whisper 后端生效
    language: zh
```

整段 `voice.stt` 省略就等于保持 `mlx_whisper` 默认。

---

## 3. 配置

```bash
cp config.example.yaml config.yaml
```

`config.yaml` 是 gitignored 的，不会被 commit。字段说明：

- `providers` — 每家 LLM 的 API key。至少填一个；留空或保持 `sk-REPLACE_ME` 的条目会被自动忽略。
- `default_provider` / `default_model` — 用哪家的哪个模型。必须和上面 `providers` 里的 key 一致。默认 `deepseek` + `deepseek-chat`。
- `daily_budget_usd` — 每日花费软上限（美元），由 Arcana 的 budget tracker 在进程内强制。默认 5。
- `voice.tts_voice` — Edge TTS 的声音名，例如 `zh-CN-XiaoxiaoNeural`、`en-US-JennyNeural`。留空 = `zh-CN-YunxiNeural`。完整列表：`edge-tts --list-voices`。
- `voice.language` — CLI `--voice` 模式的语音识别语言。
- `name` / `personality` — 助手名字和基础人设。注意：如果 `soul.md` 存在，它里面的 Identity/Personality/Speaking Style 会覆盖这里（参见第 6 节）。
- `telegram.bot_token` — 从 @BotFather 拿到的 bot token。留空 = 禁用 Telegram。
- `telegram.allowed_users` — 允许的 Telegram user ID 列表（用 @userinfobot 查）。**空列表 = 所有人都能和你的 bot 对话，强烈不建议。**
- `remote_access.method` — `"none"` / `"official_relay"` / `"custom_relay"`。
- `remote_access.relay.enabled` / `endpoint` — relay 连接开关和地址。官方 relay 是 `wss://relay.coordbound.com`，免费，跑在 Cloudflare Workers 上；想自部署看 `relay/` 目录。

改完配置重启 `python server.py` 即可生效。

---

## 4. 使用方式

### Web 控制台（主要方式）

```bash
python server.py    # https://localhost:8765 （如证书存在）或 http://localhost:8765
```

这是一站式前端：聊天、iTerm2 会话侧栏、摄像头、语音、relay 配对二维码、`撤销所有远程访问` 按钮都在这里。

### CLI

```bash
python run.py            # 纯键盘
python run.py --voice    # 本地麦克风 + macOS `say` TTS
```

适合在终端里快速问一句话、不开浏览器的场景。

### Telegram Bot

```bash
python -m adapters.telegram_bot
```

前提：`config.yaml` 里填好 `telegram.bot_token`。强烈建议同时设置 `allowed_users`。支持 `/start`、`/sessions`（互动管理 iTerm2 会话）、`/screenshot`、`/remote`、`/refresh`。直接发消息就是和助手聊天。

### 通过 Relay 远程访问

在 `config.yaml` 把 `remote_access.relay.enabled` 设为 `true` 并保留 `endpoint`。重启 `server.py`，banner 里会多出一段配对 URL 和二维码。手机扫码打开网页，和 Web 控制台功能一致，但流量经 Cloudflare Worker 中转且端到端加密（ECDH P-256 → HKDF → AES-GCM，daemon 用 ed25519 身份密钥签名）。每 30 分钟 token 自动轮换；本地 Web 控制台的红色 **撤销所有远程访问** 按钮可立刻踢掉所有已配对的手机并作废旧 URL。

---

## 5. Claude Code 集成

Roboot 的重头戏是和 iTerm2 里你正在跑的 Claude Code 会话对话。

**启用 iTerm2 Python API。** iTerm2 → Settings → General → Magic → 勾选 **Enable Python API**。第一次连接 iTerm2 会弹一个授权提示，点允许。

**四个工具。** 让 Roboot 帮你管理会话：

- `list_sessions` — 「列出我当前的 Claude Code session」→ 返回项目名、窗口名、session_id。
- `read_session` — 「hair 那个会话在干嘛？」→ 读最近 100 行终端输出。
- `send_to_session` — 「让 agent 那个继续」或「帮我按一下 y」→ 向指定会话发文本（模糊匹配项目名）。
- `create_claude_session` — 「开一个新的 Claude Code 搞定这个 PR」→ 在 iTerm2 新 tab 里启动。

**🔔 主动通知。** 后台 `session_watcher.py` 每 5 秒扫一遍所有会话，当某个 Claude Code 进入“等待 y/n 确认”状态时，服务器会向所有连上的 Web 控制台（包括手机上的 relay pair page）广播一个 notify 帧，文本以 🔔 开头，格式 `🔔 Session <项目>: <等待行>`。看到 🔔 就知道某个 Claude Code 在等你拍板。

---

## 6. 个性与记忆

`soul.md` 是 Roboot 的**身份文件**，被 commit 进仓库，同时也是它自己可以改的。文件分五段：

- `## Identity` — 名字、语音、角色。
- `## Personality` — 性格。
- `## Speaking Style` — 说话风格。
- `## About User` — 它关于**你**的累积记忆。
- `## Notes` — 它给自己写的笔记。
- `## 自我反馈` — 它对自己表现的反思。

每次新建聊天会话，系统提示都会从 `soul.md` 现场组装（见 `tools/soul.py::build_personality`）。助手可以调用 `update_self`、`remember_user`、`add_note` 三个工具主动改它——你直接说「以后叫我 Ty」「你改名叫 Ava」「把声音换成 Xiaoxiao」它就改了。

**自动蒸馏。** 每 20 轮对话后，后台会并发跑两个蒸馏 pass（在 `memory.py`）：一个往 `## About User` 追加对你的新了解，一个往 `## 自我反馈` 写一条不超过 60 字的“我这 20 轮做错了什么、下次怎么改”。两者都对模型要求“没新东西就输出 NOTHING”，所以平淡的对话不会污染文件。

**手动编辑。** `soul.md` 就是普通 Markdown，随便改。如果觉得它学歪了（比如记成“用户喜欢半夜被摄像头拍”），直接打开删掉那一行。每次写入前，前一版会被快照到 `.soul/history/<时间戳>.md`，可以翻旧账。

---

## 7. 自动升级

默认**关**。只在设置了环境变量后才启动：

```bash
ROBOOT_AUTO_UPGRADE=1 python server.py
```

开启后会：每小时 `git fetch origin main` → 有新 commit 且工作区干净 → `git pull --ff-only` → 跑 `python -m pytest tests/ -x`（60 秒内）→ 通过则写 `.upgrade_pending` sentinel、向所有连接的控制台广播 `⬆️ 升级到 <sha>，重启中…` → `os.execv` 自重启。任何一步失败（网络、测试超时、测试不过）都会回滚到升级前的 SHA 并继续正常运行。正在流式回复时自动推迟到下一轮。

再加一个 `ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1` 可以要求 `origin/main` 的 HEAD 必须被一个 `git verify-tag` 验证通过的 `v*` 标签指向才会拉取（GPG 或 SSH 签名皆可，取决于你的 git 配置）。这样你得每次发版都 `git tag -s vX.Y.Z && git push --tags`，否则循环每次都静默跳过。

**什么时候想开：** 长期跑在家里/服务器上、你不会日常动代码的部署。
**什么时候不想开：** 你本地正在开发、工作区经常有未提交改动、或者你不信任 main 分支的每一个 HEAD。

---

## 8. 故障排查

**`iTerm2 未连接` / `list_sessions` 总是空。** 确认 iTerm2 → Settings → General → Magic → **Enable Python API** 勾上了，并且第一次连接时在 iTerm2 弹出的授权框点了允许。重启 `server.py`。

**手机打开 relay pair page 一直转圈 / 证书错误。** 本地 LAN 用的是自签名证书。iOS Safari 第一次会拦“不安全连接”，按“显示详细信息 → 访问此网站”放行即可；详细步骤见 [docs/ssl-trust-guide.md](ssl-trust-guide.md)。Relay 路径用的是 Cloudflare 的正式证书，不需要额外信任，但如果一直连不上，先确认 `python server.py` 的 banner 里 `✅ Relay started` 出现了，以及 `remote_access.relay.enabled: true`。

**Telegram bot 启动没报错但没人能用。** 检查 `config.yaml` 里 `telegram.allowed_users`：如果**留空**，bot 理论上对所有人开放——但前提是他们得先 `/start`，而你没收到消息可能就是因为你自己不在允许列表里。把你从 @userinfobot 拿到的 user ID 加进去。

**`撤销所有远程访问` 按钮灰掉 / relay 配对二维码不出现。** Relay 没启用。编辑 `config.yaml` 的 `remote_access.relay.enabled: true` 并填好 `endpoint`（官方 relay 是 `wss://relay.coordbound.com`），然后重启。

**启动 banner 显示 `⚠️ SSL/TLS: DISABLED` / Web 控制台不能用摄像头语音。** 浏览器只在 HTTPS 下允许 `getUserMedia`。在 `certs/` 下放 `cert.pem` + `key.pem`（仓库里已经带了一对自签证书，如不适用可用 `openssl req -x509 -newkey rsa:4096 -nodes -keyout certs/key.pem -out certs/cert.pem -days 365 -subj "/CN=localhost"` 生成一对），重启即可。
