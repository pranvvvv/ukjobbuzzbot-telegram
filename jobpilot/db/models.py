"""ORM models (spec Section 6, plus FSM state and caches).

Conventions:
- Every timestamp is stored as UTC via `UTCDateTime` (SQLite has no tz type).
- Enums are stored as strings with CHECK constraints (portable to Postgres).
- JSON columns hold Pydantic-validated payloads; the schema lives in code, not the DB.
- Everything user-related is keyed by `user_id` (multi-user ready, owner-locked by config).
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from jobpilot.core.timeutil import ensure_utc, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend (SQLite returns naive values)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime; use timezone-aware UTC")
        return ensure_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else ensure_utc(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: dict[Any, Any] = {  # noqa: RUF012 - SQLAlchemy API
        datetime: UTCDateTime(),
        dict[str, Any]: JSON(),
        list[Any]: JSON(),
    }


class MatchStatus(StrEnum):
    NEW = "new"
    SENT = "sent"
    SAVED = "saved"
    DISMISSED = "dismissed"
    APPLIED = "applied"


class RunType(StrEnum):
    SEARCH = "search"
    DIGEST = "digest"


def _str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    digest_time: Mapped[time] = mapped_column(Time(), default=time(9, 0))
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs_per_digest: Mapped[int] = mapped_column(Integer, default=10)
    next_digest_at: Mapped[datetime | None] = mapped_column(index=True)
    last_digest_at: Mapped[datetime | None]
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    profiles: Mapped[list[Profile]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("user_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    raw_resume_text: Mapped[str] = mapped_column(Text)
    resume_sha256: Mapped[str | None] = mapped_column(String(64))
    profile_json: Mapped[dict[str, Any]]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="profiles")


class Preference(Base):
    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "profile_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    prefs_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    weights_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id"),
        Index("ix_jobs_posted_at", "posted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(40))
    source_job_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(300))
    remote_type: Mapped[str | None] = mapped_column(String(20))
    employment_type: Mapped[str | None] = mapped_column(String(20))
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str | None] = mapped_column(String(10))
    salary_is_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    apply_url: Mapped[str] = mapped_column(String(2000))
    posted_at: Mapped[datetime | None]
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
    raw_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Match(Base):
    __tablename__ = "matches"
    # One row per (user, job) for life: the "never show the same job twice" invariant.
    __table_args__ = (UniqueConstraint("user_id", "job_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_version: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    is_basic: Mapped[bool] = mapped_column(Boolean, default=False)
    sub_scores_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reasons_json: Mapped[list[Any]] = mapped_column(default=list)
    missing_skills_json: Mapped[list[Any]] = mapped_column(default=list)
    red_flags_json: Mapped[list[Any]] = mapped_column(default=list)
    pitch: Mapped[str | None] = mapped_column(Text)
    status: Mapped[MatchStatus] = mapped_column(
        _str_enum(MatchStatus, "match_status"), default=MatchStatus.NEW
    )
    sent_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    type: Mapped[RunType] = mapped_column(_str_enum(RunType, "run_type"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    counts_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    errors_json: Mapped[list[Any]] = mapped_column(default=list)
    llm_tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class ApiUsage(Base):
    """Calls per source per UTC day; weekly/monthly windows are sums over this table."""

    __tablename__ = "api_usage"

    source: Mapped[str] = mapped_column(String(40), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)


class FsmState(Base):
    """aiogram FSM state + data, so a restart mid-conversation resumes (spec 4, 10)."""

    __tablename__ = "fsm_state"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class HttpCache(Base):
    """Raw source responses cached ~6h to save free-tier quota during development."""

    __tablename__ = "http_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)


class LinkCheck(Base):
    """Apply-link health results, so links aren't re-checked on every run."""

    __tablename__ = "link_cache"

    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(String(2000))
    status_code: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(20))  # ok | dead | unverified
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)
