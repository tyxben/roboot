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
