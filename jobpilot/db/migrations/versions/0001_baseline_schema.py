"""baseline schema

Revision ID: 0001
Revises:
Create Date: 2026-11-21 16:50:01.057045
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import jobpilot.db.models

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Baseline schema: all tables from spec Section 6 + fsm_state, http_cache, link_cache.
    op.create_table(
        "api_usage",
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("source", "day", name=op.f("pk_api_usage")),
    )
    op.create_table(
        "fsm_state",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=255), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_fsm_state")),
    )
    op.create_table(
        "http_cache",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("fetched_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.Column("expires_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_http_cache")),
    )
    with op.batch_alter_table("http_cache", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_http_cache_expires_at"), ["expires_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_http_cache_source"), ["source"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_job_id", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("remote_type", sa.String(length=20), nullable=True),
        sa.Column("employment_type", sa.String(length=20), nullable=True),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("salary_period", sa.String(length=10), nullable=True),
        sa.Column("salary_is_estimated", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("apply_url", sa.String(length=2000), nullable=False),
        sa.Column("posted_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_jobs_fingerprint")),
        sa.UniqueConstraint("source", "source_job_id", name=op.f("uq_jobs_source_source_job_id")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_posted_at", ["posted_at"], unique=False)

    op.create_table(
        "link_cache",
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(length=20), nullable=False),
        sa.Column("checked_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("url_hash", name=op.f("pk_link_cache")),
    )
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("digest_time", sa.Time(), nullable=False),
        sa.Column("digest_enabled", sa.Boolean(), nullable=False),
        sa.Column("jobs_per_digest", sa.Integer(), nullable=False),
        sa.Column("next_digest_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=True),
        sa.Column("last_digest_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), nullable=False),
        sa.Column("created_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("telegram_id", name=op.f("pk_users")),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_users_next_digest_at"), ["next_digest_at"], unique=False
        )

    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=True),
        sa.Column("created_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_feedback_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_feedback_user_id"), ["user_id"], unique=False)

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("is_basic", sa.Boolean(), nullable=False),
        sa.Column("sub_scores_json", sa.JSON(), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("missing_skills_json", sa.JSON(), nullable=False),
        sa.Column("red_flags_json", sa.JSON(), nullable=False),
        sa.Column("pitch", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "sent",
                "saved",
                "dismissed",
                "applied",
                name="match_status",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("sent_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_matches_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_matches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_matches")),
        sa.UniqueConstraint("user_id", "job_id", name=op.f("uq_matches_user_id_job_id")),
    )
    with op.batch_alter_table("matches", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_matches_user_id"), ["user_id"], unique=False)

    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("raw_resume_text", sa.Text(), nullable=False),
        sa.Column("resume_sha256", sa.String(length=64), nullable=True),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profiles")),
        sa.UniqueConstraint("user_id", "version", name=op.f("uq_profiles_user_id_version")),
    )
    with op.batch_alter_table("profiles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_profiles_user_id"), ["user_id"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "type",
            sa.Enum(
                "search",
                "digest",
                name="run_type",
                native_enum=False,
                create_constraint=True,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("started_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.Column("finished_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=True),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("llm_tokens_in", sa.Integer(), nullable=False),
        sa.Column("llm_tokens_out", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_runs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_user_id"), ["user_id"], unique=False)

    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("prefs_json", sa.JSON(), nullable=False),
        sa.Column("weights_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", jobpilot.db.models.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name=op.f("fk_preferences_profile_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_id"],
            name=op.f("fk_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preferences")),
        sa.UniqueConstraint(
            "user_id", "profile_id", name=op.f("uq_preferences_user_id_profile_id")
        ),
    )
    with op.batch_alter_table("preferences", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_preferences_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    # Drop everything in reverse dependency order.
    with op.batch_alter_table("preferences", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_preferences_user_id"))

    op.drop_table("preferences")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_user_id"))

    op.drop_table("runs")
    with op.batch_alter_table("profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_profiles_user_id"))

    op.drop_table("profiles")
    with op.batch_alter_table("matches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_matches_user_id"))

    op.drop_table("matches")
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_feedback_user_id"))

    op.drop_table("feedback")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_next_digest_at"))

    op.drop_table("users")
    op.drop_table("link_cache")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_jobs_posted_at")

    op.drop_table("jobs")
    with op.batch_alter_table("http_cache", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_http_cache_source"))
        batch_op.drop_index(batch_op.f("ix_http_cache_expires_at"))

    op.drop_table("http_cache")
    op.drop_table("fsm_state")
    op.drop_table("api_usage")
