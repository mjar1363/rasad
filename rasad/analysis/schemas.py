"""Strict application contracts; provider schema support is optional, validation is not."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Citation(Contract):
    chunk_id: str
    quote: str = Field(min_length=1, max_length=2200)


class Claim(Contract):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["reported", "attributed", "estimate"]
    speaker: str | None
    event_time: str | None
    citations: list[Citation]


class Observation(Contract):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["entity", "event", "number", "framing", "headline_mismatch"]
    citations: list[Citation]


class Extraction(Contract):
    claims: list[Claim]
    observations: list[Observation]
    limitations: list[str]


class EvidenceAssessment(Contract):
    claim_id: str
    chunk_id: str
    quote: str = Field(min_length=1, max_length=2200)
    relation: Literal["supports", "contradicts", "related", "different_time", "shared_origin", "insufficient"]
    explanation: str


class Assessments(Contract):
    items: list[EvidenceAssessment]


class Statement(Contract):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["reported", "inference", "scenario", "gap"]
    claim_ids: list[str]
    assumptions: list[str]
    watch_indicators: list[str]


class Synthesis(Contract):
    statements: list[Statement]
    limitations: list[str]


class JobRequest(Contract):
    article_ids: list[str] = Field(default_factory=list, max_length=12)
    story_id: str | None = None
    document_ids: list[str] = Field(default_factory=list, max_length=12)
    question: str = Field(default="", max_length=1200)
    mode: Literal["article", "compare", "timeline", "framing", "impact", "scenario", "brief", "question"] = "article"
    hours: int = Field(default=168, ge=1, le=8760)
    max_chunks: int = Field(default=24, ge=1, le=60)


class TextDocument(Contract):
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=200000)
    language: str = Field(default="unknown", max_length=20)


class Review(Contract):
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=3, max_length=4000)
    revision: int = Field(ge=1)


class StatementCheck(Contract):
    index: int
    verdict: Literal["supported", "unsupported", "uncertain"]
    explanation: str


class QualityChecks(Contract):
    items: list[StatementCheck]


class ExternalDocument(Contract):
    url: str = Field(min_length=10, max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    source_type: Literal["primary_claimed", "secondary", "unknown"] = "unknown"


class Provenance(Contract):
    child_id: str
    parent_id: str
    relation: Literal["republication", "cites", "translation", "same_origin_claimed"]
    note: str = Field(min_length=3, max_length=4000)


class Followup(Contract):
    document_ids: list[str] = Field(min_length=1, max_length=6)
    question: str = Field(default="", max_length=1200)


class Revision(Contract):
    revision: int = Field(ge=1)
    statements: list[Statement] = Field(min_length=1, max_length=16)
    limitations: list[str] = Field(max_length=50)
    note: str = Field(min_length=3, max_length=4000)


class Assignment(Contract):
    reviewer_id: int | None


class RevisionNumber(Contract):
    revision: int = Field(ge=1)
