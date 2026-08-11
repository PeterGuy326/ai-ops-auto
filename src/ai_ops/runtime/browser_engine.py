"""浏览器引擎适配 — 把 settings.browser_engine 落到 subprocess 环境。

四档：
  - playwright_chromium       裸 Playwright Chromium，最易被识别（仅测试）
  - playwright_chrome_channel SAU 上游默认，channel="chrome" 用真 Chrome
  - patchright                drop-in 替换 Playwright Chromium，零侵入接入
  - camoufox                  Firefox 反检测之王，0% 检测率，但需要显式 launch

接入方式：
  patchright 通过 sitecustomize.py 注入 PYTHONPATH（subprocess 自动生效）
  camoufox   需要业务代码显式 import camoufox（见 publishers/xhs_camoufox.py）
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Collection

from ..config import settings


_INJECT_DIR = Path(__file__).parent / "stealth_inject"
_SUBPROCESS_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "PLAYWRIGHT_BROWSERS_PATH",
}


def build_subprocess_env(
    base_env: dict | None = None,
    proxy: str | None = None,
    *,
    include_configured_proxy: bool = True,
    inject_browser_runtime: bool = True,
    extra_allowlist: Collection[str] = (),
) -> dict:
    """生成最小 subprocess 环境，可按调用方选择注入 browser runtime。

    proxy: http://user:pass@host:port，会同时设到 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。

    ``extra_allowlist`` 只用于适配器声明其运行时必需的非密钥变量；调用方不得
    把控制面 API/LLM/Fernet 凭证加入其中。媒体/模型进程应将
    ``inject_browser_runtime`` 设为 False，避免加载 stealth ``sitecustomize``。
    """
    source = base_env if base_env is not None else os.environ
    # External browser tools do not need the control plane's API/LLM/Fernet
    # credentials.  An allowlist prevents ambient secrets from crossing the
    # subprocess boundary while preserving browser/display/proxy essentials.
    allowed = _SUBPROCESS_ENV_ALLOWLIST | set(extra_allowlist)
    env = {key: value for key, value in source.items() if key in allowed}
    env.update({"NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"})

    engine = settings.browser_engine
    if not inject_browser_runtime:
        # Explicitly drop ambient injection knobs even if a caller accidentally
        # adds PYTHONPATH/AI_OPS_STEALTH to its extra allowlist.
        env.pop("PYTHONPATH", None)
        env.pop("AI_OPS_STEALTH", None)
    elif engine == "patchright":
        # PYTHONPATH 前置 stealth_inject，让 sitecustomize 启动时被加载
        sep = os.pathsep
        env["PYTHONPATH"] = f"{_INJECT_DIR}{sep}{env.get('PYTHONPATH', '')}"
        env["AI_OPS_STEALTH"] = "patchright"
    elif engine == "camoufox":
        # 给上游一个标记，方便日志里识别
        env["AI_OPS_STEALTH"] = "camoufox"
    elif engine == "playwright_chrome_channel":
        # SAU/XHS Skills 上游默认行为，无需注入
        pass
    elif engine == "playwright_chromium":
        # 裸 Playwright，无注入
        pass

    effective_proxy = (
        proxy
        if proxy is not None
        else settings.browser_proxy if include_configured_proxy else ""
    )
    if effective_proxy:
        env["HTTP_PROXY"] = effective_proxy
        env["HTTPS_PROXY"] = effective_proxy
        env["ALL_PROXY"] = effective_proxy

    return env


def describe_engine() -> dict:
    """供 /health 或日志展示。"""
    return {
        "engine": settings.browser_engine,
        "headless": settings.browser_headless,
        "proxy_configured": bool(settings.browser_proxy),
        "stealth_inject_path": str(_INJECT_DIR),
    }
