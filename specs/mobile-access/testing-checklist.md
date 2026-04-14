# Mobile Access Testing Checklist

## Pre-Testing Setup

- [ ] Install dependencies: `pip install "qrcode[pil]"`
- [ ] Generate PWA icons: `python static/generate_icons.py`
- [ ] Ensure computer connected to WiFi
- [ ] Ensure phone connected to same WiFi network

---

## Phase 1.1: IP Detection and QR Code

### Terminal Display
- [ ] Start server: `python server.py`
- [ ] Verify startup banner displays
- [ ] Verify QR code ASCII art renders correctly
- [ ] Verify primary IP address shown (e.g., 192.168.x.x)
- [ ] Verify server binds to 0.0.0.0 (not 127.0.0.1)

### Web Console - Network Info Panel
- [ ] Open http://localhost:8765 in desktop browser
- [ ] Click "移动访问" (Mobile Access) in sidebar
- [ ] Verify QR code image loads
- [ ] Verify primary network address displays
- [ ] Click address to copy to clipboard
- [ ] Verify toast notification shows "已复制"

### API Endpoints
- [ ] Visit http://localhost:8765/api/network-info
- [ ] Verify JSON response contains `primary_ip`, `all_ips`, `urls`
- [ ] Visit http://localhost:8765/api/qr-code
- [ ] Verify PNG QR code image downloads

---

## Phase 1.2: Mobile Responsive Design

### Desktop Browser (> 768px)
- [ ] Open console in desktop browser
- [ ] Verify sidebar visible on left
- [ ] Verify no mobile header/menu visible
- [ ] Verify normal layout and spacing

### Mobile Browser (< 768px)
- [ ] Open console in mobile browser or resize to < 768px
- [ ] Verify sidebar hidden by default
- [ ] Verify mobile header with "☰" menu button visible
- [ ] Verify mobile title displays "Roboot"

### Sidebar Toggle
- [ ] Tap "☰" menu button
- [ ] Verify sidebar slides in from left
- [ ] Verify backdrop overlay appears
- [ ] Tap backdrop to close
- [ ] Verify sidebar slides out
- [ ] Select a session/chat
- [ ] Verify sidebar auto-closes
- [ ] Verify mobile title updates

### Mobile Layout
- [ ] Verify chat messages max-width increased to 90%
- [ ] Verify input areas and buttons have appropriate touch targets
- [ ] Verify no horizontal scrolling
- [ ] Test in portrait and landscape orientations
- [ ] Verify safe area insets respected on iPhone

---

## Phase 1.3: PWA Support

### Service Worker Registration
- [ ] Open DevTools → Application → Service Workers
- [ ] Verify service worker registered at `/static/sw.js`
- [ ] Verify status is "activated and running"

### Manifest
- [ ] Open DevTools → Application → Manifest
- [ ] Verify manifest loads from `/static/manifest.json`
- [ ] Verify name: "Roboot - Personal AI Agent"
- [ ] Verify theme color: #e94560
- [ ] Verify icons listed (192x192, 512x512)

### Install Prompt (Desktop Chrome)
- [ ] Wait 5 seconds after page load
- [ ] Verify toast shows "💡 提示: 可将 Roboot 添加到主屏幕"
- [ ] Check address bar for install icon
- [ ] Click to install
- [ ] Verify app installs and opens in standalone window

### Add to Home Screen (iOS)
- [ ] Open in Safari on iPhone
- [ ] Tap Share button
- [ ] Tap "Add to Home Screen"
- [ ] Verify app name and icon preview
- [ ] Tap "Add"
- [ ] Find icon on home screen
- [ ] Launch from home screen
- [ ] Verify opens in standalone mode (no browser chrome)

### Add to Home Screen (Android)
- [ ] Open in Chrome on Android
- [ ] Tap menu (⋮)
- [ ] Tap "Add to Home screen"
- [ ] Verify app name
- [ ] Tap "Add"
- [ ] Find icon on home screen
- [ ] Launch from home screen
- [ ] Verify opens in standalone mode

### Offline Behavior
- [ ] With PWA installed, enable airplane mode
- [ ] Launch PWA
- [ ] Verify static assets load from cache
- [ ] Verify graceful error when trying to chat (no connection)

---

## Integration Testing

### Mobile Access via QR Code
- [ ] Start server on computer
- [ ] Open phone camera app
- [ ] Scan QR code from terminal
- [ ] Tap notification to open link
- [ ] Verify console loads on phone
- [ ] Verify all features work

### Chat Functionality on Mobile
- [ ] Send text message
- [ ] Verify response streams correctly
- [ ] Test voice input (🎤 button)
- [ ] Test JARVIS mode toggle
- [ ] Verify voice recognition works
- [ ] Verify TTS playback works

### Session Management on Mobile
- [ ] Open sidebar
- [ ] Select a Claude Code session
- [ ] Verify terminal output displays
- [ ] Send message to session
- [ ] Verify "允许/拒绝" buttons work if prompted
- [ ] Test auto-refresh toggle

### Multi-Device Testing
- [ ] Open console on desktop browser
- [ ] Open console on mobile browser simultaneously
- [ ] Send message from mobile
- [ ] Verify desktop updates (sessions list)
- [ ] Send message from desktop
- [ ] Verify both stay connected

---

## Cross-Browser Testing

### Desktop
- [ ] Chrome
- [ ] Firefox
- [ ] Safari
- [ ] Edge

### Mobile
- [ ] Safari (iOS)
- [ ] Chrome (iOS)
- [ ] Chrome (Android)
- [ ] Samsung Internet (Android)

---

## Network Scenarios

### Local WiFi Access
- [ ] Both devices on same 2.4GHz WiFi
- [ ] Both devices on same 5GHz WiFi
- [ ] Phone on WiFi, computer on Ethernet (same network)

### Edge Cases
- [ ] Computer has multiple network interfaces
- [ ] Verify all IPs listed in "Other network interfaces"
- [ ] Test connection to each IP
- [ ] Computer not connected to network
- [ ] Verify "未检测到网络连接" message

---

## Performance Testing

### Load Time
- [ ] Measure initial page load (should be < 2s on WiFi)
- [ ] Measure subsequent loads with service worker (should be < 500ms)

### Responsiveness
- [ ] Chat message round-trip latency (should be < 500ms on local network)
- [ ] Streaming text should appear smoothly
- [ ] Voice input response should be immediate

### Battery Impact (Mobile)
- [ ] Use app for 10 minutes
- [ ] Check battery usage in phone settings
- [ ] Should not be excessive

---

## Issues to Watch For

### Common Problems
- [ ] QR code doesn't render (missing Pillow dependency)
- [ ] Can't connect from mobile (firewall blocking port 8765)
- [ ] Service worker not registering (HTTP vs HTTPS)
- [ ] Icons not loading (haven't run generate_icons.py)
- [ ] Sidebar doesn't close on mobile (JavaScript error)
- [ ] Layout breaks on very small screens (< 320px)

### Regression Checks
- [ ] Desktop functionality unchanged
- [ ] Existing features still work
- [ ] No console errors in DevTools
- [ ] No broken links or 404s

---

## Sign-Off

- [ ] All critical tests passing
- [ ] No blocking bugs
- [ ] Documentation accurate
- [ ] Ready for user testing

**Tested by**: _______________  
**Date**: _______________  
**Device**: _______________  
**Browser**: _______________  
**Notes**: _______________
