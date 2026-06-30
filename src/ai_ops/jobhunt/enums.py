"""jobhunt 专题枚举。

刻意与 core.enums.Platform 分开：那是内容平台（小红书/抖音…），
这里是招聘平台（Boss/智联…），两套语义不该混在一个 enum 里。
全部 str-enum，DB 侧按 String 存（无 DB 级 enum 约束，加值不需迁移）。
"""
from enum import Enum


class JobBoard(str, Enum):
    """招聘平台。投递方式分两类：
    - 聊天式：BOSS（发打招呼语 → 跟 HR 一来一回），最难
    - 表单式：ZHILIAN / LIEPIN / JOB51（点击申请 + 表单），相对好做
    """
    BOSS = "boss"          # Boss 直聘
    ZHILIAN = "zhilian"    # 智联招聘
    LIEPIN = "liepin"      # 猎聘
    JOB51 = "job51"        # 前程无忧 51job


class ApplicationStatus(str, Enum):
    """投递记录状态机（对齐 core.enums.ArticleStatus 的设计哲学）：

        DRAFT → READY → SCHEDULED → APPLIED → REPLIED
                                  ↘ SKIPPED / FAILED → DEAD

    - DRAFT     候选池：采集+打分后落库，等人工勾选（默认永远过这道人工闸）
    - READY     人工勾选审过，待投
    - SCHEDULED 已交给 scheduler 排队（限流 + jitter）
    - APPLIED   已投递 / 已发打招呼语
    - REPLIED   HR 回复了（触发钉钉通知）
    - SKIPPED   人工主动跳过（不投这个岗位）
    - FAILED    单次投递失败（可重试）
    - DEAD      重试耗尽，放弃
    """
    DRAFT = "draft"
    READY = "ready"
    SCHEDULED = "scheduled"
    APPLIED = "applied"
    REPLIED = "replied"
    SKIPPED = "skipped"
    FAILED = "failed"
    DEAD = "dead"


class MatchVerdict(str, Enum):
    """匹配引擎给单个岗位的总体结论（score 之外的离散标签，方便 UI 过滤）。"""
    STRONG = "strong"    # 强匹配，建议投
    MAYBE = "maybe"      # 可投，有可补足的 gap
    WEAK = "weak"        # 弱匹配，默认不投
