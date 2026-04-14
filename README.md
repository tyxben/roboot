# Roboot -- 个人 AI Agent Hub

运行在 Mac 上的 AI 助手，管理 Claude Code 会话，支持语音对话，手机远程控制。

## 功能

- **AI 聊天** -- 流式输出、工具调用、多模型切换（DeepSeek / Claude / GLM）
- **iTerm2 Claude Code 会话管理** -- 自动发现所有会话，查看输出、发送指令、一键允许/拒绝
- **JARVIS 语音模式** -- 浏览器端语音输入 + Edge TTS 朗读，说完自动继续听，支持打断
- **摄像头 + 人脸识别** -- 拍照看世界、注册人脸、自动识别已知的人
- **远程访问** -- 局域网二维码、Telegram Bot、Relay 远程控制台三种方式

## 快速开始

### 安装依赖

```bash
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2 "qrcode[pil]"

# 人脸识别（可选）
pip install opencv-python face_recognition

# Telegram 远程控制（可选）
pip install python-telegram-bot
```

### 配置

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`，至少填一个 LLM provider 的 API key（推荐 deepseek，便宜）。

### 启动

```bash
# iTerm2 设置（首次）
# iTerm2 -> Settings -> General -> Magic -> 勾选 Enable Python API

python server.py
# 打开 http://localhost:8765
```

其他启动方式：

```bash
python run.py                       # 终端键盘模式
python run.py --voice               # 终端语音模式
python -m adapters.telegram_bot     # Telegram 远程
chainlit run chainlit_app.py -w     # Chainlit UI
```

## 远程访问

### 方式 1：局域网（零配置）

启动后终端会显示二维码，手机扫码直接访问。支持响应式布局和 PWA（添加到主屏幕）。

同一 Wi-Fi 下即可使用，无需任何配置。

### 方式 2：Telegram Bot（填个 Token）

1. 在 Telegram 找 @BotFather，创建 bot，拿到 token
2. 填入 `config.yaml`:
   ```yaml
   telegram:
     bot_token: "123456:ABC-DEF..."
     allowed_users: [你的Telegram用户ID]
   ```
3. 启动：`python -m adapters.telegram_bot`

出门后通过 Telegram 跟助手聊天、管理 Claude Code 会话。

### 方式 3：Relay 远程控制台（全球可用）

通过 Cloudflare Worker 中转，在任何地方访问完整的 Web 控制台。

1. 在 `config.yaml` 中启用：
   ```yaml
   remote_access:
     method: "official_relay"
     relay:
       enabled: true
       endpoint: "wss://relay.coordbound.com"
   ```
2. 启动 `python server.py`，终端会显示 relay 配对二维码
3. 手机扫码打开配对页面，自动建立 WebSocket 连接

配对 URL 包含一次性加密 token，30 分钟后自动过期。

## 安全

- Token 30 分钟自动过期（可通过 `/api/relay-refresh` 手动刷新）
- 256-bit 加密随机 token（`secrets.token_hex(32)`）
- 常量时间比较防时序攻击（relay 端 `timingSafeEqual`）
- WebSocket 连接建立后不受 token 过期影响（已认证的连接保持有效）
- Relay 限流：每 IP 每小时最多 10 个会话
- Telegram 可限制 `allowed_users` 白名单

## 架构图

```
+------------------+     +-------------------+     +------------------+
|  浏览器/手机      |     |  Telegram          |     |  Relay 控制台     |
|  console.html    |     |  adapters/         |     |  relay/          |
|  (WebSocket)     |     |  telegram_bot.py   |     |  (CF Worker)     |
+--------+---------+     +--------+----------+     +--------+---------+
         |                         |                         |
         v                         v                         v
+--------+-------------------------+-------------------------+---------+
|                        server.py (FastAPI)                           |
|  /ws              流式聊天（LLM_CHUNK 事件）                          |
|  /api/sessions/*  iTerm2 会话控制                                     |
|  /api/tts         Edge TTS 语音合成                                   |
|  /api/relay-*     Relay 状态/刷新/二维码                               |
+--------+--------------------+--------------------+-------------------+
         |                    |                    |
    arcana.Runtime      iterm_bridge.py      relay_client.py
    (LLM + Tools)       (iTerm2 API)         (Relay WebSocket)
         |
    tools/
    +-- shell.py          终端命令
    +-- claude_code.py    Claude Code 会话
    +-- vision.py         摄像头 + 截屏 + 人脸
    +-- soul.py           自我修改 + 记忆
    +-- face_db.py        人脸数据库
```

## 配置参考 (config.yaml)

参见 `config.example.yaml`，包含所有可用选项及注释说明。核心配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `providers.deepseek` | DeepSeek API key | 必填（至少一个） |
| `default_provider` | 默认 LLM 提供商 | `deepseek` |
| `default_model` | 默认模型 | `deepseek-chat` |
| `daily_budget_usd` | 每日预算（美元） | `5.0` |
| `voice.tts_voice` | Edge TTS 语音 | 空（用默认） |
| `telegram.bot_token` | Telegram Bot token | 空（不启用） |
| `remote_access.method` | 远程访问方式 | `none` |

## 添加新工具

1. 创建 `tools/my_tool.py`
2. 用 `@arcana.tool()` 装饰器，填写 `when_to_use` 描述何时调用
3. 在 `server.py` 的 `ALL_TOOLS` 列表中导入并添加
4. 完成 -- Arcana 自动注册，无需其他改动

示例：

```python
import arcana

@arcana.tool(
    when_to_use="当用户需要做某件事时",
    what_to_expect="返回结果描述",
)
async def my_tool(param: str) -> str:
    """工具说明。"""
    return f"结果: {param}"
```

## 技术栈

- **Agent**: [Arcana](https://github.com/tyxben/arcana) 0.4.0
- **LLM**: DeepSeek（默认）/ Claude / GLM / OpenAI
- **TTS**: Edge TTS（微软神经网络语音，免费）
- **STT**: Chrome Web Speech API（浏览器端）
- **人脸识别**: face_recognition + OpenCV
- **iTerm2**: Python API WebSocket 持久连接
- **Web**: FastAPI + WebSocket + 原生 HTML/JS
- **Relay**: Cloudflare Workers + Durable Objects
