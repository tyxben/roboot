# Mobile Access Implementation Plan

## Phase 1: Core Functionality (In Progress)

### 1.1 Auto-detect Local IP and Display with QR Code
**Status**: Starting
**Files to modify**:
- `server.py` - Add IP detection on startup, QR code generation endpoint
- `pyproject.toml` - Add qrcode dependency

**Implementation**:
- Detect local network IP addresses (filter out 127.0.0.1, docker, etc.)
- Generate QR code containing `http://LOCAL_IP:8765` 
- Display on startup in terminal
- Add `/api/network-info` endpoint for web console
- Add visual QR code display in console.html

**Dependencies**: `qrcode[pil]` library

---

### 1.2 Make console.html Responsive for Mobile
**Status**: Not started
**Files to modify**:
- `static/console.html` - Add responsive CSS

**Implementation**:
- Add mobile-friendly meta viewport (already present)
- Add media queries for screens < 768px
- Make sidebar collapsible/toggleable on mobile
- Adjust font sizes and touch targets
- Test on actual mobile device

**Dependencies**: None

---

### 1.3 Add PWA Support
**Status**: Not started
**Files to create**:
- `static/manifest.json` - PWA manifest
- `static/sw.js` - Service worker for offline support
- Update `static/console.html` - Link manifest

**Implementation**:
- Create manifest.json with app metadata
- Create basic service worker for caching
- Add manifest link to HTML
- Add install prompt handling
- Test "Add to Home Screen" on mobile

**Dependencies**: None

---

## Phase 2: Security (Optional, Configurable)

### 2.1 JWT Authentication System
**Status**: Not started
**Files to modify**:
- `server.py` - Add auth middleware
- `config.yaml` - Add security section
- `static/console.html` - Add login UI

**Implementation**: TBD

---

### 2.2 Permission Levels
**Status**: Not started

**Implementation**: TBD

---

### 2.3 Session Management
**Status**: Not started

**Implementation**: TBD

---

## Phase 3: Remote Access Helpers (Optional)

### 3.1 Tailscale Detection
**Status**: Not started

**Implementation**: TBD

---

### 3.2 Ngrok Integration
**Status**: Not started

**Implementation**: TBD

---

### 3.3 Documentation
**Status**: Not started

**Implementation**: TBD

---

## Completed Tasks

### ✅ 1.1 Auto-detect Local IP and Display with QR Code
- Created `network_utils.py` with IP detection and QR code generation
- Added `/api/network-info` and `/api/qr-code` endpoints to server.py
- Updated server startup to display QR code in terminal
- Changed server bind from `127.0.0.1` to `0.0.0.0` for network access
- Added "移动访问" panel in console.html with QR code display
- Added dependency: `qrcode[pil]>=7.4.0`

### ✅ 1.2 Make console.html Responsive for Mobile
- Added comprehensive mobile CSS with media queries
- Implemented collapsible sidebar for mobile (< 768px)
- Added mobile header with menu toggle
- Added backdrop overlay for sidebar
- Updated all view-switching functions to close sidebar on mobile
- Improved touch targets and spacing for mobile

### ✅ 1.3 Add PWA Support
- Created `static/manifest.json` with app metadata
- Created `static/sw.js` service worker for offline support
- Added manifest link and PWA meta tags to HTML
- Added service worker registration
- Added install prompt handling
- Created icon generator script (`static/generate_icons.py`)

## Current Status

**Phase 1 Complete!** Ready for testing.

### Next Steps:
1. Install dependencies: `pip install qrcode[pil]`
2. Generate icons: `python static/generate_icons.py`
3. Test server startup: `python server.py`
4. Test mobile access: Scan QR code from phone
5. Test PWA: Add to home screen on mobile

### Files Modified:
- `/Users/ty/self/roboot/pyproject.toml` - Added qrcode dependency
- `/Users/ty/self/roboot/server.py` - Network endpoints and startup banner
- `/Users/ty/self/roboot/static/console.html` - Mobile responsive + PWA support

### Files Created:
- `/Users/ty/self/roboot/network_utils.py` - Network utilities
- `/Users/ty/self/roboot/static/manifest.json` - PWA manifest
- `/Users/ty/self/roboot/static/sw.js` - Service worker
- `/Users/ty/self/roboot/static/generate_icons.py` - Icon generator
