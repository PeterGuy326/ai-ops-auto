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

from ..config import settings


_INJECT_DIR = Path(__file__).parent / "stealth_inject"


def build_subprocess_env(base_env: dict | None = None, proxy: str | None = None) -> dict:
    """生成 subprocess 启动用的 env，注入 stealth + proxy。

    proxy: http://user:pass@host:port，会同时设到 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。
    """
    env = dict(base_env or os.environ)

    engine = settings.browser_engine
    if engine == "patchright":
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

    effective_proxy = proxy or settings.browser_proxy
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
