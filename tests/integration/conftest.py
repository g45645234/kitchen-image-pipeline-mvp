from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 - populate SQLAlchemy metadata
from app.db import Base, get_db
from app.main import app
from app.models.candidate import ImageCandidate
from app.models.mistake import Mistake
from app.models.video import Video


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for integration tests", allow_module_level=True)
    db_name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not db_name.endswith("_test"):
        raise RuntimeError("Refusing to run integration tests unless database name ends with _test")
    return url


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(_test_database_url(), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seed_video(db_session: AsyncSession) -> Callable[..., object]:
    async def _seed(**overrides) -> Video:
        suffix = uuid.uuid4().hex[:8]
        video = Video(
            title=overrides.pop("title", "Test video"),
            slug=overrides.pop("slug", f"test-video-{suffix}"),
            transcript=overrides.pop("transcript", None),
            status=overrides.pop("status", "draft"),
            **overrides,
        )
        db_session.add(video)
        await db_session.commit()
        await db_session.refresh(video)
        return video

    return _seed


@pytest_asyncio.fixture
async def seed_mistake(db_session: AsyncSession, seed_video) -> Callable[..., object]:
    async def _seed(**overrides) -> Mistake:
        video = overrides.pop("video", None) or await seed_video()
        mistake = Mistake(
            video_id=video.id,
            order_index=overrides.pop("order_index", 1),
            title=overrides.pop("title", "Test mistake"),
            wrong_visual_prompt=overrides.pop("wrong_visual_prompt", "Wrong prompt"),
            right_visual_prompt=overrides.pop("right_visual_prompt", "Right prompt"),
            negative_criteria=overrides.pop("negative_criteria", []),
            **overrides,
        )
        db_session.add(mistake)
        await db_session.commit()
        await db_session.refresh(mistake)
        return mistake

    return _seed


@pytest_asyncio.fixture
async def seed_candidate(db_session: AsyncSession, seed_mistake) -> Callable[..., object]:
    async def _seed(**overrides) -> ImageCandidate:
        mistake = overrides.pop("mistake", None) or await seed_mistake()
        suffix = uuid.uuid4().hex
        candidate = ImageCandidate(
            mistake_id=mistake.id,
            side=overrides.pop("side", "wrong"),
            source_type=overrides.pop("source_type", "search"),
            source_provider=overrides.pop("source_provider", "test"),
            source_page_url=overrides.pop("source_page_url", "https://example.com/source"),
            image_url=overrides.pop("image_url", f"https://example.com/{suffix}.jpg"),
            image_url_hash=overrides.pop("image_url_hash", suffix[:40]),
            thumbnail_url=overrides.pop("thumbnail_url", None),
            original_width=overrides.pop("original_width", 1200),
            original_height=overrides.pop("original_height", 800),
            domain=overrides.pop("domain", "example.com"),
            author_name=overrides.pop("author_name", None),
            license_label=overrides.pop("license_label", None),
            rights_status=overrides.pop("rights_status", "unknown"),
            usage_role=overrides.pop("usage_role", "candidate"),
            may_use_directly=overrides.pop("may_use_directly", False),
            storage_key_original=overrides.pop("storage_key_original", None),
            storage_status=overrides.pop("storage_status", "pending"),
            quality_flags=overrides.pop("quality_flags", {}),
            is_low_quality=overrides.pop("is_low_quality", False),
            status=overrides.pop("status", "new"),
            **overrides,
        )
        db_session.add(candidate)
        await db_session.commit()
        await db_session.refresh(candidate)
        return candidate

    return _seed
