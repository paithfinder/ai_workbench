from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import UserDefinedType


class Ltree(UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "LTREE"


class Base(DeclarativeBase):
    pass


class SourceKind(StrEnum):
    PDF = "pdf"
    MARKDOWN = "markdown"
    TEXT = "text"
    WEB = "web"
    PASTED_TEXT = "pasted_text"


class SourceStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    FAILED = "failed"
    DELETED = "deleted"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ParseStatus(StrEnum):
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


class AcquisitionType(StrEnum):
    UPLOAD = "upload"
    PASTED_TEXT = "pasted_text"
    WEB_FETCH = "web_fetch"


class ParseArtifactStatus(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


class ParseRequestKind(StrEnum):
    INITIAL = "initial"
    REPARSE = "reparse"


class ExtractionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    NEEDS_VERIFICATION = "needs_verification"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class CandidateReviewAction(StrEnum):
    EDIT = "edit"
    ACCEPT = "accept"
    MARK_NEEDS_VERIFICATION = "mark_needs_verification"
    REJECT = "reject"


class KnowledgeNodeKind(StrEnum):
    ROOT = "root"
    FOLDER = "folder"
    DOCUMENT = "document"
    POINT = "point"
    SOURCE = "source"


class CandidateAtomicity(StrEnum):
    ATOMIC = "atomic"
    NEEDS_SPLIT = "needs_split"


class JobAttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobKind(StrEnum):
    SOURCE_INGEST = "source_ingest"
    SOURCE_PARSE = "source_parse"
    SOURCE_EXTRACT = "source_extract"
    SOURCE_INDEX = "source_index"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActorType(StrEnum):
    SYSTEM = "system"
    USER = "user"
    AI = "ai"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class KnowledgeSpace(TimestampMixin, Base):
    __tablename__ = "knowledge_spaces"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    sources: Mapped[list[Source]] = relationship(back_populates="space")


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pdf','markdown','text','web','pasted_text')",
            name="ck_sources_kind",
        ),
        CheckConstraint(
            "status IN ('pending','active','failed','deleted')",
            name="ck_sources_status",
        ),
        Index("ix_sources_space_created", "space_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=SourceStatus.PENDING)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    space: Mapped[KnowledgeSpace] = relationship(back_populates="sources")
    versions: Mapped[list[SourceVersion]] = relationship(back_populates="source")


class SourceCreateRequest(Base):
    __tablename__ = "source_create_requests"
    __table_args__ = (
        UniqueConstraint("space_id", "idempotency_key", name="uq_source_create_requests_key"),
        CheckConstraint(
            "((source_id IS NOT NULL) AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR ((source_id IS NULL) AND ((lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)))",
            name="ck_source_create_requests_lease_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT")
    )
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["current_parse_artifact_id", "id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_source_versions_current_parse_artifact",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("id", "source_id", name="uq_source_versions_id_source"),
        UniqueConstraint("source_id", "version_number", name="uq_source_versions_number"),
        UniqueConstraint(
            "source_id", "upload_idempotency_key", name="uq_source_versions_upload_idempotency"
        ),
        CheckConstraint("version_number > 0", name="ck_source_versions_number_positive"),
        CheckConstraint("size_bytes >= 0", name="ck_source_versions_size_nonnegative"),
        CheckConstraint(
            "processing_status IN ('pending','ready','failed')",
            name="ck_source_versions_processing_status",
        ),
        CheckConstraint(
            "parse_status IN ('not_started','queued','parsing','ready','failed')",
            name="ck_source_versions_parse_status",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_versions_content_sha256",
        ),
        CheckConstraint(
            "expected_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_versions_expected_sha256",
        ),
        CheckConstraint(
            "completed_at IS NULL OR (content_sha256 IS NOT NULL AND storage_key IS NOT NULL "
            "AND object_etag IS NOT NULL)",
            name="ck_source_versions_completed_identity",
        ),
        CheckConstraint(
            "acquisition_type IN ('upload','pasted_text','web_fetch')",
            name="ck_source_versions_acquisition_type",
        ),
        CheckConstraint(
            "acquisition_type <> 'upload' OR (expected_content_sha256 IS NOT NULL "
            "AND original_filename IS NOT NULL AND media_type IS NOT NULL "
            "AND size_bytes IS NOT NULL AND upload_storage_key IS NOT NULL "
            "AND upload_idempotency_key IS NOT NULL AND upload_request_hash IS NOT NULL "
            "AND upload_expires_at IS NOT NULL)",
            name="ck_source_versions_upload_identity",
        ),
        Index(
            "uq_source_versions_content",
            "source_id",
            "content_sha256",
            unique=True,
            postgresql_where=sql_text("content_sha256 IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    expected_content_sha256: Mapped[str | None] = mapped_column(String(64))
    original_filename: Mapped[str | None] = mapped_column(String(500))
    media_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    upload_storage_key: Mapped[str | None] = mapped_column(String(1000))
    storage_key: Mapped[str | None] = mapped_column(String(1000))
    object_etag: Mapped[str | None] = mapped_column(String(255))
    upload_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    upload_request_hash: Mapped[str | None] = mapped_column(String(64))
    completion_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    upload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acquisition_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AcquisitionType.UPLOAD
    )
    source_uri: Mapped[str | None] = mapped_column(String(2000))
    acquisition_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    current_parse_artifact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProcessingStatus.PENDING
    )
    parse_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ParseStatus.NOT_STARTED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[Source] = relationship(back_populates="versions")


class SourceParseArtifact(Base):
    __tablename__ = "source_parse_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "id", "source_version_id", name="uq_source_parse_artifacts_id_version"
        ),
        UniqueConstraint(
            "source_version_id", "revision", name="uq_source_parse_artifacts_revision"
        ),
        CheckConstraint("revision > 0", name="ck_source_parse_artifacts_revision_positive"),
        CheckConstraint(
            "status IN ('queued','parsing','ready','failed')",
            name="ck_source_parse_artifacts_status",
        ),
        CheckConstraint(
            "canonical_content_sha256 IS NULL OR "
            "canonical_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_parse_artifacts_canonical_sha256",
        ),
        CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_source_parse_artifacts_page_count",
        ),
        Index(
            "uq_source_parse_artifacts_active",
            "source_version_id",
            unique=True,
            postgresql_where=sql_text("status IN ('queued','parsing')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_name: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ParseArtifactStatus.QUEUED
    )
    native_storage_key: Mapped[str | None] = mapped_column(String(1000))
    native_sha256: Mapped[str | None] = mapped_column(String(64))
    markdown_storage_key: Mapped[str | None] = mapped_column(String(1000))
    markdown_sha256: Mapped[str | None] = mapped_column(String(64))
    canonical_storage_key: Mapped[str | None] = mapped_column(String(1000))
    canonical_content_sha256: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceSection(Base):
    __tablename__ = "source_sections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parse_artifact_id", "source_version_id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_source_sections_artifact_version",
            ondelete="CASCADE",
        ),
        UniqueConstraint("parse_artifact_id", "ordinal", name="uq_source_sections_ordinal"),
        UniqueConstraint("parse_artifact_id", "block_id", name="uq_source_sections_block"),
        CheckConstraint("ordinal >= 0", name="ck_source_sections_ordinal_nonnegative"),
        CheckConstraint(
            "page_number IS NULL OR page_number > 0", name="ck_source_sections_page_positive"
        ),
        CheckConstraint(
            "paragraph_index IS NULL OR paragraph_index >= 0",
            name="ck_source_sections_paragraph_nonnegative",
        ),
        CheckConstraint("quote_hash ~ '^[0-9a-f]{64}$'", name="ck_source_sections_quote_hash"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_source_sections_content_hash"),
        Index("ix_source_sections_version_ordinal", "source_version_id", "ordinal", "id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    parse_artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    block_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_block_id: Mapped[str | None] = mapped_column(String(128))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    block_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    paragraph_index: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceParseRequest(Base):
    __tablename__ = "source_parse_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parse_artifact_id", "source_version_id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_source_parse_requests_artifact_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "source_version_id", "idempotency_key", name="uq_source_parse_requests_key"
        ),
        UniqueConstraint("parse_artifact_id", name="uq_source_parse_requests_artifact"),
        UniqueConstraint("job_id", name="uq_source_parse_requests_job"),
        CheckConstraint("kind IN ('initial','reparse')", name="ck_source_parse_requests_kind"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_source_parse_requests_hash"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False
    )
    parse_artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_extraction_jobs_job"),
        UniqueConstraint(
            "source_version_id", "prompt_version", name="uq_extraction_jobs_version_prompt"
        ),
        CheckConstraint(
            "status IN ('queued','running','ready','failed')",
            name="ck_extraction_jobs_status",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="ck_extraction_jobs_metrics_nonnegative",
        ),
        Index("ix_extraction_jobs_space_status_created", "space_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False
    )
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    source_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False
    )
    parse_artifact_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("source_parse_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ExtractionStatus.QUEUED
    )
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExtractionCandidate(Base):
    __tablename__ = "extraction_candidates"
    __table_args__ = (
        UniqueConstraint("extraction_job_id", "ordinal", name="uq_extraction_candidates_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_extraction_candidates_ordinal_nonnegative"),
        CheckConstraint(
            "status IN ('pending_review','needs_verification','accepted','rejected')",
            name="ck_extraction_candidates_status",
        ),
        CheckConstraint(
            "atomicity IN ('atomic','needs_split')",
            name="ck_extraction_candidates_atomicity",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_extraction_candidates_confidence"
        ),
        CheckConstraint(
            "status <> 'needs_verification' OR "
            "(verification_reason IS NOT NULL AND btrim(verification_reason) <> '')",
            name="ck_extraction_candidates_verification_reason",
        ),
        CheckConstraint(
            "status = 'rejected' OR rejection_reason IS NULL",
            name="ck_extraction_candidates_rejection_reason",
        ),
        CheckConstraint("version > 0", name="ck_extraction_candidates_version_positive"),
        Index("ix_extraction_candidates_space_status_created", "space_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    extraction_job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("extraction_jobs.id", ondelete="CASCADE"), nullable=False
    )
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    source_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    suggested_destination_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    atomicity: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    verification_reason: Mapped[str | None] = mapped_column(String(1000))
    rejection_reason: Mapped[str | None] = mapped_column(String(1000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conditions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    exceptions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CandidateEvidence(Base):
    __tablename__ = "candidate_evidence"
    __table_args__ = (
        UniqueConstraint("candidate_id", "section_id", name="uq_candidate_evidence_section"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("extraction_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="RESTRICT"), nullable=False
    )
    section_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_sections.id", ondelete="RESTRICT"), nullable=False
    )
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeNode(TimestampMixin, Base):
    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        UniqueConstraint("id", "space_id", name="uq_knowledge_nodes_id_space"),
        UniqueConstraint("origin_candidate_id", name="uq_knowledge_nodes_origin_candidate"),
        ForeignKeyConstraint(
            ["parent_id", "space_id"],
            ["knowledge_nodes.id", "knowledge_nodes.space_id"],
            name="fk_knowledge_nodes_parent_space",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "source_id"],
            ["source_versions.id", "source_versions.source_id"],
            name="fk_knowledge_nodes_source_version_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["id", "current_revision_id"],
            ["knowledge_revisions.node_id", "knowledge_revisions.id"],
            name="fk_knowledge_nodes_current_revision",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "kind IN ('root','folder','document','point','source')",
            name="ck_knowledge_nodes_kind",
        ),
        CheckConstraint(
            "(kind = 'root' AND parent_id IS NULL AND source_id IS NULL "
            "AND source_version_id IS NULL AND origin_candidate_id IS NULL) OR "
            "(kind = 'source' AND parent_id IS NOT NULL AND source_id IS NOT NULL "
            "AND source_version_id IS NOT NULL AND current_revision_id IS NULL "
            "AND origin_candidate_id IS NULL) OR "
            "(kind IN ('folder','document') AND parent_id IS NOT NULL "
            "AND source_id IS NULL AND source_version_id IS NULL "
            "AND origin_candidate_id IS NULL) OR "
            "(kind = 'point' AND parent_id IS NOT NULL AND source_id IS NULL "
            "AND source_version_id IS NULL)",
            name="ck_knowledge_nodes_shape",
        ),
        CheckConstraint("version > 0", name="ck_knowledge_nodes_version_positive"),
        CheckConstraint("sort_order >= 0", name="ck_knowledge_nodes_sort_nonnegative"),
        Index("ix_knowledge_nodes_space_parent_kind", "space_id", "parent_id", "kind"),
        Index(
            "ix_knowledge_nodes_space_parent_order",
            "space_id",
            "parent_id",
            "sort_order",
            "id",
        ),
        Index("ix_knowledge_nodes_path", "path", postgresql_using="gist"),
        Index(
            "uq_knowledge_nodes_space_path",
            "space_id",
            "path",
            unique=True,
        ),
        Index(
            "uq_knowledge_nodes_space_root",
            "space_id",
            unique=True,
            postgresql_where=sql_text("kind = 'root' AND deleted_at IS NULL"),
        ),
        Index(
            "uq_knowledge_nodes_document_source_version",
            "space_id",
            "parent_id",
            "source_version_id",
            unique=True,
            postgresql_where=sql_text("kind = 'source' AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_candidate_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("extraction_candidates.id", ondelete="RESTRICT"),
    )
    current_revision_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    path: Mapped[str] = mapped_column(Ltree(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("sources.id", ondelete="RESTRICT")
    )
    source_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["node_id", "space_id"],
            ["knowledge_nodes.id", "knowledge_nodes.space_id"],
            name="fk_knowledge_revisions_node_space",
            ondelete="CASCADE",
        ),
        UniqueConstraint("node_id", "id", name="uq_knowledge_revisions_node_id"),
        UniqueConstraint("node_id", "revision_number", name="uq_knowledge_revisions_node_number"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_knowledge_revisions_content_hash",
        ),
        CheckConstraint(
            "revision_number > 0", name="ck_knowledge_revisions_number_positive"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    space_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    conditions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    exceptions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    edit_reason: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeEvidence(Base):
    __tablename__ = "knowledge_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parse_artifact_id", "source_version_id"],
            ["source_parse_artifacts.id", "source_parse_artifacts.source_version_id"],
            name="fk_knowledge_evidence_artifact_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("revision_id", "section_id", name="uq_knowledge_evidence_section"),
        CheckConstraint(
            "quote_hash ~ '^[0-9a-f]{64}$'", name="ck_knowledge_evidence_quote_hash"
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_knowledge_evidence_content_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    source_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    parse_artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    section_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_sections.id", ondelete="RESTRICT"), nullable=False
    )
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    frozen_quote: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeWriteRequest(Base):
    __tablename__ = "knowledge_write_requests"
    __table_args__ = (
        UniqueConstraint("space_id", "idempotency_key", name="uq_knowledge_write_requests_key"),
        CheckConstraint(
            "operation IN ('create','edit','move','delete')",
            name="ck_knowledge_write_requests_operation",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_knowledge_write_requests_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeWriteResult(Base):
    __tablename__ = "knowledge_write_results"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_knowledge_write_results_request"),
        CheckConstraint("node_version > 0", name="ck_knowledge_write_results_version"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_write_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="RESTRICT"), nullable=False
    )
    node_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReviewCard(Base):
    __tablename__ = "review_cards"
    __table_args__ = (
        ForeignKeyConstraint(
            ["knowledge_node_id", "space_id"],
            ["knowledge_nodes.id", "knowledge_nodes.space_id"],
            name="fk_review_cards_node_space",
            ondelete="CASCADE",
        ),
        UniqueConstraint("knowledge_node_id", name="uq_review_cards_node"),
        CheckConstraint("status IN ('active','retired')", name="ck_review_cards_status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    knowledge_node_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    knowledge_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CandidateReview(Base):
    __tablename__ = "candidate_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", "to_version", name="uq_candidate_reviews_version"),
        CheckConstraint(
            "action IN ('edit','accept','mark_needs_verification','reject')",
            name="ck_candidate_reviews_action",
        ),
        CheckConstraint(
            "from_version > 0 AND to_version = from_version + 1",
            name="ck_candidate_reviews_versions",
        ),
        Index("ix_candidate_reviews_space_created", "space_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("extraction_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1000))
    before_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CandidateReviewRequest(Base):
    __tablename__ = "candidate_review_requests"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "idempotency_key", name="uq_candidate_review_requests_key"
        ),
        CheckConstraint(
            "operation IN ('edit','accept','mark_needs_verification','reject')",
            name="ck_candidate_review_requests_operation",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_candidate_review_requests_hash"
        ),
        CheckConstraint(
            "expected_version > 0", name="ck_candidate_review_requests_version_positive"
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("extraction_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CandidateReviewResult(Base):
    __tablename__ = "candidate_review_results"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_candidate_review_results_request"),
        CheckConstraint(
            "candidate_version > 0", name="ck_candidate_review_results_version_positive"
        ),
        CheckConstraint(
            "candidate_status IN ('pending_review','needs_verification','accepted','rejected')",
            name="ck_candidate_review_results_status",
        ),
        CheckConstraint(
            "(candidate_status = 'accepted' AND knowledge_node_id IS NOT NULL "
            "AND knowledge_revision_id IS NOT NULL AND review_card_id IS NOT NULL) OR "
            "(candidate_status <> 'accepted' AND knowledge_node_id IS NULL "
            "AND knowledge_revision_id IS NULL AND review_card_id IS NULL)",
            name="ck_candidate_review_results_acceptance",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidate_review_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_review_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidate_reviews.id", ondelete="RESTRICT"),
        nullable=False,
    )
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_status: Mapped[str] = mapped_column(String(32), nullable=False)
    knowledge_node_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="RESTRICT")
    )
    knowledge_revision_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_revisions.id", ondelete="RESTRICT")
    )
    review_card_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("review_cards.id", ondelete="RESTRICT")
    )
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('source_ingest','source_parse','source_extract','source_index')",
            name="ck_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_jobs_progress_range"),
        CheckConstraint("attempt_count >= 0", name="ck_jobs_attempt_count_nonnegative"),
        CheckConstraint(
            "attempt_budget_start >= 0 AND attempt_budget_start <= attempt_count",
            name="ck_jobs_attempt_budget_start",
        ),
        Index("ix_jobs_space_status_created", "space_id", "status", "created_at"),
        Index(
            "uq_jobs_idempotency",
            "space_id",
            "kind",
            "idempotency_key",
            unique=True,
            postgresql_where=sql_text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_jobs_source_version_kind",
            "source_version_id",
            "kind",
            unique=True,
            postgresql_where=sql_text(
                "source_version_id IS NOT NULL AND kind <> 'source_parse'"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    source_version_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("source_versions.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JobStatus.QUEUED)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_budget_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
        CheckConstraint("attempt_number > 0", name="ck_job_attempts_number_positive"),
        CheckConstraint(
            "status IN ('running','succeeded','failed')", name="ck_job_attempts_status"
        ),
        CheckConstraint(
            "heartbeat_at >= started_at AND lease_expires_at >= heartbeat_at",
            name="ck_job_attempts_lease_timeline",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_job_attempts_finished_state",
        ),
        Index("ix_job_attempts_job_status", "job_id", "status"),
        Index("ix_job_attempts_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    worker_name: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class JobRetryRequest(Base):
    __tablename__ = "job_retry_requests"
    __table_args__ = (
        UniqueConstraint("job_id", "idempotency_key", name="uq_job_retry_requests_key"),
        UniqueConstraint(
            "job_id", "target_attempt_number", name="uq_job_retry_requests_attempt"
        ),
        CheckConstraint("target_attempt_number > 0", name="ck_job_retry_target_attempt_positive"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="ck_outbox_attempt_count_nonnegative"),
        Index(
            "ix_outbox_events_pending",
            "available_at",
            "created_at",
            postgresql_where=sql_text("published_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('system','user','ai')", name="ck_activity_actor_type"),
        Index("ix_activity_events_space_occurred", "space_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("knowledge_spaces.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
