from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ai_ops.api.main import _job_out
from ai_ops.core.enums import AccountHealth, ContentType, JobStatus, Platform
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


def test_job_projection_includes_owning_topic_for_ui_filtering():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        topic = Topic(name="truthful-filter", keywords=[], persona={}, target_platforms=[])
        account = Account(
            platform=Platform.ZHIHU,
            nickname="topic-account",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        session.add_all([topic, account])
        session.flush()
        article = Article(
            topic_id=topic.id,
            title="topic article",
            body="body",
            content_type=ContentType.LONG_ARTICLE,
        )
        session.add(article)
        session.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=account.id,
            platform=Platform.ZHIHU,
            status=JobStatus.PENDING,
        )
        session.add(job)
        session.flush()

        projected = _job_out(job)

        assert projected.topic_id == topic.id
        assert projected.article_id == article.id

    engine.dispose()
