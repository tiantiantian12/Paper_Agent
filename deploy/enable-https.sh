#!/usr/bin/env bash
# Paper Agent Server —— 阶段二：拿到域名后切入 HTTPS。
#
#   sudo bash deploy/enable-https.sh api.example.com                          # 只对外提供 API
#   sudo bash deploy/enable-https.sh api.example.com admin.example.com 1.2.3.4 # 另开看板，仅白名单 IP 可访问
#
# 做的事：装 Caddy → 写反代（自动申请 / 续期 Let's Encrypt 证书）→
#       把 API 收回 127.0.0.1（8000 不再对外）→ 打开日志的真实 IP 归因（LOG_TRUST_PROXY）。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE=/etc/paper-agent/paper-agent.env
CADDY_FILE=/etc/caddy/Caddyfile

DOMAIN="${1:-}"
ADMIN_DOMAIN="${2:-}"
ALLOW_IP="${3:-}"

ok()   { printf '\033[32m[deploy]\033[0m %s\n' "$*"; }
note() { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/enable-https.sh <api域名> [看板域名] [白名单IP]"
[[ -n "$DOMAIN" ]] || die "用法：sudo bash deploy/enable-https.sh api.你的域名 [admin.你的域名] [你的公网IP]"
[[ -f "$ENV_FILE" ]] || die "先跑阶段一：sudo bash deploy/install.sh"

if [[ -n "$ADMIN_DOMAIN" ]]; then
  [[ -n "$ALLOW_IP" ]] || die "要看板域名就必须给白名单：… enable-https.sh $DOMAIN $ADMIN_DOMAIN 你的公网IP
（或不要看板站点，直接 SSH 隧道：ssh -L 8010:127.0.0.1:8010 root@服务器IP）"
  [[ "$ALLOW_IP" =~ ^[0-9a-fA-F:.]+(/[0-9]{1,3})?$ ]] || warn "白名单「$ALLOW_IP」看着不像 IP / CIDR，生成后请检查 $CADDY_FILE"
fi

# ---------------------------------------------------------------- 1) 装 Caddy
if ! command -v caddy >/dev/null 2>&1; then
  note "安装 Caddy（官方 apt 源）…"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
else
  ok "Caddy 已安装：$(caddy version | head -1)"
fi

# ---------------------------------------------------------------- 2) 生成 Caddyfile
note "生成 $CADDY_FILE …"
if [[ -n "$ADMIN_DOMAIN" ]]; then
  sed -e "s|__DOMAIN__|$DOMAIN|g" \
      -e "s|__ADMIN_DOMAIN__|$ADMIN_DOMAIN|g" \
      -e "s|__ADMIN_ALLOW_IP__|$ALLOW_IP|g" \
      "$APP_DIR/deploy/Caddyfile.example" > "$CADDY_FILE"
else
  awk '
    /^# >>>ADMIN-BLOCK/ {skip=1; next}
    /^# <<<ADMIN-BLOCK/ {skip=0; next}
    !skip
  ' "$APP_DIR/deploy/Caddyfile.example" | sed -e "s|__DOMAIN__|$DOMAIN|g" > "$CADDY_FILE"
  note "未提供看板域名：只配 API 站点，看板继续走 SSH 隧道"
fi
chmod 644 "$CADDY_FILE"

caddy validate --config "$CADDY_FILE" >/dev/null || { cat "$CADDY_FILE"; die "Caddyfile 校验没过（上面是生成的内容）"; }

# ---------------------------------------------------------------- 3) 收回本机 + 真实 IP 归因
set_env() {   # set_env KEY VALUE —— 去重后在文件末尾追加（systemd 取最后一条）
  local key="$1" value="$2"
  sed -i "/^[[:space:]]*#\?[[:space:]]*${key}=/d" "$ENV_FILE"
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}
cp "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
set_env API_HOST 127.0.0.1
set_env LOG_TRUST_PROXY true
ok "已把 API 收回 127.0.0.1，并打开 LOG_TRUST_PROXY（反代后日志才记得到真实来源）"

systemctl daemon-reload
systemctl restart paper-agent-api.service paper-agent-admin.service
systemctl enable --now caddy >/dev/null 2>&1
systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 2

# ---------------------------------------------------------------- 4) 自检
if systemctl is-active --quiet paper-agent-api; then ok "API 已重启（本机 8000）"; else journalctl -u paper-agent-api -n 40 --no-pager; die "API 没起来"; fi

API_PORT_NOW="$(sed -n 's/^API_PORT=//p' "$ENV_FILE" | tail -1)"; API_PORT_NOW="${API_PORT_NOW:-8000}"

if [[ -n "$ADMIN_DOMAIN" ]]; then
  ADMIN_NOTE="
看板：https://$ADMIN_DOMAIN —— 只有白名单 IP（$ALLOW_IP）能打开，其余返回 403"
else
  ADMIN_NOTE="
看板仍走 SSH 隧道（不占公网端口）：ssh -L 8010:127.0.0.1:8010 root@服务器IP"
fi

cat <<EOF

────────────────────────────────────────────────────────────────
Caddy 已加载，接下来只差防火墙与证书签发
────────────────────────────────────────────────────────────────
1) 轻量应用服务器控制台 → 防火墙：放通 80 / 443（来源 0.0.0.0/0），
   并把阶段一那条 $API_PORT_NOW 端口的规则**删掉** —— 服务已经不听公网了，规则留着只是风险。

2) 证书签发要求域名已解析到本机、且 80/443 可达；第一次访问会触发签发：
     curl -s https://$DOMAIN/health
     curl -s https://$DOMAIN/api/llm/models
   看签发过程：journalctl -u caddy -n 50 --no-pager

3) 桌面端登录对话框「服务地址」改成：https://$DOMAIN （把之前的 http://IP:$API_PORT_NOW 换掉）$ADMIN_NOTE

4) 日志归因自检：随便发一次请求，看板「请求日志」里来源 IP 应该是你自己的公网 IP，
   而不是 127.0.0.1；如果还是 127.0.0.1，查这两处：
     grep -E '^(API_HOST|LOG_TRUST_PROXY)=' $ENV_FILE
     systemctl show paper-agent-api -p Environment
EOF
