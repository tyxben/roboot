# 📱 Mobile Access - Quick Start

Access Roboot from your phone in under 1 minute!

## Step 1: Start the Server

```bash
python server.py
```

You'll see:

```
============================================================
🤖 Roboot - Personal AI Agent Hub
============================================================

📍 Local access:
   http://localhost:8765

📱 Mobile access (scan QR code):
   http://192.168.1.100:8765

██████████████  ← QR Code
██  ████████  ██
██  ██    ██  ██
...
```

## Step 2: Scan the QR Code

1. **Open your phone's camera** (works on iPhone and Android)
2. **Point at the QR code** in your terminal
3. **Tap the notification** that appears
4. **Done!** Roboot opens in your mobile browser

## Step 3: Add to Home Screen (Optional)

For a native app experience:

### iPhone (Safari)
1. Tap Share button (📤)
2. Scroll down → "Add to Home Screen"
3. Tap "Add"

### Android (Chrome)
1. Tap menu (⋮)
2. "Add to Home screen"
3. Tap "Add"

---

## Troubleshooting

**Can't connect?**
- Make sure phone and computer are on the **same WiFi**
- Check if firewall is blocking port 8765

**QR code not scanning?**
- Manually type the URL shown below the QR code

**No QR code visible?**
- Install dependencies: `pip install "qrcode[pil]"`
- Check if your computer is connected to WiFi

---

## Option B: Telegram (works from anywhere, even off your WiFi)

For mobile access when you're not on the home network, Telegram is often the simplest path — no QR, no cert trust, no port forwarding.

```bash
pip install -e '.[telegram]'           # or ./scripts/setup.sh
brew install ffmpeg                    # for voice replies
python -m adapters.stt prewarm         # one-time Whisper model download (~3 GB)
# Edit config.yaml: set telegram.bot_token (from @BotFather) and telegram.allowed_users
python -m adapters.telegram_bot
```

In the Telegram chat with your bot:

- Send **text** → agent replies in text
- Send a **voice message** → mlx-whisper transcribes (~96% Chinese accuracy offline), agent replies, and you hear the answer back as 1–3 OGG voice bubbles via Edge TTS
- `/help` — list all commands (`/sessions`, `/screenshot`, `/voice`, `/remote`, `/refresh`)
- `/voice` — pick the AI's TTS voice (10 curated options, per-user)
- Or just say it in plain language: *"帮我截个屏"*, *"换成女声"*, *"看看 claude code 在跑什么"* — the agent calls the matching tool automatically

Security note: Telegram traffic is protected by Telegram's TLS but is **not** end-to-end encrypted (Secret Chats don't work with bots). Use the relay option in the full docs if E2EE matters for your use case.

---

## Full Documentation

See [docs/mobile-access.md](docs/mobile-access.md) for:
- Remote access setup (Tailscale, Ngrok, etc.)
- Security configuration
- Advanced troubleshooting
- FAQ

---

## Requirements

- **Same WiFi network** (for local access)
- **Modern browser** (Chrome, Safari, Firefox, Edge)
- **No configuration needed!**

---

**That's it!** You can now control your AI assistant from anywhere in your home.
