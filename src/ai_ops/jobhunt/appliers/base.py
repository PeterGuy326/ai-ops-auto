"""招聘平台适配器统一接口（对照 publishers/base.py）。

三个动作，分属不同阶段：
  - search_jobs : P1 采集岗位 → JobCandidate[]
  - apply       : P2 真投递（聊天式发打招呼语 / 表单式提交）
  - poll_replies: P3 轮询 HR 回复

凭证（credential）由调用方解密后传入（复用 accounts/store.py 的 Fernet 体系，P2 接），
适配器本身不碰加密存储——和 PublisherBase 同样的薄壳约定。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..enums import JobBoard
from ..schemas import ApplyResult, HrReply, JobCandidate, JobQuery


class ApplierBase(ABC):
    board: JobBoard

    @abstractmethod
    async def search_jobs(
        self,
        query: JobQuery,
        *,
        credential: dict | None = None,
    ) -> list[JobCandidate]:
        """按条件搜岗位。credential 为空时部分平台只能拿到游客可见的有限结果。"""

    @abstractmethod
    async def apply(
        self,
        *,
        credential: dict,
        job: JobCandidate,
        resume_summary: str,
        greeting: str,
    ) -> ApplyResult:
        """单次投递（P2）。job 用 JobCandidate（平台无关）以免耦合 ORM/session。

        - 聊天式（Boss）：在岗位会话里发 greeting
        - 表单式（智联/猎聘/51job）：点「申请职位」并提交
        """

    async def poll_replies(self, *, credential: dict) -> list[HrReply]:
        """轮询 HR 回复（P3）。默认空实现——非聊天式平台可不 override。"""
        return []
