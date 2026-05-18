# dev DB migration SOP（alembic 工作流固化）

本文是 ai-ops-auto 数据库 schema 演进的标准操作指南。任何 `core/models.py` 改动都必须走本文 SOP，**不允许直接 `Base.metadata.create_all()` 改生产 / staging schema**。

> 上游来源：Round 4-P3 owner（P7-Round 1 在引入 alembic 后报告"autogenerate 流程繁琐、易踩坑"，本文固化 SOP 解决该 TD-FU4）。
>
> 配套测试：`tests/test_alembic_migration.py` 通过 `subprocess.run("alembic ...")` 真跑迁移，3 个用例覆盖 upgrade head / 字段存在 / downgrade base，**改动 SOP 后必须看这 3 个用例还绿**。

---

## 背景：为什么用 alembic 而不是 create_all

历史上本项目用 `Base.metadata.create_all(engine)` 一把建表，开发期快、零配置；但生产链路一旦上线就**只能加不能改**——任何字段类型变更、FK 增加、约束调整都没有回滚路径。

引入 alembic 解决三件事：

1. **可演进**：每次 schema 变更产出一份不可变 revision 文件，按线性 history 推进
2. **可回滚**：每个 revision 配 `upgrade()` + `downgrade()`，改坏了可往回退
3. **可审计**：`alembic_version` 表记录当前 schema 版本，运维一眼能看清生产在哪一版

**测试套 + alembic 关系**：测试套用 in-memory SQLite + `create_all`（快、独立、零配置），alembic CLI 由 `tests/test_alembic_migration.py` 子进程跑真迁移验证。**这两条路径不要 mix**，详见下文「测试套与 alembic 关系」。

---

## dev workflow

### 加字段（最常见，相对安全）

```bash
# Step 1: 改 model
# 编辑 src/ai_ops/core/models.py，在目标 SQLAlchemy model 上加新字段
# 例如给 PublishJob 加 retry_count 字段：
#   retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

# Step 2: 生成 migration（autogenerate 会 diff models vs alembic_version 当前状态）
alembic revision --autogenerate -m "add publish_jobs.retry_count"

# Step 3: 核对生成的 migration（!!! 务必人工 review，autogenerate 不总是对 !!!）
# 检查 alembic/versions/ 下新生成的 .py 文件：
#   - 字段类型 / nullable / default 是否符合预期
#   - SQLite 加 FK / 改类型必须走 batch_alter_table（详见下文）
#   - FK constraint 必须显式命名 fk_<table>_<column>（详见下文）

# Step 4: 升级本地 dev DB
alembic upgrade head

# Step 5: 跑测试套确认无回归
pytest tests/ -v 2>&1 | tail -5
```

`server_default="0"` 不是可选项——存量行没有 `retry_count` 数据，缺默认值会让 `NOT NULL` 升级失败。

### 删字段（危险，建议两阶段 deprecate）

**禁止一刀切删字段**。生产里可能还有正在跑的旧版进程访问这个字段，直接 DROP 会让旧进程 query 报错。

**两阶段 SOP**：

```bash
# 阶段 1（本 release）：标 deprecated，但不删
# - 在 model 上把该字段标 nullable=True，注释 "DEPRECATED: 计划下个 release 删"
# - 业务代码全部停止写入该字段（继续读取兼容旧数据）
# - 灰度全量后观察 1-2 个 release 周期

alembic revision --autogenerate -m "deprecate publish_jobs.legacy_xxx (nullable)"
alembic upgrade head

# 阶段 2（下个 release）：真删
# - 从 model 移除字段
# - 业务代码连读取都停掉

alembic revision --autogenerate -m "drop publish_jobs.legacy_xxx"
alembic upgrade head
```

如果 dev / staging 环境改坏想紧急清掉，用 `alembic downgrade -1` 回退最近一次（参见下文「回滚 SOP」）。

### 改字段类型（最危险，常需 data migration）

类型变更（如 `VARCHAR(100) → VARCHAR(200)`、`Integer → BigInteger`）autogenerate 经常**检测不到或检测错**（参见下文「autogenerate 限制清单」）。

**SOP**：

```bash
# Step 1: 改 model 字段类型
# Step 2: 生成 migration
alembic revision --autogenerate -m "widen articles.title to varchar(512)"

# Step 3: ★ 核对生成的 migration —— 改类型很可能需要手写
# 如果 autogenerate 没生成 alter_column 语句，要自己加：
#   with op.batch_alter_table('articles') as batch_op:
#       batch_op.alter_column('title', type_=sa.String(length=512), existing_nullable=False)
#
# 如果涉及数据形态变化（如 String → JSON），upgrade() 里要加 data migration：
#   - 加临时新列
#   - SELECT 旧列、转换、UPDATE 新列
#   - DROP 旧列、RENAME 新列

# Step 4: 在 staging 环境先跑一遍验证
DATABASE_URL=sqlite:///tmp/test_widen.db alembic upgrade head

# Step 5: 跑测试套
pytest tests/ -v
```

---

## 测试套与 alembic 关系

**两条路径分工明确，不要混用**：

| 路径 | 用途 | 速度 | 何时触发 |
|------|------|------|----------|
| `Base.metadata.create_all(engine)` | 单测用 in-memory SQLite，每个用例独立 fixture | 毫秒级 | 几乎所有业务测试（`tests/conftest.py`） |
| `alembic upgrade head`（子进程） | 真跑迁移文件，验证生产链路 | 秒级 | 只在 `tests/test_alembic_migration.py` 三个用例 |

**为什么这样切**：

- 业务测试用 alembic 会让 272 个用例每个都跑全量迁移 = 测试套从秒级膨胀到分钟级，得不偿失
- alembic 测试用 in-memory create_all 又跳过了真实生产链路验证，等于没测
- 各跑各的，互不污染

**禁止反模式**：

- 在业务 conftest fixture 里调 `alembic upgrade head`（慢、依赖 CLI、污染并行）
- 在 alembic 测试里调 `Base.metadata.create_all()`（绕过了被测对象本身）

---

## batch_alter_table 模式（SQLite 必须）

### 为什么 SQLite 不支持 `ALTER TABLE ADD CONSTRAINT`

SQLite 的 `ALTER TABLE` 只支持 `RENAME` 和 `ADD COLUMN`（普通列），**不支持加 FK、改类型、加 CHECK、改 nullable**。alembic 的 `batch_alter_table` 是 workaround：

1. CREATE 一张同结构 + 新约束的临时表
2. COPY 旧表数据到临时表
3. DROP 旧表
4. RENAME 临时表为旧名

代价是单次迁移要 rewrite 整张表（大表慢），收益是 SQLite 也能演进 schema。

### alembic 配置

`alembic/env.py` 已在 online + offline 两条路径都设置：

```python
context.configure(
    ...,
    render_as_batch=True,  # SQLite ALTER TABLE 限制必备
)
```

`render_as_batch=True` 在 Postgres / MySQL 上会**退化为普通 alter**，无副作用，所以可永久打开不用按后端切。

### 案例：加 FK 字段（Round 1 `superseded_by_job_id`）

参考 `alembic/versions/7c183c0ba12a_add_publish_jobs_superseded_by_job_id.py`：

```python
def upgrade() -> None:
    with op.batch_alter_table('publish_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('superseded_by_job_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_publish_jobs_superseded_by_job_id',  # 显式命名（详见下文）
            'publish_jobs',
            ['superseded_by_job_id'],
            ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('publish_jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_publish_jobs_superseded_by_job_id', type_='foreignkey')
        batch_op.drop_column('superseded_by_job_id')
```

autogenerate 会自动包 `batch_alter_table`（因 env.py 配了 `render_as_batch=True`），但 **constraint 名字默认匿名**，需要手工补上（下一节）。

---

## FK 显式命名约定

### 业界教训

默认匿名 FK 在 Postgres / MySQL 上 downgrade 时**找不到约束名**，drop 失败。SQLite 因为 batch_alter_table 重建整表绕过这个问题，但跨 DB 兼容必须显式命名。

### SOP

```python
# ✗ 反模式：匿名约束，downgrade 在 Postgres 上会找不到
batch_op.create_foreign_key(None, 'publish_jobs', ['superseded_by_job_id'], ['id'])

# ✓ 正确：显式命名 fk_<table>_<column>
batch_op.create_foreign_key(
    'fk_publish_jobs_superseded_by_job_id',
    'publish_jobs',
    ['superseded_by_job_id'],
    ['id'],
)
```

约定：`fk_<table>_<column>`，全小写、下划线分隔。downgrade 用同样的名字 drop。

### model 侧也建议

`core/models.py` 里 `ForeignKey` 加 `name="fk_..."`：

```python
superseded_by_job_id: Mapped[int | None] = mapped_column(
    Integer,
    ForeignKey('publish_jobs.id', name='fk_publish_jobs_superseded_by_job_id'),
    nullable=True,
)
```

这样 autogenerate 出来的 migration 也会用同名约束，免去人工改 migration 文件。

---

## autogenerate 限制清单

`alembic revision --autogenerate` 不是银弹，**必须人工 review**。已知盲区：

| 类别 | autogenerate 是否检测 | 备注 |
|------|----------------------|------|
| 加表 / 删表 | 是 | 可靠 |
| 加列 / 删列 | 是 | 可靠 |
| 加 FK / 删 FK | 是 | 但 constraint 名字匿名，必须人工补 |
| 加索引 / 删索引 | 是（基本） | 复合索引 / 部分索引偶有遗漏 |
| 列类型变更（VARCHAR 长度 / Integer 位宽） | 否 / 不稳定 | 几乎都要手写 alter_column |
| 服务端默认值变化（server_default） | 否 | 必须手写 |
| CHECK constraint | 否 | 必须手写 |
| 索引重命名 | 否 | autogenerate 会识别成 drop+create，注意 review |
| nullable True ↔ False | 是 | 但要确认存量数据兼容（缺值行升 NOT NULL 会失败） |

**SOP 强制要求**：autogenerate 后**人工通读 migration 文件**，跑一次 `alembic upgrade head` 验证，再 commit。

---

## 回滚 SOP

```bash
# 看当前在哪一版
alembic current

# 看完整历史
alembic history

# 回退一个版本（最常用，dev 调试）
alembic downgrade -1

# 回退到指定 revision
alembic downgrade <revision_id>

# 全部清空（只剩 alembic_version 空表）
alembic downgrade base
```

**注意**：

- 生产环境回滚要先 dump 数据快照，downgrade 可能丢字段就丢数据
- downgrade 失败说明 migration 的 `downgrade()` 写错了 / 不对称，要修
- SQLite 下 batch_alter_table 的 downgrade 是另一次 rewrite，慢但安全

---

## 故障排查 FAQ

### Q1：autogenerate 生成空 migration

```python
def upgrade() -> None:
    pass  # 空的，什么都没生成
```

**排查路径**：

1. model 是否真改了？`git diff src/ai_ops/core/models.py` 确认
2. `alembic/env.py` 的 `target_metadata = Base.metadata` 是否指对了 Base？
3. 改动的 model 是否被 `core/models.py` import 进来？SQLAlchemy 只识别已注册到 `Base.metadata` 的 model
4. `alembic_version` 是否已经是最新？（已经 upgrade 过这个改动了）

### Q2：`alembic upgrade head` 失败

**排查路径**：

1. 看完整 stderr：是 SQL syntax 错 / 约束冲突 / 数据冲突？
2. `alembic current` 看停在哪一版
3. 直接连 DB 看 `alembic_version` 表 `version_num` 字段确认当前版本
4. 如果是 SQLite ALTER 限制错（`Cannot add a NOT NULL column with default value NULL`），多半是 migration 没用 batch_alter_table 或缺 server_default

### Q3：测试套全挂（test_alembic_migration 之外的 270 个）

**几乎一定是误把 alembic 引到业务测试 conftest 里了**。

排查：

```bash
grep -rn "alembic" tests/conftest.py tests/test_*.py | grep -v test_alembic_migration
```

如果有命中（除了 `test_alembic_migration.py` 自身），删掉。业务测试只用 `Base.metadata.create_all(engine)`，参见「测试套与 alembic 关系」。

### Q4：`alembic_version` 表卡在不存在的 revision

dev 环境调试时偶尔会出现：手动删了 `alembic/versions/` 下某个文件，但 DB 里还记着该版本。

**修复**：

```bash
# 直接连 sqlite 改版本号到一个存在的 revision
sqlite3 ./data/ai_ops.db "UPDATE alembic_version SET version_num = '<existing_revision>'"

# 或干脆删表重建
sqlite3 ./data/ai_ops.db "DROP TABLE alembic_version"
alembic stamp head
```

**禁止在生产环境这样做**，生产只能 forward —— 加一个新 revision 修正状态。

---

## 验证 SOP 工作

每次改完 migration，至少跑一遍：

```bash
# 1. 三件套（覆盖 upgrade / 字段验证 / downgrade）
pytest tests/test_alembic_migration.py -v

# 2. 全量回归（确认业务测试没被 alembic 改动碰坏）
pytest tests/ -v 2>&1 | tail -5

# 3. 手动 dry-run（看 SQL 不执行）
DATABASE_URL=sqlite:///tmp/dry.db alembic upgrade head --sql
```

三件套 + 全量回归全绿 → migration SOP 合格，可 commit。
