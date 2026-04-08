from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from .models import FeedbackType, UserMode, UserCompanyType


class UserCreate(BaseModel):
    email: str
    mode: UserMode = UserMode.balanced


class CompanyCreate(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    sector: str | None = None
    subsector: str | None = None
    description: str | None = None


class UserCompanyCreate(BaseModel):
    company_id: UUID
    type: UserCompanyType


class InsightOut(BaseModel):
    id: UUID
    article_id: UUID
    summary: str
    why_it_matters: str
    base_score: float
    final_score: float
    created_at: datetime

    class Config:
        from_attributes = True


class ArticleOut(BaseModel):
    id: UUID
    title: str
    content: str
    source: str
    url: str
    published_at: datetime

    class Config:
        from_attributes = True


class FeedbackCreate(BaseModel):
    insight_id: UUID
    feedback_type: FeedbackType


class MessageFeedbackInput(BaseModel):
    insight_id: UUID
    message: str


class QueryInput(BaseModel):
    query: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[UUID]


class AssessmentOut(BaseModel):
    id: UUID
    article_id: UUID
    company_id: UUID
    article_title: str
    article_url: str
    relevance_type: str
    relevance_score: float
    conclusion: str
    passed_step_2: bool
    displayed: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AssessmentAskInput(BaseModel):
    question: str
    company_id: UUID | None = None
    max_items: int = 50


class AssessmentAskResponse(BaseModel):
    answer: str
    matched_titles: list[str]


class ReasoningCompanyInput(BaseModel):
    name: str
    industry: str | None = None
    description: str | None = None


class ReasoningGenerateInput(BaseModel):
    companies: list[ReasoningCompanyInput] = Field(default_factory=list)
    strictness: str = "average"
    limit: int = 25


class SettingsUpdate(BaseModel):
    mode: UserMode | None = None
    event_weights: dict[str, Any] | None = None
    sector_weights: dict[str, Any] | None = None
    company_weights: dict[str, Any] | None = None
    sensitivity: float | None = None

