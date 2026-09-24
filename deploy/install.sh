#!/usr/bin/env bash
# Paper Agent Server —— 阶段一部署：先把服务在 Lighthouse(Ubuntu) 上跑起来，用 IP 直连验证功能。
# 域名 + HTTPS 见 deploy/README.md 的「阶段二」，一条命令就能切过去（deploy/enable-https.sh）。
#
#   sudo bash deploy/install.sh
#
# 可重复执行：已存在的虚拟环境 / 配置文件不会被覆盖。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${PAPER_AGENT_USER:-paperagent}"
ENV_DIR=/etc/paper-agent
ENV_FILE="$ENV_DIR/paper-agent.env"
SYSTEMD_DIR=/etc/systemd/system

ok()   { printf '\033[32m[deploy]\033[0m %s\n' "$*"; }
note() { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/install.sh"
[[ -f "$APP_DIR/run.py" ]] || die "在当前目录找不到 run.py —— 请在 Paper_Agent_Server 根目录执行本脚本"

# ---------------------------------------------------------------- 1) 系统依赖
command -v python3 >/dev/null || die "没找到 python3（Ubuntu: apt install -y python3 python3-venv）"
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  note "安装 python3-venv …"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
fi

# ---------------------------------------------------------------- 2) 服务用户
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  note "创建服务用户 $RUN_USER（无登录 shell）"
  useradd -r -s /usr/sbin/nologin "$RUN_USER"
fi

# ---------------------------------------------------------------- 3) 虚拟环境
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  note "创建虚拟环境 $APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv"
fi
note "安装依赖 …"
"$APP_DIR/.venv/bin/pip" install -q -U pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------- 4) 目录与权限
mkdir -p "$APP_DIR/data"
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
# 备份脚本要被 systemd 直接执行（scp 上传的代码没有执行位）
chmod +x "$APP_DIR"/deploy/*.sh
if [[ -f "$APP_DIR/config.json" ]]; then
  chmod 640 "$APP_DIR/config.json"          # 里面可能有 SMTP 授权码等机密
fi

# ---------------------------------------------------------------- 5) 运行时配置
mkdir -p "$ENV_DIR" && chmod 700 "$ENV_DIR"
if [[ -f "$ENV_FILE" ]]; then
  ok "$ENV_FILE 已存在，保持不动"
else
  token="$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
  sed "s|__ADMIN_TOKEN__|$token|" "$APP_DIR/deploy/paper-agent.env.example" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "已生成 $ENV_FILE"
  warn "看板口令（记下来，登录 /etc/paper-agent/paper-agent.env 里也能改）：ADMIN_ACCESS_TOKEN=$token"
fi

# 机密项没填就提醒一下（SMTP 不填发不出验证码，登不上号）
grep -qE '^SMTP_SENDER=你的发件QQ邮箱@qq.com' "$ENV_FILE" && warn "还没填 SMTP_SENDER / SMTP_PASSWORD —— 注册验证码发不出去，编辑 $ENV_FILE 后重启服务"
grep -qE '^ADMIN_ACCESS_TOKEN=(__ADMIN_TOKEN__)?$' "$ENV_FILE" && warn "ADMIN_ACCESS_TOKEN 还是空的：公网上别这么跑，看板页会免口令"

# 顺手体检 config.json（只看不写）
if [[ -f "$APP_DIR/config.json" ]]; then
  "$APP_DIR/.venv/bin/python" - "$APP_DIR/config.json" <<'PY' || true
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(0)
token = str(cfg.get("admin_access_token") or "")
plain = (cfg.get("account") or {}).get("store_plain_password")
if token in ("admin123", "123456"):
    print(f"[deploy] ⚠️ config.json 里 admin_access_token 还是默认值 {token}，建议置空（改用环境变量）或换成强口令")
if plain is True:
    print("[deploy] ⚠️ config.json 里 account.store_plain_password=true：生产环境建议改成 false（看板不再展示明文密码）")
PY
fi

# ---------------------------------------------------------------- 6) systemd
note "安装 systemd 服务 …"
for unit in paper-agent-api paper-agent-admin paper-agent-backup; do
  sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__RUN_USER__|$RUN_USER|g" \
      "$APP_DIR/deploy/systemd/$unit.service" > "$SYSTEMD_DIR/$unit.service"
done
install -m 644 "$APP_DIR/deploy/systemd/paper-agent-backup.timer" "$SYSTEMD_DIR/paper-agent-backup.timer"
systemctl daemon-reload
systemctl enable --now paper-agent-api.service paper-agent-admin.service >/dev/null 2>&1
systemctl enable --now paper-agent-backup.timer >/dev/null 2>&1

# ---------------------------------------------------------------- 7) 自检
sleep 2
API_PORT_NOW="$(sed -n 's/^API_PORT=//p' "$ENV_FILE" | tail -1)"; API_PORT_NOW="${API_PORT_NOW:-8000}"
if systemctl is-active --quiet paper-agent-api; then
  HEALTH="$(curl -s --max-time 5 "http://127.0.0.1:$API_PORT_NOW/health" || true)"
  [[ -n "$HEALTH" ]] && ok "API 健康检查：$HEALTH" || warn "API 进程活着但 /health 没响应，看 journalctl -u paper-agent-api -n 50"
else
  journalctl -u paper-agent-api -n 40 --no-pager || true
  die "API 没起来（上面是最近 40 行日志）"
fi
if systemctl is-active --quiet paper-agent-admin; then
  ok "看板已在本机 8010 端口起好"
else
  warn "看板没起来：journalctl -u paper-agent-admin -n 40 --no-pager"
fi

PUBLIC_IP="$(curl -s --max-time 5 https://ifconfig.me || true)"
cat <<EOF

────────────────────────────────────────────────────────────────
下一步（阶段一：用 IP 直连验证功能）
────────────────────────────────────────────────────────────────
1) 轻量应用服务器控制台 → 防火墙：加一条规则
     TCP  $API_PORT_NOW    来源：你的公网出口 IP/32   ← 别开 0.0.0.0/0，明文只给自己的 IP
   （查自己的出口 IP：curl ifconfig.me）

2) 桌面端登录对话框「服务地址」填：   http://${PUBLIC_IP:-<服务器公网IP>}:$API_PORT_NOW
   然后：发验证码 → 注册 → 登录 → 看模型下拉框是否出现内置模型

3) 看板（本机，不进公网）：
     ssh -L 8010:127.0.0.1:8010 root@${PUBLIC_IP:-<服务器公网IP>}
     浏览器打开 http://127.0.0.1:8010   （口令见 $ENV_FILE 的 ADMIN_ACCESS_TOKEN）

4) 验证完把防火墙那条 $API_PORT_NOW 规则删掉，然后走阶段二上 HTTPS：
     域名解析到本机后 →  sudo bash deploy/enable-https.sh api.你的域名 [admin.你的域名] [你的公网IP]

常用排障命令：
  systemctl status paper-agent-api paper-agent-admin
  journalctl -u paper-agent-api -f          # 实时日志（验证码、报错都在这）
  bash deploy/backup.sh                     # 手工备份一次数据
EOF
