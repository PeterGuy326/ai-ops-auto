"""零侵入注入 patchright 替代 playwright。

机制：
  Python 启动时会自动 import sitecustomize（如果 PYTHONPATH 能找到）。
  我们把这个文件的目录塞到 subprocess 的 PYTHONPATH 最前面，
  外部工具 (SAU/XHS Skills) 启动时 import playwright 实际拿到 patchright。

效果：
  零修改外部工具源码，drop-in 反检测。

启用条件：
  环境变量 AI_OPS_STEALTH=patchright
"""
import os
import sys


def _activate_patchright() -> None:
    try:
        import patchright
        import patchright.async_api
        import patchright.sync_api
    except ImportError:
        # patchright 没装，静默不注入（不影响主流程）
        return

    # 模块别名：让 import playwright 实际命中 patchright
    sys.modules.setdefault("playwright", patchright)
    sys.modules.setdefault("playwright.async_api", patchright.async_api)
    sys.modules.setdefault("playwright.sync_api", patchright.sync_api)
    print("[ai-ops stealth] playwright -> patchright (drop-in)", file=sys.stderr)


_engine = os.environ.get("AI_OPS_STEALTH", "")
if _engine == "patchright":
    _activate_patchright()
elif _engine == "camoufox":
    # Camoufox 不是 Playwright drop-in，无法用 sitecustomize 注入。
    # 需要在业务代码里显式用 from camoufox.async_api import AsyncCamoufox。
    # 见 publishers/xhs_camoufox.py 的实现路线。
    print(
        "[ai-ops stealth] camoufox 模式：需要显式 launch，不走 sitecustomize",
        file=sys.stderr,
    )
