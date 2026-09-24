# 部署到腾讯云 Lighthouse（轻量应用服务器）

面向 `Paper_Agent_Server`（FastAPI + SQLite，API 8000 / 看板 8010）。分两步走：

| 阶段 | 目标 | 用什么 | 什么时候做 |
| --- | --- | --- | --- |
| 一 | 先把功能跑通（**纯 IP + 明文 HTTP**） | `deploy/install.sh` | 现在就能做，不需要域名 |
| 二 | 换成域名 + HTTPS（桌面端正式对外） | `deploy/enable-https.sh` | 拿到域名（中国大陆地域需先完成 ICP 备案）之后 |

阶段一是**临时验证**：明文 HTTP 意味着登录令牌与用户代理密钥在公网可被中间人看到 ——
所以那一步必须把防火墙来源**限制在你自己的公网出口 IP**（脚本会提醒你），验证完立刻删掉规则。

---

## 0. 前置

1. 实例：**Ubuntu 22.04 / 24.04 系统镜像**（不要选「宝塔」面板镜像，本方案用 systemd + Caddy）。
2. 把代码放上服务器：`git clone <仓库>` 到 `/opt/paper-agent-server`，或用 Lighthouse 的**文件传输 / scp**。`deploy/` 目录要一起传。
3. 防火墙在 **Lighthouse 控制台**（实例 → 防火墙），和服务器里的 ufw 是两回事：
   - 阶段一：只加 `TCP 8000`，**来源填你的公网 IP/32**（查：`curl ifconfig.me`）
   - 阶段二：改成放通 `80` + `443`，把 8000 那条删掉
4. 建议在控制台开启**自动快照**（业务数据就是 `data/paper_agent.db` 这一个文件）。
5. 域名：中国大陆地域（北上广）用域名**必须先 ICP 备案**，否则 80/443 的域名访问会被拦；
   不想备案就选**香港 / 新加坡**地域（免备案，国内访问延迟略高，QQ 邮箱 SMTP 一样可用）。

---

## 1. 阶段一：IP 直连跑通

```bash
cd /opt/paper-agent-server
sudo bash deploy/install.sh
```

脚本做的事（**可重复执行**，已有的不会被覆盖）：

- 装 `python3-venv` → 建服务用户 `paperagent` → 建 `.venv` 并装依赖
- 生成 `/etc/paper-agent/paper-agent.env`（600 权限，随机看板口令会打印出来）
- 装并启动 `paper-agent-api` / `paper-agent-admin` / `paper-agent-backup.timer`
- 跑 `/health` 自检，最后打印「下一步」（防火墙规则、客户端该填什么）

**装完必须先做两件事**：

```bash
sudo nano /etc/paper-agent/paper-agent.env     # 填 SMTP_SENDER / SMTP_PASSWORD，否则收不到验证码
sudo systemctl restart paper-agent-api         # 改完重启
```

然后按控制台提示加防火墙规则，桌面端登录对话框「服务地址」填 `http://<公网IP>:8000`，
注册 → 登录 → 看模型下拉框里有没有内置模型。

看板（**不要**开 8000 之外的端口给公网）：

```bash
ssh -L 8010:127.0.0.1:8010 root@<公网IP>       # 本地转发，之后浏览器开 http://127.0.0.1:8010
```

### （可选）沿用本地已有的账号与密钥池

仓库里那份 `data/paper_agent.db` 通常已经配好了供应商密钥、注册过账号 —— 想在服务器上直接
接着用（而不是重配十把 Key），把它**按一致性快照**搬过去：

```bash
# 1) 本地：生成快照（WAL 模式下别直接拷文件）
python -c "import sqlite3;s=sqlite3.connect('data/paper_agent.db');d=sqlite3.connect('paper_agent-snapshot.db');s.backup(d);d.close();s.close()"
scp paper_agent-snapshot.db root@<公网IP>:/tmp/

# 2) 服务器：停服务 → 覆盖 → 清掉旧的 -wal/-shm → 修权限 → 起服务
cd /opt/paper-agent-server
sudo systemctl stop paper-agent-api paper-agent-admin
sudo cp /tmp/paper_agent-snapshot.db data/paper_agent.db
sudo rm -f data/paper_agent.db-wal data/paper_agent.db-shm
sudo chown paperagent:paperagent data/paper_agent.db
sudo systemctl start paper-agent-api paper-agent-admin
```

搬完可以去 `/etc/paper-agent/paper-agent.env` 把 `STORE_PLAIN_PASSWORD=false` 打开（环境变量
优先于库里的旧值），并在看板「请求日志」里清掉本地开发期攒下的记录。

---

## 2. 阶段二：域名 + HTTPS

域名解析（A 记录）指向实例公网 IP，等解析生效后：

```bash
cd /opt/paper-agent-server
# 只要 API（看板继续走 SSH 隧道）
sudo bash deploy/enable-https.sh api.example.com

# 或者顺带开看板域名，只允许你的出口 IP 访问
sudo bash deploy/enable-https.sh api.example.com admin.example.com 1.2.3.4
```

脚本做的事：装 Caddy → 生成 `/etc/caddy/Caddyfile`（自动申请 / 续期 Let's Encrypt）→
把 `API_HOST` 收回 `127.0.0.1`（8000 不再对外，防火墙那条规则可以删了）→
打开 `LOG_TRUST_PROXY`（反代后日志才记得到真实来源）→ 重启服务 → 打印自检命令。

验收：

```bash
curl -s https://api.example.com/health
curl -s https://api.example.com/api/llm/models
journalctl -u caddy -n 50 --no-pager      # 证书签发过程
```

桌面端「服务地址」改成 `https://api.example.com` 即完成迁移。

> 为什么用 Caddy：对话是 SSE 长连接，Caddy 默认不缓冲响应、直接透传。
> 换成 Nginx 必须自己补 `proxy_buffering off; proxy_read_timeout 600s; proxy_http_version 1.1;`，
> 少一样就会出现「回答卡半天、最后一次性吐出来」或中途 504。

---

## 3. 配置放哪

优先级：**环境变量 > `config.json` > 代码默认值**（`app/config.py`）。

| 放哪 | 放什么 |
| --- | --- |
| `/etc/paper-agent/paper-agent.env`（600） | SMTP 授权码、看板口令 `ADMIN_ACCESS_TOKEN`、`STORE_PLAIN_PASSWORD=false`、监听地址、日志开关 |
| `config.json`（640，别提交仓库） | 非机密项 + **`llm.models`（内置模型与供应商密钥播种）** |

两个容易踩的点：

- **`LLM_MODELS` 不能用环境变量配**：环境变量传进来是字符串，`app/config.py` 只认
  `config.json` 里的**列表**，写了会被静默忽略。模型 / 密钥要么写 `config.json` 的 `llm.models`
  （启动时幂等播种，密钥按 `api_key` 去重），要么登录看板手工加。
- **监听地址只由 env 决定**：两个 systemd 单元都用 `python run.py --only api|admin` 启动，
  所以 `enable-https.sh` 改写 `API_HOST` 之后就真的只监听本机了（不存在「单元里写死 0.0.0.0」的漏网）。

---

## 4. 日常运维

```bash
# 状态 / 日志
systemctl status paper-agent-api paper-agent-admin
journalctl -u paper-agent-api -f                 # 验证码、供应商报错都在这
journalctl -u paper-agent-admin -f

# 更新代码
cd /opt/paper-agent-server && sudo git pull
sudo -u paperagent .venv/bin/pip install -r requirements.txt
sudo systemctl restart paper-agent-api paper-agent-admin

# 备份（每天 04:30 自动跑，这里手工补一份）
sudo bash deploy/backup.sh                       # 产物在 /var/backups/paper-agent，默认留 14 天
systemctl list-timers paper-agent-backup

# 恢复：停服务 → 用备份覆盖 data/paper_agent.db（连带删掉 -wal / -shm）→ 启服务
sudo systemctl stop paper-agent-api paper-agent-admin
sudo cp /var/backups/paper-agent/paper_agent-2026-09-24-0430.db /opt/paper-agent-server/data/paper_agent.db
sudo rm -f /opt/paper-agent-server/data/paper_agent.db-wal /opt/paper-agent-server/data/paper_agent.db-shm
sudo chown paperagent:paperagent /opt/paper-agent-server/data/paper_agent.db
sudo systemctl start paper-agent-api paper-agent-admin

# 查谁在刷（看板接口，远端走 SSH 隧道时用 127.0.0.1:8010）
curl -H "X-Admin-Token: 你的口令" "http://127.0.0.1:8010/api/admin/logs?hours=24&limit=100"
```

---

## 5. 排障速查

| 现象 | 先看这里 |
| --- | --- |
| 客户端「无法连接服务端」 | 防火墙那条 8000/443 规则还在吗？`curl -v http://127.0.0.1:8000/health` 在本机通不通？`API_HOST` 是不是被收回 127.0.0.1 了（阶段一必须是 0.0.0.0） |
| 验证码收不到 | `journalctl -u paper-agent-api -f` 看 SMTP 报错；QQ 邮箱必须用**授权码**；临时可用 `SMTP_DRY_RUN=true`（验证码直接打在日志里） |
| 看板里来源 IP 全是 127.0.0.1 | `LOG_TRUST_PROXY` 没开（阶段二必须 true） |
| 对话要等很久才一次性吐出 | 前面挂了 Nginx 且没关 `proxy_buffering`；用 Caddy 或按上面注释补 Nginx 配置 |
| 证书申请失败 | 域名没解析到本机 / 80 端口没放通 / 用了未备案的大陆域名；`journalctl -u caddy -n 100` |
| 桌面端 HTTPS 报证书错误 | 用了 IP 或自签证书（客户端走系统信任链，必须是公共 CA 签的） |
| 服务起不来看不出原因 | `journalctl -u paper-agent-api -n 50`；多数是 `data/` 权限（必须是 `paperagent` 可写） |

---

## 6. 不买域名能不能长期跑？

能。三个阶段一就直接是 `http://<公网IP>:8000`，功能一点不少（注册 / 登录 / 对话 / 生图 / 生视频
都走它）。只是**明文 HTTP** 意味着登录令牌与用户代理密钥在公网上裸奔，按风险从高到低选：

| 做法 | 加密 | 适合 | 代价 |
| --- | --- | --- | --- |
| ① 纯 IP + 防火墙只放自己的出口 IP | ❌ 明文 | 自己临时验证 | 家宽 IP 会变，要常改规则；给别人用等于裸奔 |
| ② `sslip.io` / `nip.io` 拿真证书（**推荐，仍不需要买域名**） | ✅ | 服务器在**非大陆**地域 | 名字固定为 IP 变形；共享域名偶尔会撞 Let's Encrypt 限流（换另一个即可） |
| ③ Tailscale 组网（不需要任何公网端口） | ✅ | 自己 + 几个固定用户 | 每台客户端都要装并加入 tailnet，不适合公开分发 |
| ④ 便宜域名 + 非大陆地域 | ✅ | 要分发给别人 | 域名成本（约 10–40 元/年） |

**② 一条命令就能有 HTTPS**（`sslip.io` 会把 `1-2-3-4.sslip.io` 解析到 `1.2.3.4`，
所以 Let's Encrypt 能给你签发真证书，Caddy 自动搞定）：

```bash
sudo bash deploy/enable-https.sh 1-2-3-4.sslip.io      # 把 1-2-3-4 换成你 IP 的短横线写法
# 桌面端「服务地址」填：https://1-2-3-4.sslip.io
```

> ⚠️ 大陆地域的 80/443 对**未备案域名**会被拦，`sslip.io` 正属于未备案域名 —— 大陆实例
> 这条路大概率打不开，请走 ③ / ④，或老老实实备案。
> `nip.io` 是同类替代（`1.2.3.4.nip.io`），其中一个签发失败就换另一个。

**③ Tailscale**（连公网端口都不用开，最省心）：

```bash
# 服务器
curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
# 记下 tailscale 给的 100.x.y.z；Lighthouse 防火墙**不要**放开 8000（保持只开 22）
# API_HOST 保持 0.0.0.0（上面两个 systemd 单元由 env 控制），客户端填 http://100.x.y.z:8000
```

**客户端怎么改地址**：登录对话框的「服务地址」直接填即可（存进 `settings.ini` 的 `auth/serverUrl`）。
要让**所有人开箱就用你的地址**，得改客户端源码 `paper_agent/services/auth_client.py` 的
`DEFAULT_SERVER_URL` 再重新打包 —— 那是客户端仓库（`Paper_Agent`）的事。

## 7. 安全红线

1. **阶段一的明文窗口只对自己的 IP 开放**，验证完立刻删规则，尽快进阶段二。
2. `ADMIN_ACCESS_TOKEN` 必须是强口令（`install.sh` 已随机生成）；`/api/admin/*` 里有
   账号明文密码回显与供应商密钥「取明文」接口，**永远不要对全网开放看板**。
3. `STORE_PLAIN_PASSWORD=false`：看板不再需要明文密码副本。
4. `config.json` 与 `/etc/paper-agent/paper-agent.env` **都不要提交仓库 / 不要进快照分享**。
5. 想挡刷：域名挂 Cloudflare（免费），源站 443 只允许 Cloudflare 网段；
   或在高防/云厂商那层做限速 —— 本服务的请求日志是「事后查得出是谁」，不是替它挡流量。
