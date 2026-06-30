"""jobhunt 专题 P0 验收测试 —— 简历解析 + 入库。

纯离线、确定性：LLM 走注入的 FakeDriver，不发网络请求，可重复跑。
覆盖三层：
  1. 解析纯函数（_strip_json / _normalize / extract_text）
  2. ResumeParser.parse（fake LLM → 结构化 + 归一化 + 错误路径）
  3. ingest_resume 端到端（落 Asset+ResumeProfile、active 互斥）+ 表约束（唯一键/状态默认）

迁移可逆性由 test_alembic_migration.py 的 upgrade head / downgrade base 全链路覆盖
（会连带跑本专题的 e4a7c1d9b2f3 升降），此处不重复。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ai_ops.core.enums import AssetSource, AssetType
from ai_ops.core.models import Asset, Base
from ai_ops.jobhunt import models as jh_models  # noqa: F401  确保四表注册进 metadata
from ai_ops.jobhunt import resume_parser as rp
from ai_ops.jobhunt.enums import ApplicationStatus, JobBoard
from ai_ops.jobhunt.models import Application, JobMatch, JobPosting, ResumeProfile
from ai_ops.jobhunt.resume_parser import (
    LLMDriver,
    ResumeParser,
    _normalize,
    _strip_json,
    extract_text,
)
from ai_ops.jobhunt.service import ingest_resume


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# 故意把 salary 给成字符串、years 给成字符串，验证 _normalize 的类型纠偏
FAKE_STRUCT = {
    "name": "张三",
    "summary": "6 年 Go/Python 后端，主导日活千万级系统",
    "years_of_experience": "6",
    "current_title": "后端工程师",
    "target_titles": ["后端工程师", "Golang 工程师"],
    "expected_cities": ["杭州", "上海"],
    "expected_salary_min": "25000",
    "expected_salary_max": "40000",
    "skills": ["Go", "Python", "Kubernetes"],
    "search_keywords": ["Go 后端", "Python 后端"],
}


class FakeDriver(LLMDriver):
    """返回固定 JSON 的假 LLM。验证 plumbing，不验证模型质量。"""

    def __init__(self, payload: str | None = None):
        self._payload = payload

    async def complete(self, system: str, user: str, **kw) -> str:
        if self._payload is not None:
            return self._payload
        return "```json\n" + json.dumps(FAKE_STRUCT, ensure_ascii=False) + "\n```"


@pytest.fixture()
def fake_llm(monkeypatch):
    """把 resume_parser.get_driver 换成 FakeDriver（ingest 链路默认会调它）。"""
    monkeypatch.setattr(rp, "get_driver", lambda: FakeDriver())


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1) 解析纯函数
# ---------------------------------------------------------------------------
def test_strip_json_handles_fence_and_prose():
    assert _strip_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_json('{"a": 1}') == '{"a": 1}'
    # 前后有废话也要能抠出主体
    assert _strip_json('好的，结果是：\n{"a": 1}\n以上。') == '{"a": 1}'


def test_normalize_fills_missing_and_coerces_types():
    out = _normalize({"name": "张三", "years_of_experience": "5", "skills": "Go"})
    # 缺失键补默认
    assert out["target_titles"] == [] and out["expected_cities"] == []
    assert out["expected_salary_min"] is None
    # 类型纠偏：str→float、标量→list
    assert out["years_of_experience"] == 5.0
    assert out["skills"] == ["Go"]
    # 顶层键齐全，下游无需再防御
    assert set(out) >= {
        "name", "summary", "years_of_experience", "target_titles",
        "expected_cities", "expected_salary_min", "expected_salary_max",
        "skills", "search_keywords", "education", "experiences",
    }


def test_normalize_salary_garbage_becomes_none():
    out = _normalize({"expected_salary_min": "面议", "expected_salary_max": None})
    assert out["expected_salary_min"] is None
    assert out["expected_salary_max"] is None


def test_extract_text_txt_and_md(tmp_path):
    assert "后端" in extract_text(_write(tmp_path, "r.txt", "张三 后端工程师"))
    assert "# 简历" in extract_text(_write(tmp_path, "r.md", "# 简历\n张三"))


def test_extract_text_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_text(tmp_path / "不存在.txt")


def test_extract_text_doc_gives_actionable_error(tmp_path):
    # .doc（老二进制）应给可操作的报错，引导另存为 docx/PDF
    p = _write(tmp_path, "r.doc", "x")
    with pytest.raises(ValueError, match="另存为"):
        extract_text(p)


def test_extract_text_unknown_ext_raises(tmp_path):
    with pytest.raises(ValueError, match="不支持"):
        extract_text(_write(tmp_path, "r.rtf", "x"))


def test_extract_text_docx_roundtrip(tmp_path):
    """真造一个 docx（含段落 + 表格）再抽取，验证二进制格式路径。无 python-docx 则跳过。"""
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("张三 后端工程师")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "技能"
    table.rows[0].cells[1].text = "Go/Python"
    path = tmp_path / "resume.docx"
    d.save(str(path))
    text = extract_text(path)
    assert "张三" in text
    assert "Go/Python" in text  # 表格内容也被抽到


# ---------------------------------------------------------------------------
# 2) ResumeParser.parse
# ---------------------------------------------------------------------------
async def test_parser_parses_fenced_json():
    out = await ResumeParser(FakeDriver()).parse("简历原文……")
    assert out["name"] == "张三"
    assert out["expected_salary_min"] == 25000  # 字符串被归一化成 int
    assert isinstance(out["expected_salary_min"], int)
    assert out["target_titles"] == ["后端工程师", "Golang 工程师"]


async def test_parser_empty_text_raises():
    with pytest.raises(ValueError, match="为空"):
        await ResumeParser(FakeDriver()).parse("   ")


async def test_parser_non_json_output_raises():
    bad = FakeDriver(payload="抱歉，我无法解析这份简历。")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        await ResumeParser(bad).parse("简历原文")


# ---------------------------------------------------------------------------
# 3) ingest_resume 端到端
# ---------------------------------------------------------------------------
async def test_ingest_creates_asset_and_profile(session, fake_llm, tmp_path):
    resume = _write(tmp_path, "resume.txt", "张三 后端工程师 6 年经验")
    profile = await ingest_resume(session, resume)

    # ResumeProfile 字段
    assert profile.name == "张三"
    assert profile.is_active is True
    assert profile.expected_salary_min == 25000  # int 化
    assert profile.target_titles == ["后端工程师", "Golang 工程师"]
    assert profile.skills == ["Go", "Python", "Kubernetes"]
    assert profile.raw_text.startswith("张三")  # 原文留档

    # 原始文件登记成 DOCUMENT 资产，且与 profile FK 关联
    asset = session.get(Asset, profile.raw_asset_id)
    assert asset is not None
    assert asset.asset_type == AssetType.DOCUMENT
    assert asset.source == AssetSource.USER_UPLOAD
    assert asset.meta.get("kind") == "resume"
    assert asset.local_path.endswith("resume.txt")


async def test_ingest_active_mutual_exclusion(session, fake_llm, tmp_path):
    """连续入库两份主简历：只有最后一份 is_active=True。"""
    r1 = _write(tmp_path, "a.txt", "简历一")
    r2 = _write(tmp_path, "b.txt", "简历二")
    p1 = await ingest_resume(session, r1, name="旧版")
    p2 = await ingest_resume(session, r2, name="新版")

    session.refresh(p1)
    assert p1.is_active is False
    assert p2.is_active is True
    actives = session.scalars(
        select(ResumeProfile).where(ResumeProfile.is_active.is_(True))
    ).all()
    assert len(actives) == 1 and actives[0].id == p2.id


async def test_ingest_no_active_does_not_disturb_existing(session, fake_llm, tmp_path):
    """set_active=False 不抢主、也不动既有 active。"""
    p1 = await ingest_resume(session, _write(tmp_path, "a.txt", "简历一"))
    p2 = await ingest_resume(session, _write(tmp_path, "b.txt", "简历二"), set_active=False)
    session.refresh(p1)
    assert p1.is_active is True   # 仍是主
    assert p2.is_active is False


# ---------------------------------------------------------------------------
# 表约束 / 状态默认
# ---------------------------------------------------------------------------
def test_jobhunt_tables_registered():
    need = {"resume_profiles", "job_postings", "job_matches", "applications"}
    assert need <= set(Base.metadata.tables)


def test_application_status_defaults_draft_and_unique(session):
    r = ResumeProfile(name="r", raw_text="", structured={})
    j = JobPosting(board=JobBoard.BOSS, external_id="J1")
    session.add_all([r, j])
    session.flush()

    app1 = Application(resume_id=r.id, job_id=j.id, board=JobBoard.BOSS)
    session.add(app1)
    session.flush()
    assert app1.status == ApplicationStatus.DRAFT  # 默认进候选池

    # 同 (resume, job) 不可重复投
    session.add(Application(resume_id=r.id, job_id=j.id, board=JobBoard.BOSS))
    with pytest.raises(IntegrityError):
        session.flush()


def test_jobposting_board_external_unique(session):
    session.add(JobPosting(board=JobBoard.BOSS, external_id="DUP"))
    session.flush()
    session.add(JobPosting(board=JobBoard.BOSS, external_id="DUP"))
    with pytest.raises(IntegrityError):
        session.flush()
