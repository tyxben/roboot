# Requirements: Secure Mobile Access

## Overview

Enable secure remote access to Roboot from mobile devices while maintaining strict security controls for high-risk operations (shell commands, camera access, file operations, iTerm2 session control).

## User Stories

### US-1: Mobile User Access
**As a** Roboot user  
**I want** to access my Roboot instance securely from my mobile phone  
**So that** I can control my Mac remotely without compromising security

**Acceptance Criteria:**
- WHEN the user opens the mobile web interface, THEN they must authenticate before accessing any functionality
- WHEN authentication succeeds, THEN the user receives a JWT token with defined expiration
- WHEN the token expires, THEN the user is automatically logged out and must re-authenticate
- WHEN accessing from an untrusted network, THEN all traffic is encrypted (HTTPS/WSS)

### US-2: QR Code Pairing
**As a** Roboot user  
**I want** to scan a QR code from my desktop to log in on mobile  
**So that** I don't have to type complex credentials on a small screen

**Acceptance Criteria:**
- WHEN the user opens the desktop console, THEN a QR code is displayed for pairing
- WHEN the user scans the QR code with their mobile device, THEN they are authenticated automatically
- WHEN the QR code is scanned, THEN it becomes invalid (one-time use)
- WHEN the QR code is older than 5 minutes, THEN it expires and cannot be used

### US-3: Permission-Based Access Control
**As a** Roboot administrator  
**I want** to configure different permission levels for different access methods  
**So that** I can grant limited access to less-trusted devices

**Acceptance Criteria:**
- WHEN a user has "read-only" permission, THEN they can view chat history and session status but cannot execute commands
- WHEN a user has "standard" permission, THEN they can chat and use safe tools but require confirmation for dangerous operations
- WHEN a user has "admin" permission, THEN they can execute all operations without additional confirmation
- WHEN permissions are configured in config.yaml, THEN they are enforced at the API and WebSocket level

### US-4: Secure Public Access via Cloudflare Tunnel
**As a** Roboot user  
**I want** to expose Roboot through Cloudflare Tunnel  
**So that** I can access it from anywhere without port forwarding or dynamic DNS

**Acceptance Criteria:**
- WHEN Cloudflare Tunnel is configured, THEN Roboot is accessible via a stable HTTPS URL
- WHEN traffic arrives via the tunnel, THEN it is encrypted end-to-end
- WHEN the tunnel configuration is invalid, THEN Roboot continues to work on localhost
- WHEN the user disables the tunnel, THEN external access is immediately revoked

### US-5: Audit Logging
**As a** Roboot administrator  
**I want** all sensitive operations logged with timestamps and user context  
**So that** I can review what was done remotely

**Acceptance Criteria:**
- WHEN a shell command is executed, THEN the command, user, timestamp, and result status are logged
- WHEN camera/screenshot tools are used, THEN the access is logged with user and timestamp
- WHEN a user logs in or out, THEN the authentication event is logged
- WHEN logs exceed 10MB, THEN old entries are rotated to archive files

### US-6: Session Management
**As a** Roboot user  
**I want** my mobile session to timeout after inactivity  
**So that** an unattended device doesn't remain authenticated indefinitely

**Acceptance Criteria:**
- WHEN there is no activity for 30 minutes, THEN the session expires automatically
- WHEN the session expires, THEN the client is notified via WebSocket disconnect
- WHEN the user attempts an action with an expired session, THEN they receive a 401 Unauthorized response
- WHEN the user activity is detected, THEN the session timeout is extended

### US-7: Mobile-Optimized UI
**As a** mobile user  
**I want** the interface to be responsive and touch-friendly  
**So that** I can use Roboot comfortably on a small screen

**Acceptance Criteria:**
- WHEN viewing on a screen smaller than 768px, THEN the sidebar collapses to a drawer
- WHEN typing in the chat input, THEN the mobile keyboard doesn't obscure the conversation
- WHEN scrolling through messages, THEN touch gestures work smoothly with momentum scrolling
- WHEN the app is added to home screen (PWA), THEN it launches in fullscreen mode

### US-8: Optional IP Whitelisting
**As a** Roboot administrator  
**I want** to restrict access to specific IP addresses  
**So that** only my known networks can connect even with valid credentials

**Acceptance Criteria:**
- WHEN IP whitelist is configured in config.yaml, THEN requests from non-whitelisted IPs are rejected before authentication
- WHEN the whitelist is empty, THEN IP filtering is disabled
- WHEN a request is blocked by IP filter, THEN the event is logged with the rejected IP
- WHEN behind Cloudflare, THEN the real client IP is extracted from CF-Connecting-IP header

## Edge Cases and Error Scenarios

### EC-1: Token Theft
**Scenario:** An attacker obtains a valid JWT token  
**Handling:**
- Tokens have short expiration (1 hour access token, 7 day refresh token)
- Refresh tokens are invalidated on logout
- Suspicious activity (multiple IPs using same token) triggers automatic logout
- User can revoke all sessions from desktop console

### EC-2: WebSocket Connection Loss
**Scenario:** Mobile network switches or becomes unstable  
**Handling:**
- Client automatically attempts reconnection with exponential backoff
- JWT is re-validated on reconnection
- Incomplete tool executions are marked as "interrupted" in UI
- User is notified of connection status changes

### EC-3: Concurrent Sessions
**Scenario:** User logs in from multiple mobile devices  
**Handling:**
- Each device gets its own session with unique JWT
- Maximum of 5 concurrent sessions per user
- Oldest session is terminated when limit is exceeded
- User can view and terminate active sessions from settings

### EC-4: Cloudflare Tunnel Failure
**Scenario:** Cloudflare Tunnel process crashes or disconnects  
**Handling:**
- Server continues to function on localhost
- Health check endpoint reports tunnel status
- Automatic tunnel restart with exponential backoff
- Desktop console shows tunnel connection status

### EC-5: Password Reset / First-Time Setup
**Scenario:** User has never set up authentication  
**Handling:**
- On first run, server generates a random admin password and displays it in console
- Admin must set a permanent password via desktop interface before mobile access is enabled
- QR code is only shown after authentication is configured
- Temporary password expires after first use

### EC-6: Camera Access During Active Call
**Scenario:** User tries to use look() tool while Mac camera is in use  
**Handling:**
- Tool detects camera unavailability and returns clear error message
- Audit log records the failed attempt (not a security issue)
- Mobile UI shows error without crashing

## Performance Requirements

### PR-1: Authentication Latency
- Token validation must complete within 50ms
- JWT signature verification must not block WebSocket message handling
- WHEN 10 concurrent mobile clients are connected, THEN authentication overhead adds less than 100ms to message latency

### PR-2: Mobile Data Efficiency
- WHEN streaming LLM responses, THEN messages are compressed with gzip when client supports it
- WHEN sending chat history, THEN pagination limits initial load to last 50 messages
- WHEN uploading camera images, THEN client-side compression reduces payload by at least 50%

### PR-3: TLS Handshake
- WHEN establishing HTTPS connection, THEN TLS handshake completes within 500ms on 4G connection
- WHEN using Cloudflare Tunnel, THEN end-to-end latency adds less than 100ms overhead

## Security Requirements

### SR-1: Authentication Strength
- Passwords must be at least 12 characters
- JWT secret must be cryptographically random (32+ bytes)
- Password storage uses bcrypt with cost factor 12+
- Failed login attempts are rate-limited (5 attempts per 15 minutes per IP)

### SR-2: Transport Security
- All remote access requires HTTPS with TLS 1.2+ (TLS 1.3 preferred)
- WebSocket connections must use WSS (WebSocket Secure)
- Self-signed certificates are acceptable for local network access
- Cloudflare Tunnel provides CA-signed certificates automatically

### SR-3: Tool Execution Security
- WHEN executing shell commands remotely, THEN the command and user are logged
- WHEN a dangerous command pattern is detected, THEN it requires explicit confirmation
- WHEN permission level is insufficient, THEN operation is blocked with 403 Forbidden
- WHEN tool execution fails, THEN error messages don't leak system details

### SR-4: Secrets Management
- JWT secret is generated on first run and stored in config.yaml (gitignored)
- Passwords are never logged or transmitted in plain text
- API keys in config.yaml remain inaccessible to mobile clients
- Session tokens are invalidated on password change

### SR-5: Input Validation
- All WebSocket messages are validated against JSON schema
- SQL injection, XSS, and command injection patterns are filtered
- File paths are validated to prevent directory traversal
- User input length is limited (chat: 10000 chars, commands: 2000 chars)

## Integration Requirements

### IR-1: Existing FastAPI Server
- Authentication middleware integrates with existing FastAPI app
- WebSocket endpoint /ws is extended to validate JWT on connection
- Existing desktop access remains unauthenticated on localhost
- Mobile access detection is based on User-Agent or explicit auth requirement

### IR-2: config.yaml Integration
- All security settings are configured in config.yaml under `security:` section
- Sensible defaults allow immediate use without extensive configuration
- Missing security config triggers warning but doesn't prevent startup
- Example configuration is documented in config.example.yaml

### IR-3: Arcana Runtime Compatibility
- Authentication doesn't interfere with Arcana's tool execution flow
- User context is passed to tools that need it (for audit logging)
- JWT claims include user ID that tools can access via context
- Budget tracking remains global (not per-user)

### IR-4: Cloudflare Tunnel Integration
- Tunnel configuration stored in cloudflare-tunnel.yaml (separate from main config)
- Tunnel health is monitored and reported in /api/health endpoint
- Automatic tunnel restart on failure with exponential backoff
- Tunnel logs are separate from main application logs

## Out of Scope (Explicitly Not Included)

- Multi-user support (this is a personal assistant, single user only)
- OAuth2/SSO integration (overkill for single-user scenario)
- TOTP/2FA (considered, but QR code pairing + short token expiration is sufficient for v1)
- Biometric authentication (client-side implementation varies, defer to v2)
- End-to-end encryption beyond TLS (WebSocket content is already encrypted by TLS)
- Mobile native apps (PWA is sufficient for v1)

## Dependencies

- Python libraries: PyJWT (JWT handling), bcrypt (password hashing), python-multipart (file uploads)
- Cloudflare Tunnel: cloudflared binary (installed separately)
- TLS certificates: Self-signed via OpenSSL (local) or Cloudflare-managed (tunnel)

## Success Metrics

- User can authenticate from mobile device within 10 seconds of QR scan
- Zero unauthorized access attempts succeed in security testing
- Mobile UI is usable on screens as small as 375px (iPhone SE)
- Audit logs capture 100% of shell/camera/file operations
- Token validation adds less than 50ms latency to WebSocket messages
