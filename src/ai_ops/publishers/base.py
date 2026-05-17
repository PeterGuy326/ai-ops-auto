from abc import ABC, abstractmethod

from ..core.enums import AccountHealth, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult


class PublisherBase(ABC):
    """所有平台发布器的统一接口。

    实现类是外部工具的薄壳 wrapper——不要在这里写发布的核心逻辑（反爬/签名/上传），
    那些归属于集成的开源工具（social-auto-upload / xhs-toolkit / ...）。
    """

    platform: Platform
    kind: PublisherKind

    @abstractmethod
    async def login(self, account_id: int, credential: dict) -> bool:
        """触发外部工具完成登录（通常落 cookie/token）。"""

    @abstractmethod
    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        """单次发布。content 已是平台无关的标准化结构，由 wrapper 翻译成工具需要的格式。"""

    @abstractmethod
    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """登录态/风控感知。"""

    async def collect_metrics(
        self,
        post_id: str,
        post_url: str | None,
        credential: dict,
    ) -> dict:
        """采集已发布内容的互动数据。

        默认实现返回空（不强求每个 publisher 都做数据采集，按需 override）。
        返回 dict 字段：{likes, comments, shares, views, raw}
        """
        return {"likes": 0, "comments": 0, "shares": 0, "views": 0, "raw": {}}
