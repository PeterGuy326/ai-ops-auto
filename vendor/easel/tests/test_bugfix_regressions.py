"""Regression tests for the confirmed bug-fix round.

每个用例对应一个已确认（可复现/可推理验证）并已修复的 bug。命名即 bug 描述。
覆盖：
- web/app.py   ：job 事件回放跳过坏行；登出终止仍在跑的登录进程；schedule 锁单例
- doctor/ping  ：healthz 回环探针绕过继承的 http_proxy
- video_ops    ：cut 的 start+end 换算成 -t(end-start)；_tsec 解析
- intro_outro  ：card 的 logo 恒为输入 1（--bg-image 不再多 +1）
- content_guard：私网 IP 正则不再吞 3 段版本号（macOS 10.15.7）
- image_ops    ：compress 低 quality + --max-kb 不再 TypeError
- gemini 适配器：非流式 finishReason 映射与流式一致
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.app as web

SHARED = Path(__file__).resolve().parents[1] / "skills" / "shared" / "scripts"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── web/app.py：_read_job_events 单行损坏不应丢弃整个事件流 ───────────
def test_read_job_events_skips_torn_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "SESSIONS_DIR", tmp_path / "sessions")
    jobs = tmp_path / "sessions" / "jobs"
    jobs.mkdir(parents=True)
    path = web._job_event_file("turn-1")
    lines = [
        json.dumps({"id": 1, "event": "activity", "data": "收到"}),
        json.dumps({"id": 2, "event": "token", "data": "你好"}),   # torn line below
        '{"id": 3, "event": "tok',
        json.dumps({"id": 4, "event": "done", "data": None}),
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = web._read_job_events("turn-1")
    assert [e["id"] for e in events] == [1, 2, 4]
    assert [e["id"] for e in web._read_job_events("turn-1", after=2)] == [4]


# ── web/app.py：api_logout 应终止仍在跑的登录进程（防 success 标记写回） ──
class _FakeLoginProc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None            # 永远“还在跑”

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def test_logout_terminates_inflight_login_runner(tmp_path, monkeypatch):
    platform = "kuaishou"
    monkeypatch.setattr(web, "LOGIN_RUNNERS", {
        platform: {"name": "快手", "backend": "web", "wp": "kuaishou", "profile": "KuaishouProfile"}})
    monkeypatch.setattr(web, "BROWSER_PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(web, "LOGIN_DIR", tmp_path / "_login")
    (tmp_path / "profiles" / "KuaishouProfile").mkdir(parents=True, exist_ok=True)
    fake = _FakeLoginProc()
    monkeypatch.setattr(web, "LOGIN_PROCESSES", {platform: fake})
    result = asyncio.run(web.api_logout(platform))
    assert result["ok"] is True
    assert fake.terminated is True, "登出必须停掉仍在跑的登录进程，否则它会把 success 标记写回"
    assert platform not in web.LOGIN_PROCESSES


# ── web/app.py：_schedule.json 读-改-写必须有共享锁 ─────────────────────
def test_schedule_lock_is_a_shared_singleton():
    lock = web._schedule_lock()
    assert callable(getattr(lock, "acquire", None)) and callable(getattr(lock, "release", None))
    assert web._schedule_lock() is lock, "发布回流与增删改端点必须共用同一把锁"


def test_schedule_write_endpoints_hold_shared_lock(monkeypatch):
    class GuardedLock:
        depth = 0

        def __enter__(self):
            self.depth += 1
            return self

        def __exit__(self, *args):
            self.depth -= 1

    lock = GuardedLock()
    store = [{"id": "item-1", "title": "旧标题", "date": "2026-10-18", "kind": "content", "status": "idea"}]

    def guarded_read():
        assert lock.depth > 0, "读取排期参与 read-modify-write 时必须先持有共享锁"
        return [dict(item) for item in store]

    def guarded_write(items):
        assert lock.depth > 0, "写入排期参与 read-modify-write 时必须先持有共享锁"
        store[:] = [dict(item) for item in items]

    monkeypatch.setattr(web, "_schedule_lock", lambda: lock)
    monkeypatch.setattr(web, "_read_schedule", guarded_read)
    monkeypatch.setattr(web, "_write_schedule", guarded_write)
    monkeypatch.setattr(web.uuid, "uuid4", lambda: SimpleNamespace(hex="new-item-123456"))

    created = asyncio.run(web.api_schedule_create(web.ScheduleItem(
        title="新内容", date="2026-10-19", platform="xhs")))
    assert created["id"] == "new-item-123"
    assert [item["id"] for item in store] == ["item-1", "new-item-123"]

    updated = asyncio.run(web.api_schedule_update("item-1", web.ScheduleItem(
        title="新标题", date="2026-10-20", status="scheduled")))
    assert updated["title"] == "新标题"
    assert store[0]["status"] == "scheduled"

    deleted = asyncio.run(web.api_schedule_delete("new-item-123"))
    assert deleted == {"ok": True, "deleted": "new-item-123"}
    assert [item["id"] for item in store] == ["item-1"]


# ── gateway healthz：回环探针不得走继承的 http_proxy ────────────────────
def test_gateway_healthz_bypasses_inherited_proxy(monkeypatch):
    from easel.commands import doctor

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        monkeypatch.setattr(doctor, "healthz_url",
                             lambda: f"http://127.0.0.1:{port}/healthz")
        # 黑洞代理：若探针继承系统代理，会连到 10.255.255.1:9 并超时 → 误报 FAIL
        monkeypatch.setenv("http_proxy", "http://10.255.255.1:9")
        monkeypatch.setenv("HTTP_PROXY", "http://10.255.255.1:9")
        monkeypatch.setenv("https_proxy", "http://10.255.255.1:9")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        assert doctor._gateway_healthy() is True

        monkeypatch.setattr(web, "healthz_url",
                             lambda: f"http://127.0.0.1:{port}/healthz")
        assert web.check_gateway() is True
    finally:
        server.shutdown()
        server.server_close()


# ── video_ops：cut 的 --start+--end 必须产出 [start,end) 片段 ────────────
def test_video_cut_start_end_duration_math(tmp_path, monkeypatch):
    video_ops = _load("video_ops_fix", SHARED / "video_ops.py")
    src = tmp_path / "in.mp4"
    src.write_bytes(b"fake")
    captured: list[list[str]] = []
    monkeypatch.setattr(video_ops, "_run", lambda cmd, **kw: captured.append(cmd))

    def run_cut(**kw):
        args = SimpleNamespace(
            input=str(src), output=str(tmp_path / kw.pop("out")),
            start=kw.pop("start", None), end=kw.pop("end", None),
            duration=kw.pop("duration", None), reencode=False, crf=23, preset="medium")
        assert not kw, kw
        return video_ops.cmd_cut(args)

    # 2s→6s：输入侧 -ss 归零时间戳后，输出侧时长必须是 end-start=4s
    run_cut(out="o1.mp4", start="00:00:02", end="00:00:06")
    cmd = captured[-1]
    assert cmd[0:2] == ["ffmpeg", "-y"]
    assert cmd[2:4] == ["-ss", "00:00:02"], "输入侧 -ss 保留（快速关键帧定位）"
    assert "-to" not in cmd, "--start+--end 不能用输出侧 -to（从新零点起算会得到 [2s,8s)）"
    i = cmd.index("-t")
    assert abs(float(cmd[i + 1]) - 4.0) < 0.01, cmd

    # 只有 --end：无 -ss，-to 从原始 0 点起算 → 正确
    run_cut(out="o2.mp4", end="6")
    assert "-to" in captured[-1] and "-ss" not in captured[-1]

    # start>end：报错退出而不是产出空片段
    with pytest.raises(SystemExit):
        run_cut(out="o3.mp4", start="10", end="2")


def test_video_tsec_parses_ffmpeg_time_forms():
    video_ops = _load("video_ops_tsec", SHARED / "video_ops.py")
    t = video_ops._tsec
    assert t("90") == 90.0
    assert t("90.5") == 90.5
    assert t("1234567") == 1234567.0
    assert t("1.2345") == 1.2345
    assert t("00:01:30") == 90.0
    assert t("1:30") == 90.0
    assert t("1:2:3.5") == 3723.5
    assert t("1000000:00:00.123456") == 3600000000.123456
    for bad in ("", "1:2:3:4", "abc", "-5", "1:30:"):
        with pytest.raises(ValueError):
            t(bad)


# ── intro_outro：card 的 logo 恒为输入 1（--bg-image 不再 +1） ──────────
def test_card_logo_is_always_input_one(tmp_path, monkeypatch):
    intro = _load("intro_outro_fix", SHARED / "intro_outro.py")
    bg = tmp_path / "bg.png"; bg.write_bytes(b"x")
    logo = tmp_path / "logo.png"; logo.write_bytes(b"x")
    out = tmp_path / "card.mp4"
    captured: list[list[str]] = []
    monkeypatch.setattr(intro, "_check_ffmpeg", lambda: None)
    monkeypatch.setattr(intro, "_find_font", lambda explicit=None: "/tmp/font.ttf")
    monkeypatch.setattr(intro, "_run", lambda cmd: captured.append(cmd))
    argv = ["intro_outro.py", "card", "-o", str(out),
            "--bg-image", str(bg), "--logo", str(logo),
            "--title", "测试", "--duration", "2"]
    monkeypatch.setattr(sys, "argv", argv)
    intro.main()
    cmd = captured[-1]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[1:v]scale=" in graph, "logo 必须引用输入 1：背景恒占一个 -i（见 _audio_idx 注释）"
    assert "[2:v]" not in graph, "带 --bg-image 时旧代码错指 [2:v]（anullsrc 音频输入）→ filtergraph 必挂"
    # 输入布局：0=bg，1=logo，2=anullsrc；音频 map 仍指最后一个输入
    assert cmd[cmd.index("-map") + 1] == "[vout]"
    assert cmd[cmd.index("-map") + 3] == "2:a", "anullsrc 是输入 2，音频 map 应为 2:a"


# ── content_guard：私网 IP 正则不吞 3 段版本号 ──────────────────────────
def test_content_guard_private_ip_regex(tmp_path, monkeypatch):
    guard = _load("content_guard_fix", SHARED / "content_guard.py")
    monkeypatch.chdir(tmp_path)          # 无 .env → 字面值通道为空

    def cats(text):
        return [f.category for f in guard.scan(text)]

    # 真私网 IP（4 段）必须命中 BLOCK 级 proxy-ip
    assert "proxy-ip" in cats("内网机器 10.1.2.3:8080 走代理")
    assert "proxy-ip" in cats("路由器后台 192.168.1.1 默认密码")
    assert "proxy-ip" in cats("手机热点网段 172.20.10.1")
    # 3 段版本号形态（旧正则 {2,3} 会误拦发布）
    assert "proxy-ip" not in cats("升级到 macOS 10.15.7 后再试")
    assert "proxy-ip" not in cats("版本 10.3.2 修复了问题")
    assert "proxy-ip" not in cats("10.15.7")


# ── image_ops：compress 低 quality + --max-kb 不再 TypeError ────────────
def test_image_compress_low_quality_with_max_kb(tmp_path):
    from PIL import Image
    image_ops = _load("image_ops_fix", SHARED / "image_ops.py")
    src = tmp_path / "big.jpg"
    Image.new("RGB", (256, 256),
              tuple(200 - i % 64 for i in range(3))).save(src, quality=95)
    out = tmp_path / "small.jpg"

    def run(quality, max_kb):
        args = SimpleNamespace(input=str(src), output=str(out),
                               quality=quality, max_kb=max_kb)
        return image_ops.cmd_compress(args)

    # quality=5：旧代码 range(5,9,-5) 为空 → best=None → len(None) 崩
    assert run(quality=5, max_kb=10240) == 0
    assert out.is_file() and out.stat().st_size > 0
    # 永远压不到目标的兜底路径也不崩，仍产出文件 + WARN
    assert run(quality=5, max_kb=1) == 0
    assert out.is_file() and out.stat().st_size > 0


# ── gemini_maas_adapter：非流式 finishReason 与流式一致 ────────────────
def test_gemini_adapter_non_streaming_finish_reason():
    adapter = _load("gemini_adapter_fix", SCRIPTS / "gemini_maas_adapter.py")
    base = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2,
                              "thoughtsTokenCount": 0, "totalTokenCount": 3}}

    def finish_for(candidate_extra):
        data = dict(base)
        data["candidates"] = [dict({"content": {"parts": [{"text": "hi"}]}},
                                   **candidate_extra)]
        return adapter.gemini_to_openai(data, "gemini-pro")["choices"][0]["finish_reason"]

    assert finish_for({"finishReason": "MAX_TOKENS"}) == "length"
    assert finish_for({"finishReason": "SAFETY"}) == "content_filter"
    assert finish_for({"finishReason": "STOP"}) == "stop"
    assert finish_for({}) == "stop"
    # 工具调用优先于 STOP：客户端要继续 tool loop
    data = dict(base)
    data["candidates"] = [{"finishReason": "STOP", "content": {"parts": [
        {"functionCall": {"name": "f", "args": {}}}]}}]
    assert adapter.gemini_to_openai(data, "m")["choices"][0]["finish_reason"] == "tool_calls"

# ── issue #110：未知新闻源不能静默成功 ────────────────────────────────
def test_news_fetch_rejects_unknown_source(monkeypatch):
    import types
    fake_bs4 = types.ModuleType("bs4")
    fake_bs4.BeautifulSoup = lambda *args, **kwargs: None
    fake_bs4.XMLParsedAsHTMLWarning = UserWarning
    monkeypatch.setitem(sys.modules, "bs4", fake_bs4)
    scripts_dir = (
        Path(__file__).resolve().parents[1]
        / "skills" / "builtin" / "skill-news-intelligence" / "scripts"
    )
    monkeypatch.syspath_prepend(str(scripts_dir))
    fetch_news = _load("fetch_news_source_guard", scripts_dir / "fetch_news.py")
    monkeypatch.setattr(sys, "argv", ["fetch_news.py", "--source", "rss", "--no-save"])
    with pytest.raises(SystemExit) as exc:
        fetch_news.main()
    assert exc.value.code != 0
    assert "未知 source key" in str(exc.value)


# ── issue #112：涨粉分析兼容中英文平台、时区与缺失指标 ────────────────
def test_publish_analytics_growth_normalizes_platform_timezone_and_missing_metrics():
    analyze = _load(
        "publish_analytics_growth_fix",
        Path(__file__).resolve().parents[1]
        / "skills" / "builtin" / "skill-publish-analytics" / "scripts" / "analyze.py",
    )
    entries = [
        {
            "id": "p1",
            "title": "自动记录",
            "platform": "小红书",
            "profile": "达人",
            "published_at": "2026-07-01T08:00:00+08:00",
            "initial_metrics": {},
        },
        {
            "id": "p2",
            "title": "无 profile 发布",
            "platform": "小红书",
            "profile": "",
            "published_at": "2026-07-01T09:00:00+08:00",
            "initial_metrics": {"likes": 10},
        },
    ]
    follower_data = {"snapshots": [
        {"recorded_at": "2026-07-01", "followers": 100, "platform": "xiaohongshu", "profile": "达人"},
        {"recorded_at": "2026-07-02", "followers": 130, "platform": "xiaohongshu", "profile": "达人"},
        {"recorded_at": "2026-07-02", "followers": 150, "platform": "xiaohongshu", "profile": "另一个"},
    ]}
    out = analyze.analyze_growth(entries, follower_data)
    by_id = {row["id"]: row for row in out["ranking"]}
    assert by_id["p1"]["delta_24h"] == 30
    assert by_id["p2"]["delta_24h"] in {30, 50}
    assert analyze._score({"initial_metrics": {}}) is None


# ── issue #112：自动发布记录必须透传 profile 到归因日志 ───────────────
def test_calendar_record_publish_forwards_profile_to_publish_log(monkeypatch, tmp_path):
    calendar_ops = _load("calendar_ops_profile_forward_fix", SHARED / "calendar_ops.py")
    data = tmp_path / "schedule.json"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(calendar_ops.subprocess, "run", fake_run)
    ok = calendar_ops.record_publish(
        "xiaohongshu", "自动记录", tags="AI", ptype="图文", note="正文", source="publish-page",
        profile="达人", data_path=data, forward_log=True,
    )
    assert ok is True
    item = json.loads(data.read_text(encoding="utf-8"))[0]
    assert item["profile"] == "达人"
    assert "--profile" in calls[-1]
    assert calls[-1][calls[-1].index("--profile") + 1] == "达人"
    assert calls[-1][calls[-1].index("--tags") + 1] == "AI"
    assert calls[-1][calls[-1].index("--type") + 1] == "图文"


# ── issue #117：依赖导入 SystemExit 也要转成安装提示码 3 ───────────────
def test_remove_bg_rembg_import_systemexit_becomes_dependency_exit(monkeypatch):
    remove_bg = _load("remove_bg_dependency_guard", SHARED / "remove_bg.py")
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "rembg":
            raise SystemExit(1)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SystemExit) as exc:
        remove_bg._require_rembg()
    assert exc.value.code == 3


# ── issue #119：bgm 可关闭 music 循环，短旁白补静音到视频长度 ──────────
def test_video_bgm_music_loop_off_uses_apad_without_stream_loop(monkeypatch, tmp_path):
    video_ops = _load("video_ops_bgm_loop_off_fix", SHARED / "video_ops.py")
    captured = []
    src = tmp_path / "clip.mp4"
    music = tmp_path / "vo.mp3"
    out = tmp_path / "out.mp4"
    monkeypatch.setattr(video_ops, "_require_input", lambda value: Path(value))
    monkeypatch.setattr(video_ops, "_prep_out", lambda value: Path(value))
    monkeypatch.setattr(video_ops, "_has_audio", lambda _path: True)
    monkeypatch.setattr(video_ops, "_run", lambda cmd: captured.append(cmd))
    monkeypatch.setattr(video_ops, "_done", lambda _out: None)

    video_ops.cmd_bgm(SimpleNamespace(
        input=str(src), music=str(music), output=str(out),
        voice_volume=0.4, music_volume=1.0, music_loop_off=True,
    ))
    cmd = captured[-1]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "-stream_loop" not in cmd
    assert "apad" in graph

    video_ops.cmd_bgm(SimpleNamespace(
        input=str(src), music=str(music), output=str(out),
        voice_volume=0.4, music_volume=1.0, music_loop_off=False,
    ))
    cmd = captured[-1]
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"


# ── issue #120：空质量门失败，{python} 占位符可替换 ──────────────────
@pytest.mark.parametrize("executable", [None, "C:\\Program Files\\Python312\\python.exe"])
def test_video_pipeline_gates_empty_phase_fails_and_python_placeholder(monkeypatch, tmp_path, executable):
    if executable is not None:
        monkeypatch.setattr(sys, "executable", executable)
    aggregate = _load(
        "video_gate_aggregate_fix",
        Path(__file__).resolve().parents[1]
        / "skills" / "builtin" / "video-production" / "vendor" / "video-pipeline-sdk"
        / "gates" / "aggregate.py",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "gates.json").write_text(json.dumps({"gates": []}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["aggregate.py", "--run-dir", str(run_dir), "--phase", "verify"])
    with pytest.raises(SystemExit) as exc:
        aggregate.main()
    assert exc.value.code == 1
    assert "无质量门" in (run_dir / "gate-report.json").read_text(encoding="utf-8")

    (run_dir / "gates.json").write_text(json.dumps({"gates": [
        {"id": "py", "title": "py", "phase": "verify", "cmd": "{python} -c pass"}
    ]}), encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(aggregate.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["aggregate.py", "--run-dir", str(run_dir), "--phase", "verify"])
    with pytest.raises(SystemExit) as exc:
        aggregate.main()
    assert exc.value.code == 0
    assert calls[-1][-1] == sys.executable.replace("\\", "/") + " -c pass"


def test_video_pipeline_stage_respects_empty_gate_failure(monkeypatch, tmp_path):
    run_mod = _load(
        "video_pipeline_run_empty_gate_fix",
        Path(__file__).resolve().parents[1]
        / "skills" / "builtin" / "video-production" / "vendor" / "video-pipeline-sdk"
        / "pipeline" / "run.py",
    )
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "gates.json").write_text(json.dumps({"gates": []}), encoding="utf-8")
    status, message = run_mod.st_verify({"rd": run_dir})
    assert status == "gatefail"
    assert "质量门" in message

    (run_dir / "gates.json").write_text(json.dumps({"gates": [
        {"id": "only-verify", "phase": "verify", "cmd": "true"}
    ]}), encoding="utf-8")
    (run_dir / "workdir" / "out").mkdir(parents=True)
    (run_dir / "workdir" / "out" / "final.mp4").write_bytes(b"fake")
    (run_dir / "run-state.json").write_text(json.dumps({"stages": {}}), encoding="utf-8")
    status, message = run_mod.st_render({"rd": run_dir, "state": {}})
    assert status == "gatefail"
    assert "质量门" in message


# ── issue #122：loudnorm 后恢复输入采样率 ─────────────────────────────
def test_audio_ops_loudnorm_restores_input_sample_rate(monkeypatch, tmp_path):
    audio_ops = _load("audio_ops_sample_rate_fix", SHARED / "audio_ops.py")
    captured = []
    inp = tmp_path / "in.wav"
    out = tmp_path / "out.wav"
    monkeypatch.setattr(audio_ops, "_check_input", lambda value: Path(value))
    monkeypatch.setattr(audio_ops, "_prep_output", lambda value: Path(value))
    monkeypatch.setattr(audio_ops, "_summary", lambda _path: {"sample_rate": "44100"})
    monkeypatch.setattr(audio_ops, "_run_ffmpeg", lambda cmd, quiet=False: captured.append(cmd))
    monkeypatch.setattr(audio_ops, "_report", lambda _path: None)

    audio_ops.cmd_normalize(SimpleNamespace(
        input=str(inp), output=str(out), i=-14.0, tp=-1.5, lra=11.0, bitrate=None,
    ))
    assert any("loudnorm=" in part and "aresample=44100" in part for part in captured[-1])

    monkeypatch.setattr(audio_ops, "_ensure_rnnoise_model", lambda _model: "model.rnnn")
    monkeypatch.setattr(audio_ops, "_probe", lambda _path: {"streams": [{"codec_type": "audio"}]})
    audio_ops.cmd_denoise(SimpleNamespace(
        input=str(inp), output=str(out), tier=3, model="model.rnnn", mix=1.0,
    ))
    assert any("arnndn" in part and "aresample=44100" in part for part in captured[-1])
