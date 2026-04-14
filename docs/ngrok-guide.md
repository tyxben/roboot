# Ngrok 快速指南

## 什么是 Ngrok？

临时内网穿透工具，5分钟让你的 Roboot 暴露到公网。

**适合场景**：
- ✅ 临时演示
- ✅ 快速测试
- ✅ 不想买域名
- ❌ 长期使用（URL 每次变化）

---

## 快速开始

### 1. 安装 Ngrok

```bash
brew install ngrok
```

### 2. 注册账号（免费）

访问：https://ngrok.com/
- 注册账号（免费）
- 复制 authtoken

### 3. 配置 authtoken

```bash
ngrok config add-authtoken <你的token>
```

### 4. 启动 Ngrok

```bash
# 启动 Roboot
python server.py

# 新开一个终端，启动 ngrok
ngrok http https://localhost:8765

# 或者直接用 HTTP（如果不需要摄像头/语音）
ngrok http 8765
```

### 5. 看到输出

```
ngrok

Session Status                online
Account                       你的邮箱
Forwarding                    https://abc123.ngrok-free.app -> https://localhost:8765

✅ 复制这个 URL！
```

### 6. 手机访问

```
https://abc123.ngrok-free.app
```

- ✅ 自动 HTTPS
- ✅ 摄像头/语音可用
- ✅ 全球可访问

---

## 免费版限制

- ❌ URL 每次重启都变化（`abc123` 会变成 `xyz789`）
- ❌ 每分钟 40 个请求
- ❌ 每个账号 1 个在线隧道
- ⚠️ 访问时有 Ngrok 警告页（需要点击继续）

---

## 付费版（可选）

**Ngrok Pro**: $8/月
- ✅ 固定子域名（`roboot.ngrok.app`）
- ✅ 无警告页
- ✅ 更多隧道

---

## 集成到 Roboot

### 自动启动脚本

创建 `scripts/start_with_ngrok.sh`：

```bash
#!/bin/bash

# 后台启动 Roboot
python server.py &
ROBOOT_PID=$!

# 等待 Roboot 启动
sleep 2

# 启动 ngrok
ngrok http https://localhost:8765

# Ctrl+C 时清理
trap "kill $ROBOOT_PID" EXIT
```

使用：
```bash
chmod +x scripts/start_with_ngrok.sh
./scripts/start_with_ngrok.sh
```

### Python 集成（高级）

```python
# server.py 添加
import subprocess
import re

def start_ngrok():
    """启动 ngrok 并获取公网 URL"""
    process = subprocess.Popen(
        ["ngrok", "http", "https://localhost:8765", "--log=stdout"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # 解析 URL
    for line in process.stdout:
        if "url=" in line:
            url = re.search(r'url=(https://[^\s]+)', line)
            if url:
                return url.group(1)
    
    return None

# 可选启动
if config.get("ngrok", {}).get("enabled"):
    ngrok_url = start_ngrok()
    print(f"\n🌍 Ngrok URL: {ngrok_url}")
```

---

## 对比其他方案

| 特性 | Ngrok | Cloudflare Tunnel | Tailscale |
|------|-------|-------------------|-----------|
| **成本** | 免费/付费 | 免费（需域名） | 免费 |
| **配置时间** | 1分钟 | 15分钟 | 5分钟 |
| **固定 URL** | 付费 | ✅ | ✅ (IP) |
| **HTTPS** | ✅ | ✅ | ✅ |
| **稳定性** | 好 | 很好 | 很好 |
| **适合场景** | 临时演示 | 长期使用 | 个人使用 |

---

## 安全提示

⚠️ **Ngrok 会暴露你的服务到公网**

**建议**：
1. 只在需要时启动
2. 演示完后立即关闭（Ctrl+C）
3. 不要分享 URL 给不信任的人
4. 考虑添加认证（Phase 2）

---

## 故障排查

### "authtoken 未配置"

```bash
ngrok config add-authtoken <你的token>
```

### "tunnel 创建失败"

- 检查网络连接
- 检查是否已有 ngrok 进程在运行：
  ```bash
  killall ngrok
  ```

### 访问慢

- Ngrok 免费版服务器在美国
- 中国访问可能较慢
- 考虑用 Cloudflare Tunnel（国内 CDN）

---

## 参考

- [Ngrok 官网](https://ngrok.com/)
- [Ngrok 文档](https://ngrok.com/docs)
- [定价](https://ngrok.com/pricing)
