# 中心化 Relay 服务设计

## 概述

提供一个统一的 Relay 服务，让所有 Roboot 用户可以零配置地实现远程访问。

## 架构

```
┌─────────────┐
│  手机 App   │
└──────┬──────┘
       │ WSS
       ↓
┌─────────────────────────────────┐
│ Cloudflare Workers              │
│ relay.roboot.dev                │
│                                 │
│ Durable Objects (每个session)  │
│ - session/abc123                │
│ - session/xyz789                │
└──────┬──────────────────────────┘
       │ WSS
       ↓
┌─────────────┐
│  Mac Daemon │
└─────────────┘
```

## 工作流程

### 1. Daemon 启动并注册

```python
# Roboot server.py 启动时
import websockets
import uuid

async def register_to_relay():
    session_id = str(uuid.uuid4())  # 生成唯一 session ID
    
    # 连接到中心 relay
    uri = f"wss://relay.roboot.dev/daemon/{session_id}"
    
    async with websockets.connect(uri) as ws:
        # 发送握手
        await ws.send(json.dumps({
            "type": "daemon_hello",
            "version": "1.0.0"
        }))
        
        # 接收配对 token
        response = json.loads(await ws.recv())
        pairing_token = response["pairing_token"]
        
        # 生成配对 URL
        pairing_url = f"https://relay.roboot.dev/pair/{pairing_token}"
        
        # 显示二维码
        print(f"扫码配对：{pairing_url}")
        print(generate_qr_ascii(pairing_url))
        
        # 保持连接，转发消息
        async for message in ws:
            # 处理来自手机的消息
            handle_client_message(message)
```

### 2. 手机扫码配对

```javascript
// 手机浏览器访问配对 URL
// https://relay.roboot.dev/pair/abc123xyz

// Relay 返回 session info
{
  "session_id": "abc123",
  "wss_endpoint": "wss://relay.roboot.dev/client/abc123"
}

// 手机连接到 WebSocket
const ws = new WebSocket("wss://relay.roboot.dev/client/abc123");

// 端到端加密握手
ws.send(JSON.stringify({
  type: "e2ee_hello",
  publicKey: clientPublicKey
}));

// 后续所有消息都加密
```

### 3. Relay 转发消息

```javascript
// Cloudflare Durable Object
export class RelaySession {
  constructor(state, env) {
    this.state = state;
    this.daemonSocket = null;
    this.clientSockets = [];
  }

  async fetch(request) {
    const url = new URL(request.url);
    
    if (url.pathname.startsWith('/daemon/')) {
      // Daemon 连接
      const [client, server] = Object.values(new WebSocketPair());
      this.daemonSocket = server;
      this.state.acceptWebSocket(server);
      return new Response(null, { status: 101, webSocket: client });
    }
    
    if (url.pathname.startsWith('/client/')) {
      // 客户端连接
      const [client, server] = Object.values(new WebSocketPair());
      this.clientSockets.push(server);
      this.state.acceptWebSocket(server);
      return new Response(null, { status: 101, webSocket: client });
    }
  }

  async webSocketMessage(ws, message) {
    // 转发消息（加密内容，relay 看不懂）
    if (ws === this.daemonSocket) {
      // Daemon → Clients
      this.clientSockets.forEach(client => {
        client.send(message);
      });
    } else {
      // Client → Daemon
      if (this.daemonSocket) {
        this.daemonSocket.send(message);
      }
    }
  }
}
```

---

## 端到端加密（E2EE）

**关键**：Relay 只转发加密消息，看不到内容

### 加密流程

```python
# 1. ECDH 密钥交换
daemon_keypair = generate_ecdh_keypair()
client_keypair = generate_ecdh_keypair()

# 2. 交换公钥（通过 relay）
daemon → relay → client: daemon_public_key
client → relay → daemon: client_public_key

# 3. 双方独立计算共享密钥
daemon_shared = ecdh_derive(daemon_private, client_public)
client_shared = ecdh_derive(client_private, daemon_public)

# 共享密钥相同！

# 4. 后续消息用 AES 加密
encrypted = aes_encrypt(message, shared_key)
daemon → relay → client: encrypted

# Relay 看到的是乱码，无法解密
```

---

## 部署步骤

### 1. 购买域名

推荐：
- Namecheap: `roboot.dev` (~$10/年)
- Cloudflare Registrar: 成本价

### 2. 创建 Cloudflare Workers 项目

```bash
# 安装 Wrangler
npm install -g wrangler

# 登录
wrangler login

# 创建项目
mkdir roboot-relay
cd roboot-relay
wrangler init
```

### 3. 配置 wrangler.toml

```toml
name = "roboot-relay"
main = "src/index.js"
compatibility_date = "2024-01-01"

[durable_objects]
bindings = [
  { name = "RELAY_SESSIONS", class_name = "RelaySession" }
]

[[migrations]]
tag = "v1"
new_classes = ["RelaySession"]

[vars]
MAX_SESSIONS_PER_USER = "5"
SESSION_TIMEOUT_MINUTES = "60"
```

### 4. 实现 Relay 逻辑

```javascript
// src/index.js
export { RelaySession } from './session.js';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    
    if (url.pathname.startsWith('/daemon/')) {
      const sessionId = url.pathname.split('/')[2];
      const id = env.RELAY_SESSIONS.idFromName(sessionId);
      const session = env.RELAY_SESSIONS.get(id);
      return session.fetch(request);
    }
    
    if (url.pathname.startsWith('/client/')) {
      const sessionId = url.pathname.split('/')[2];
      const id = env.RELAY_SESSIONS.idFromName(sessionId);
      const session = env.RELAY_SESSIONS.get(id);
      return session.fetch(request);
    }
    
    if (url.pathname.startsWith('/pair/')) {
      // 配对页面
      return new Response(pairingHTML, {
        headers: { 'Content-Type': 'text/html' }
      });
    }
    
    // 健康检查
    return new Response('Roboot Relay OK', { status: 200 });
  }
};
```

### 5. 部署

```bash
wrangler deploy
```

访问：`https://roboot-relay.你的账号.workers.dev`

### 6. 绑定自定义域名

Cloudflare Dashboard:
- Workers → Routes → Add Route
- 添加：`relay.roboot.dev/*` → `roboot-relay`

完成！

---

## 集成到 Roboot

### server.py 修改

```python
# config.yaml
remote_access:
  method: "official_relay"  # 新增
  relay:
    endpoint: "wss://relay.roboot.dev"
    enabled: true

# server.py 启动时
if config["remote_access"]["method"] == "official_relay":
    # 连接到官方 relay
    session_id = await register_to_relay()
    
    # 生成配对链接
    pairing_url = f"https://relay.roboot.dev/pair/{session_id}"
    
    print(f"\n📱 扫码远程访问（全球可用）：")
    print(generate_qr_ascii(pairing_url))
    print(f"\n或访问：{pairing_url}")
```

### 用户体验

```bash
# 用户运行
python server.py

# 输出
🤖 Roboot 已启动

📍 本地访问: http://localhost:8765
📱 远程访问（扫码）：

████████████████
█ ▄▄▄▄▄ █▀ █ ▄▄▄▄▄ █
█ █   █ █▄▀█ █   █ █
█ █▄▄▄█ █ ▄ █ █▄▄▄█ █
████████████████

或访问: https://relay.roboot.dev/pair/abc123xyz

✅ 零配置远程访问
✅ 端到端加密
✅ 全球可用
```

---

## 成本分析

### 你的成本

**一次性**：
- 域名: $10/年

**运营**：
- Cloudflare Workers: **免费**（10万请求/天）
- Durable Objects: **免费**（前100万次写入）

### 免费额度够用吗？

**假设**：
- 1000 个活跃用户
- 每个用户每天 10 次会话
- 每次会话 100 个消息

**计算**：
- 请求数：1000 × 10 × 100 = 100万/天
- Cloudflare 限制：10万请求/天（免费）

**结论**：
- ❌ 免费额度不够
- ✅ 但可以升级到 $5/月（1000万请求）

**实际**：
- 开源项目初期：几十个用户，免费够用
- 火了之后：可以考虑付费或者让用户自己部署

---

## 隐私和安全

### 你能看到什么？

**你能看到**：
- ✅ 有多少用户连接
- ✅ 连接时间和频率
- ✅ 消息数量（但不是内容）

**你看不到**：
- ❌ 消息内容（端到端加密）
- ❌ 用户IP（Cloudflare 隐藏）
- ❌ 用户数据

### 用户隐私保护

```javascript
// 端到端加密流程
用户消息 → AES加密 → Relay转发（乱码）→ AES解密 → Daemon

// Relay 只看到：
{
  "encrypted": "j3kd8f7g6h5j4k3l2m1n0..."
}
```

---

## 用户选择

### README.md 建议写法

```markdown
## 远程访问

### 方案 1: 官方 Relay（推荐）⭐⭐⭐⭐⭐

**零配置，扫码即用**

```bash
# config.yaml
remote_access:
  method: "official_relay"

# 运行
python server.py
# 扫码 → 完成
```

- ✅ 零配置
- ✅ 自动 HTTPS
- ✅ 全球访问
- ✅ 端到端加密
- ✅ 完全免费

### 方案 2: Tailscale（隐私优先）⭐⭐⭐⭐

完全点对点，不经过任何中继

### 方案 3: 自建 Relay（高级）

Fork relay 代码自己部署
```

---

## 对比 Paseo

| 特性 | Paseo Relay | Roboot Relay（你的）|
|------|-------------|---------------------|
| 架构 | Durable Objects | Durable Objects |
| 加密 | E2EE | E2EE |
| 成本 | 他们承担 | 你承担（$10/年起）|
| 控制 | Paseo 控制 | 你控制 |
| 用户体验 | 零配置 | 零配置 |

---

## 下一步

### Phase 1: 原型（1-2天）

1. 部署基础 Cloudflare Workers
2. 实现简单的消息转发
3. 测试连接

### Phase 2: 加密（1天）

1. 实现 ECDH 密钥交换
2. AES 加密/解密
3. 测试安全性

### Phase 3: 生产化（2-3天）

1. 错误处理
2. 连接管理
3. 监控和日志
4. 用户文档

**总时间**：1周完成

---

## 参考

- [Paseo Relay 源码](https://github.com/getpaseo/paseo/tree/main/packages/relay)
- [Cloudflare Durable Objects](https://developers.cloudflare.com/durable-objects/)
- [WebCrypto API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Crypto_API)
