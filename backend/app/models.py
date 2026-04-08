import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class UserMode(str, enum.Enum):
    high_signal = "high_signal"
    balanced = "balanced"
    exploratory = "exploratory"


class UserCompanyType(str, enum.Enum):
    portfolio = "portfolio"
    target = "target"


class FeedbackType(str, enum.Enum):
    click = "click"
    like = "like"
    dislike = "dislike"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    mode: Mapped[UserMode] = mapped_column(Enum(UserMode), nullable=False, default=UserMode.balanced)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    companies = relationship("UserCompany", back_populates="user")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    sector: Mapped[str] = mapped_column(String(120), nullable=True)
    subsector: Mapped[str] = mapped_column(String(120), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)


class UserCompany(Base):
    __tablename__ = "user_companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, index=True)
    type: Mapped[UserCompanyType] = mapped_column(Enum(UserCompanyType), nullable=False)

    user = relationship("User", back_populates="companies")
    company = relationship("Company")


class ContextProfile(Base):
    __tablename__ = "context_profile"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, index=True)
    sector: Mapped[str] = mapped_column(String(120), nullable=True)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    competitors: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    event_weights: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(800), unique=True, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class ArticleFeature(Base):
    __tablename__ = "article_features"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("articles.id"), nullable=False, index=True)
    entities: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    sectors: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=True)
    sentiment: Mapped[str] = mapped_column(String(40), nullable=True)
    geography: Mapped[str] = mapped_column(String(120), nullable=True)


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("articles.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[str] = mapped_column(Text, nullable=False)
    base_score: Mapped[float] = mapped_column(Float, nullable=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    insight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("insights.id"), nullable=False, index=True)
    feedback_type: Mapped[FeedbackType] = mapped_column(Enum(FeedbackType), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    event_weights: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sector_weights: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    company_weights: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    sensitivity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)


class ArticleAssessment(Base):
    __tablename__ = "article_assessments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    article_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("articles.id"), nullable=False, index=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, index=True)
    article_title: Mapped[str] = mapped_column(String(500), nullable=False)
    article_url: Mapped[str] = mapped_column(String(800), nullable=False)
    relevance_type: Mapped[str] = mapped_column(String(40), nullable=False, default="irrelevant")
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    base_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    semantic_category: Mapped[str] = mapped_column(String(40), nullable=False, default="irrelevant")
    semantic_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entity_match: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    event_importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    passed_step_2: Mapped[bool] = mapped_column(default=False, nullable=False)
    displayed: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

