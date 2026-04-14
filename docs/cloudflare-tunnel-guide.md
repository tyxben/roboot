# Cloudflare Tunnel 配置指南

## 前置要求

- ✅ Cloudflare 账号（免费）
- ✅ 一个域名（托管在 Cloudflare）
- ✅ Mac 已安装 Homebrew

## 步骤

### 1. 安装 cloudflared

```bash
brew install cloudflare/cloudflare/cloudflared
```

### 2. 登录 Cloudflare

```bash
cloudflared tunnel login
```

浏览器会打开，选择你的域名，授权。

### 3. 创建隧道

```bash
# 创建名为 roboot 的隧道
cloudflared tunnel create roboot

# 会生成隧道 ID 和凭证文件
# 凭证保存在：~/.cloudflared/<TUNNEL-ID>.json
```

### 4. 配置隧道

创建配置文件 `~/.cloudflared/config.yml`：

```yaml
tunnel: <你的TUNNEL-ID>
credentials-file: /Users/你的用户名/.cloudflared/<TUNNEL-ID>.json

ingress:
  # 将 roboot.你的域名.com 映射到本地 8765 端口
  - hostname: roboot.你的域名.com
    service: https://localhost:8765
    originRequest:
      noTLSVerify: true  # 因为本地是自签名证书
  
  # 默认规则（必需）
  - service: http_status:404
```

### 5. 添加 DNS 记录

```bash
# 自动添加 CNAME 记录
cloudflared tunnel route dns roboot roboot.你的域名.com
```

### 6. 启动隧道

```bash
# 测试运行
cloudflared tunnel run roboot

# 看到 "Connection established" 就成功了
```

### 7. 访问测试

手机浏览器访问：
```
https://roboot.你的域名.com
```

✅ 自动 HTTPS（Cloudflare 证书）
✅ 摄像头和语音可用
✅ 全球访问

---

## 后台运行

### 方法 1: 系统服务（推荐）

```bash
# 安装系统服务
sudo cloudflared service install

# 启动
sudo launchctl start com.cloudflare.cloudflared

# 开机自启
# （已自动配置）
```

### 方法 2: nohup（简单）

```bash
nohup cloudflared tunnel run roboot > /tmp/cloudflared.log 2>&1 &
```

---

## 集成到 Roboot

修改 `config.yaml`：

```yaml
remote_access:
  method: "cloudflare"
  cloudflare:
    tunnel_name: "roboot"
    domain: "roboot.你的域名.com"
    enabled: true
```

Roboot 启动时会检测 Cloudflare Tunnel 状态并显示访问地址。

---

## 成本

- **Cloudflare 账号**: 免费
- **Cloudflare Tunnel**: 免费（无限流量）
- **域名**: 
  - `.xyz`: ~$1/年（首年）
  - `.com`: ~$10/年
  - `.tech`: ~$5/年

**总成本**: $1-10/年

---

## 优缺点

### ✅ 优点
- 自动 HTTPS（CA 签名证书）
- 无需公网 IP
- 无需端口转发
- 全球 CDN 加速
- DDoS 防护

### ⚠️ 缺点
- 需要域名（有成本）
- 依赖 Cloudflare 服务
- 配置略复杂（但只需一次）

---

## 故障排查

### 隧道连接失败

```bash
# 查看日志
cloudflared tunnel info roboot

# 检查状态
sudo launchctl list | grep cloudflare
```

### DNS 未生效

```bash
# 检查 DNS 记录
dig roboot.你的域名.com

# 等待 DNS 传播（最多 5 分钟）
```

### 证书错误

确保 `config.yml` 中有 `noTLSVerify: true`（因为本地是自签名证书）

---

## 安全建议

1. **启用 Cloudflare Access**（可选）
   - 添加登录验证
   - 只有你能访问

2. **IP 白名单**（可选）
   - Cloudflare 防火墙规则
   - 限制访问 IP

3. **Rate Limiting**
   - 防止滥用
   - Cloudflare 免费版已包含

---

## 参考

- [Cloudflare Tunnel 官方文档](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)
- [域名购买推荐](https://www.namecheap.com/)
