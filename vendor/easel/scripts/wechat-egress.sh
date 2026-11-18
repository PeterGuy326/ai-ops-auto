#!/usr/bin/env bash
# 公众号专用固定出口代理：独立的第二个 mihomo 实例，单节点全局，只给公众号 API 用。
# 目的：公众号 IP 白名单需要固定出口 IP，而本机默认出口是共享 NAT 轮询池。
# 本实例把流量钉死到机场的一个固定节点 → 微信看到固定 IP，可加白名单。
#
# 配置目录 /etc/mihomo-wechat（由本仓库调试时生成：抽主实例 /etc/mihomo 的一个健康节点 +
# geodata 文件 + dns 段，mode=rule 且 MATCH 全部走该节点）。
# 用法：bash scripts/wechat-egress.sh {start|stop|status|ip}
# 起 web 时：export WECHAT_EGRESS_PROXY=http://127.0.0.1:7899 后再 easel web。

set -euo pipefail
PORT=7899
DIR=/etc/mihomo-wechat
BIN=/usr/local/bin/mihomo
LOG=/var/log/mihomo-wechat.log

case "${1:-status}" in
  start)
    fuser -k ${PORT}/tcp 2>/dev/null || true   # 按端口杀，勿用 pkill -f（会误杀本命令自身）
    sleep 1
    nohup "$BIN" -d "$DIR" > "$LOG" 2>&1 &
    sleep 4
    ss -tlnp 2>/dev/null | grep -q ":${PORT}" && echo "started on 127.0.0.1:${PORT}" || { echo "FAILED, log:"; tail -n 8 "$LOG"; exit 1; }
    ;;
  stop)
    fuser -k ${PORT}/tcp 2>/dev/null || true
    echo "stopped"
    ;;
  status)
    ss -tlnp 2>/dev/null | grep ":${PORT}" || echo "not running"
    ;;
  ip)
    # 打印微信侧看到的出口 IP（用无效 appid，只看 40164 回显的 IP）
    curl -sS -m 20 -x "http://127.0.0.1:${PORT}" \
      "https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=probe&secret=probe" 2>/dev/null \
      | python3 -c "import sys,json,re;d=json.load(sys.stdin);m=re.search(r'invalid ip ([0-9.]+)',d.get('errmsg',''));print(m.group(1) if m else d.get('errmsg','?'))"
    ;;
  *)
    echo "用法: bash scripts/wechat-egress.sh {start|stop|status|ip}"; exit 1 ;;
esac
