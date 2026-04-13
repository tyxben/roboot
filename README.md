# Roboot

你的私人 AI 助手中枢 — 语音对话、管理多个 Claude Code 会话、在 Mac 上执行任何操作。

## 它能做什么

- **语音对话（贾维斯模式）** — 对着浏览器说话，她听、想、回答，全程不碰键盘
- **管理 Claude Code** — 自动发现 iTerm2 里所有 Claude Code 会话，查看输出、发送指令、一键允许/拒绝
- **执行命令** — 通过自然语言让她在你的 Mac 上跑任何终端命令
- **远程控制** — 出门后通过 Telegram 管理你的电脑
- **自我进化** — 她有自己的 `soul.md`，能改名字、改性格、记住你的偏好

## 快速开始

```bash
# 安装
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2

# 配置
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入 API key（至少填一个 deepseek）

# iTerm2 设置
# iTerm2 → Settings → General → Magic → 勾选 Enable Python API

# 启动
python server.py
# 打开 http://localhost:8765
```

## 界面

打开 `http://localhost:8765` 后是一个左右分栏的控制台：

**左栏** — 所有 iTerm2 会话列表
- 点击进入会话终端视图
- 实时显示输出（每 3 秒刷新）
- 检测到等确认时弹出"允许/拒绝"按钮
- 底部输入框直接向 Claude Code 发消息

**右下角"Ava 聊天"** — AI 助手对话
- 文字输入或语音输入（🎤 按钮）
- 流式响应，文字实时出现
- "贾维斯模式"：连续语音对话，说完自动继续听

## 贾维斯模式

1. 打开控制台 → 点击"Ava 聊天"
2. 点右上角"贾维斯模式"
3. 绿色圆球亮起 = 正在听
4. 直接说话 → 她回答 → 继续听
5. 她说话时你开口 = 打断，她会停下来听你

语音用的是 Edge TTS（微软神经网络语音），免费且自然。她只朗读核心摘要，细节在屏幕上看。

## 自定义你的助手

编辑 `soul.md` 或直接跟她说：

```
"你以后叫小七"          → 改名
"说话活泼一点"          → 改风格
"记住我叫 xxx"          → 记住你
"你现在什么性格"        → 她会告诉你
```

所有改动写入 `soul.md`，你可以随时查看她"想"了什么。

## 多种使用方式

```bash
python server.py                    # Web 控制台（推荐）
python run.py                       # 终端键盘模式
python run.py --voice               # 终端语音模式
python -m adapters.telegram_bot     # Telegram 远程
chainlit run chainlit_app.py -w     # Chainlit UI
```

## 技术栈

- **Agent**: [Arcana](https://github.com/tyxben/arcana) 0.4.0
- **LLM**: DeepSeek（默认）/ GLM / Claude / OpenAI（可切换）
- **TTS**: Edge TTS（免费微软神经网络语音）
- **STT**: Chrome Web Speech API（浏览器端免费）
- **iTerm2**: Python API websocket 持久连接
- **Web**: FastAPI + WebSocket + 原生 HTML/JS
