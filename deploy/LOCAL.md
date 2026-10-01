# 本机当服务器 + 免域名暴露到公网

适用场景：自己有一台电脑，不想买云服务器 / 域名，想让别人也能访问你的 Paper Agent
服务（注册登录 + 内置模型代理 + 下载客户端）。

整体思路：**服务端跑在你本机**，用一条「出站隧道」把本机的 `8000` 端口转出去，
不需要公网 IP、不需要路由器端口映射、也不需要域名。

---

## 1. 本机启动服务端

```bash
cd Paper_Agent_Server
pip install -r requirements.txt
python run.py --only api        # 只起 8000（落地页 + API 都在这）
# 或 python run.py              # 同时起 8000 与 8010 看板
```

首次启动会建库、播种内置模型，并生成一个随机看板口令（留意终端打印）。
接着配置发信：

```bash
sudo nano /etc/paper-agent/paper-agent.env    # 没有就直接改 config.json 的 smtp.*
# 填 SMTP_SENDER / SMTP_PASSWORD（QQ 邮箱 SMTP 授权码，不是登录密码）
```

## 2. 准备客户端安装包

```bash
cd Paper_Agent
pip install -r requirements.txt pyinstaller
python build_exe.py
copy dist\Paper_Agent.exe ..\Paper_Agent_Server\data\downloads\Paper_Agent.exe
```

装好后落地页的「下载」按钮会自动可用（`/api/site/info` 的 `download_ready` 变 `true`）。

## 3. 本机自检

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/site/info
# 浏览器打开 http://127.0.0.1:8000/ 应看到落地页
```

## 4. 暴露到公网（二选一）

### 方案 A：Cloudflare Tunnel（最省事，HTTPS 免域名）

```bash
# 安装 cloudflared 后
cloudflared tunnel --url http://localhost:8000
# 终端打印 https://xxxx.trycloudflare.com —— 把这个地址发给别人
```

- 对方在桌面端登录框的「服务地址」填这个 `https://xxxx.trycloudflare.com` 即可注册登录、下载客户端。
- **缺点**：每次重启 `cloudflared` 地址会变（快速隧道是临时的）。要固定地址需在 CF 挂一个域名做命名隧道。
- 背后是 CF 海外节点，**完全不用备案**。

### 方案 B：Tailscale（地址稳定、全程加密）

```bash
# 本机装 Tailscale 并上线：https://tailscale.com/download
tailscale up
tailscale ip -4                 # 得到 100.x.y.z
# 把 http://100.x.y.z:8000 发给同样装了 Tailscale 并加入你网络的人
```

适合自己 + 几个固定朋友；不适合公开分发（每人都得装客户端并加入你的网络）。

## 5. 安全红线

- **只暴露 8000，永远不要把 8010 看板转出去。** 看板里能看到用户**明文密码**
  （`account.store_plain_password` 默认 `true`），隧道只映射 `localhost:8000`。
- 面向公网前建议把 `account.store_plain_password` 设为 `false`。
- 前面挂了 Cloudflare / Nginx 时，把 `config.json` 的 `log.trust_proxy` 设为 `true`，
  否则日志里记的来源 IP 会是隧道自己的地址。
- 免费隧道的地址会变、且大陆访问 CF 海外节点延迟偏高；追求稳定与低延迟，
  长期还是建议用一台大陆服务器（IP + 8000 即可，无需域名 / 备案）。
