"""OpenClaw gateway 端点解析（端口 / 主机 / URL 的唯一真相源）。

为什么需要这个模块：**OpenClaw 对非默认 profile 不用 18789**。Easel 用
``--profile easel`` 隔离自己的配置，于是走的是 OpenClaw 这段（dist/paths-*.mjs
``resolveGatewayPort``）：

    const profile = normalizeProfileName(env.OPENCLAW_PROFILE);
    if (!profile) return DEFAULT_GATEWAY_PORT;   // 18789，仅默认 profile
    let hash = 2166136261;                       // FNV-1a 32 位
    for (const byte of Buffer.from(profile, "utf8")) hash = Math.imul(hash ^ byte, 16777619) >>> 0;
    return 2e4 + hash % 4e4;                     // 20000 + hash % 40000

对 "easel" 算出来是 **37289**。

以前 web/app.py、doctor、ping、scripts/gateway.* 各自写死 18789，后果是三重的：

1. gateway 明明活着，面板却常驻「网关离线」（探 18789 拿不到连接，而真正的端口没人监听）；
2. ``gateway.sh status/start`` 同病 —— start 每次 --force 重启一个健康的 gateway；
3. 对话静默退回 CLI 传输（``_gateway_http_ready`` 探不到 HTTP 端点），每轮多付一次冷启动。

这里按 OpenClaw 自己的优先级解析，保证「Easel 连的端口 == gateway 监听的端口」：

    1. 环境变量 ``OPENCLAW_GATEWAY_PORT``（gateway 进程最认这个）/
       ``EASEL_GATEWAY_PORT``（Easel 自己的覆盖，gateway.sh 会透传给上面那个）
    2. ``<state dir>/openclaw.json`` 的 ``gateway.port``（onboard 会把解析结果写进去）
    3. profile 哈希兜底：``20000 + fnv1a32(profile) % 40000``，默认 profile 才是 18789

不复用 ``openclaw config get`` 子命令：那要付一次 CLI 冷启动（实测 1s+），而本模块在
``/api/status`` 这类每次刷新都调的热路径上。文件 + 哈希两层已逐步与 OpenClaw 对齐。

只依赖标准库（外加 ``openclaw_workspace.state_dir`` 的同约定复用），因为
``scripts/gateway.sh`` 会用系统 python3 直接 ``python3 -c`` 取端口 —— 不能有重依赖。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from easel.openclaw_workspace import PROFILE, state_dir

DEFAULT_GATEWAY_PORT = 18789
DEFAULT_GATEWAY_HOST = "127.0.0.1"
CONFIG_FILENAME = "openclaw.json"

# 与 OpenClaw 的 profile 哈希端口空间一致：20000 + hash % 40000
_PROFILE_PORT_BASE = 20_000
_PROFILE_PORT_SPAN = 40_000
_MAX_TCP_PORT = 65_535

# 环境变量优先级：OpenClaw 自己的在前 —— gateway 进程实际认的是它，Easel 只能跟随。
_PORT_ENV_KEYS = ("OPENCLAW_GATEWAY_PORT", "EASEL_GATEWAY_PORT")


def _as_port(text: str) -> int | None:
    """十进制串 → 合法端口（1..65535），否则 None。"""
    if not text.isdigit():
        return None
    port = int(text)
    return port if 0 < port <= _MAX_TCP_PORT else None


def parse_port_value(raw: str | None) -> int | None:
    """照抄 OpenClaw 的 ``parseGatewayPortEnvValue``：``18789`` / ``host:18789`` / ``[::1]:18789``。

    那三种写法 OpenClaw 都接受，Easel 也得接受 —— 否则用户照着 OpenClaw 文档设了
    ``OPENCLAW_GATEWAY_PORT=127.0.0.1:19001``，gateway 认、Easel 不认，又是端口错配。
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if trimmed.isdigit():
        return _as_port(trimmed)
    bracketed = re.fullmatch(r"\[[^\]]+\]:(\d+)", trimmed)
    if bracketed:
        return _as_port(bracketed.group(1))
    head, sep, suffix = trimmed.partition(":")
    if sep and head and ":" not in suffix:
        return _as_port(suffix)
    return None


def _env_port() -> tuple[int, str] | None:
    for key in _PORT_ENV_KEYS:
        port = parse_port_value(os.environ.get(key))
        if port is not None:
            return port, f"${key}"
    return None


def configured_port() -> int | None:
    """``<state dir>/openclaw.json`` 里的 ``gateway.port``；读不到 / 不合法 → None。

    只认 JSON number，与 OpenClaw 一致：那边 ``typeof configPort === "number"`` 才用，
    写成人肉字符串 ``"18789"`` 同样会被忽略。bool 虽然也是 int 的子类，但要排掉。
    """
    try:
        raw = (state_dir() / CONFIG_FILENAME).read_text(encoding="utf-8-sig")
        cfg = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    gateway = cfg.get("gateway")
    port = gateway.get("port") if isinstance(gateway, dict) else None
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    return port if 0 < port <= _MAX_TCP_PORT else None


def profile_port(profile: str = PROFILE) -> int:
    """OpenClaw 给非默认 profile 的确定性端口：``20000 + fnv1a32(profile) % 40000``。

    OpenClaw 的 normalizeProfileName 只在判断是否等于 "default" 时转小写比较，
    参与哈希的仍是原始大小写（见 profile-utils.ts）；这里必须照办，否则
    "Easel"/"easel" 会被当成同一个 profile，算出跟真实 gateway 不一致的端口。
    """
    name = (profile or "").strip()
    if not name or name.lower() == "default":
        return DEFAULT_GATEWAY_PORT
    digest = 2166136261
    for byte in name.encode("utf-8"):
        digest = ((digest ^ byte) * 16777619) & 0xFFFFFFFF
    return _PROFILE_PORT_BASE + digest % _PROFILE_PORT_SPAN


def resolve_port(profile: str = PROFILE) -> tuple[int, str]:
    """(端口, 来源说明)。来源说明只用于 doctor / ping / 启动日志的可观测性。"""
    from_env = _env_port()
    if from_env:
        return from_env
    from_config = configured_port()
    if from_config is not None:
        return from_config, CONFIG_FILENAME
    fallback = profile_port(profile)
    if fallback != DEFAULT_GATEWAY_PORT:
        return fallback, f"profile 哈希（{profile}）"
    return fallback, "默认端口"


def resolve_gateway_port(profile: str = PROFILE) -> int:
    return resolve_port(profile)[0]


def port_source(profile: str = PROFILE) -> str:
    return resolve_port(profile)[1]


def gateway_host() -> str:
    """gateway 主机。Easel 起 gateway 时固定 ``--bind loopback``，所以默认就是回环。"""
    host = (os.environ.get("EASEL_GATEWAY_HOST") or "").strip() or DEFAULT_GATEWAY_HOST
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"          # IPv6 字面量进 URL 得加方括号
    return host


def gateway_base_url(profile: str = PROFILE) -> str:
    return f"http://{gateway_host()}:{resolve_gateway_port(profile)}"


def healthz_url(profile: str = PROFILE) -> str:
    return f"{gateway_base_url(profile)}/healthz"


def chat_completions_url(profile: str = PROFILE) -> str:
    return f"{gateway_base_url(profile)}/v1/chat/completions"


def websocket_url(profile: str = PROFILE) -> str:
    """问答题桥接连的 gateway WS 端点（与 HTTP 侧同端口，别再单独维护一份）。"""
    return f"ws://{gateway_host()}:{resolve_gateway_port(profile)}"


def describe(profile: str = PROFILE) -> str:
    """一行诊断串，给 doctor / ping 用：``http://127.0.0.1:37289（profile 哈希）``。"""
    port, source = resolve_port(profile)
    return f"http://{gateway_host()}:{port}（{source}）"