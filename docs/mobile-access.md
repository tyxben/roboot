# Mobile Access Guide

This guide explains how to access Roboot from your mobile device.

## Quick Start (Local Network)

The simplest way to access Roboot from your phone is on the same WiFi network as your computer.

### Step 1: Start Roboot

```bash
python server.py
```

You'll see a startup banner with a QR code:

```
============================================================
🤖 Roboot - Personal AI Agent Hub
============================================================

📍 Local access:
   http://localhost:8765

📱 Mobile access (scan QR code):
   http://192.168.1.100:8765

██████████████████████████████████
██████████████████████████████████
████  ████      ██      ████  ████
████  ██  ██████  ██████  ██  ████
████  ██  ██████  ██████  ██  ████
...
```

### Step 2: Scan QR Code

1. Open your phone's camera app
2. Point it at the QR code in the terminal
3. Tap the notification to open the link
4. Roboot console will load in your mobile browser

**That's it!** No configuration needed.

### Step 3: Add to Home Screen (Optional)

For a native app-like experience:

**iOS (Safari)**:
1. Tap the Share button (box with arrow)
2. Scroll and tap "Add to Home Screen"
3. Tap "Add"

**Android (Chrome)**:
1. Tap the menu (three dots)
2. Tap "Add to Home screen"
3. Tap "Add"

The Roboot icon will appear on your home screen like a native app!

---

## Mobile Interface Features

The console automatically adapts to mobile screens:

- **Collapsible sidebar**: Tap ☰ to open/close
- **Responsive layout**: Optimized for small screens
- **Touch-friendly**: Larger buttons and touch targets
- **PWA support**: Works offline, feels like a native app
- **Full functionality**: All features work on mobile

---

## Viewing Connection Info

In the web console, click "移动访问" (Mobile Access) in the sidebar to see:

- QR code for easy mobile access
- All available network addresses
- Copy-to-clipboard for easy sharing

---

## Troubleshooting

### "No network interface detected"

**Cause**: Computer not connected to WiFi or Ethernet

**Solution**:
1. Connect to a WiFi network
2. Restart Roboot: `python server.py`

### Phone can't connect to the address

**Possible causes**:
1. Phone and computer on different WiFi networks
2. Firewall blocking connections
3. Wrong IP address

**Solutions**:
1. Ensure both devices on same WiFi
2. Check firewall settings (allow port 8765)
3. Try alternate IP addresses shown in "Other network interfaces"

### QR code doesn't scan

**Solution**: Manually type the URL shown below the QR code into your mobile browser

### Connection works but page loads slowly

**Cause**: Network congestion or weak signal

**Solutions**:
1. Move closer to WiFi router
2. Use 5GHz WiFi if available
3. Restart WiFi router

---

## Remote Access (Advanced)

For accessing Roboot from anywhere (not just local network), see the [Remote Access Guide](./remote-access.md).

Quick options:
- **Tailscale** (recommended): Secure VPN, zero-config
- **Ngrok**: Temporary public URL
- **Cloudflare Tunnel**: Custom domain (requires setup)
- **Port forwarding**: DIY (router configuration required)

---

## Security Considerations

### Local Network Access (Default)

By default, Roboot has **no authentication**. Anyone on your WiFi can access it.

**This is fine for**:
- Personal use at home
- Trusted networks
- Local-only access

**Not recommended for**:
- Public WiFi
- Shared networks (dorms, offices)
- Remote access

### Enabling Authentication (Coming Soon)

For enhanced security, see [Security Configuration](./security.md) to enable:
- JWT authentication
- Permission levels
- Session timeouts
- Audit logging

---

## Network Requirements

- **Minimum bandwidth**: 1 Mbps (for text chat)
- **Recommended**: 5+ Mbps (for voice, camera features)
- **Latency**: < 100ms for best experience
- **Ports**: 8765 (TCP)

---

## FAQ

**Q: Can I access Roboot from multiple devices simultaneously?**  
A: Yes! Multiple devices can connect at the same time.

**Q: Does mobile access work offline?**  
A: The PWA caches static assets for fast loading, but you need a connection to the server for AI features.

**Q: Can I use mobile voice features?**  
A: Yes! The Web Speech API works on mobile browsers (best on Chrome).

**Q: Is data encrypted?**  
A: Local network traffic is unencrypted HTTP. For remote access with encryption, use Tailscale or set up HTTPS.

**Q: Can I use a custom domain?**  
A: Yes, with Cloudflare Tunnel or reverse proxy. See [Remote Access Guide](./remote-access.md).

**Q: Does this work on tablets?**  
A: Yes! The responsive design works on all screen sizes.

---

## Related Documentation

- [Remote Access Guide](./remote-access.md) - Access from anywhere
- [Security Configuration](./security.md) - Enable authentication
- [PWA Features](./pwa.md) - Progressive Web App capabilities
- [CLAUDE.md](../CLAUDE.md) - Architecture overview
