import logging
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import get_current_user_id
from .database import Base, engine, get_db
from .config import settings
from .models import (
    Article,
    ArticleFeature,
    Company,
    ContextProfile,
    Insight,
    User,
    UserCompany,
    UserCompanyType,
    UserFeedback,
    UserMode,
    UserPreference,
)
from .schemas import (
    ArticleOut,
    CompanyCreate,
    FeedbackCreate,
    InsightOut,
    MessageFeedbackInput,
    QueryInput,
    QueryResponse,
    ReasoningGenerateInput,
    SettingsUpdate,
    UserCompanyCreate,
    UserCreate,
)
from .services.article_pipeline import fetch_articles, persist_article_features
from .services.context_engine import build_context
from .services.delivery import DAILY_LIMITS, generate_daily_report
from .services.feedback import apply_message_directive, message_to_feedback_type, update_user_preferences
from .services.insight_generation import generate_insight
from .services.scoring import THRESHOLDS, score_with_db

app = FastAPI(title="Portfolio Intelligence Platform")
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://signaliq-pi.vercel.app",
        "https://signaliq-bjxa3ifyt-jipstienen-1309s-projects.vercel.app",
    ],
    allow_origin_regex=r"^http://localhost:\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_create_tables() -> None:
    try:
        Base.metadata.create_all(bind=engine)
    except SQLAlchemyError as exc:
        logger.warning("Database unavailable on startup; API is up but DB operations will fail until DB is reachable: %s", exc)


def _get_user_or_404(user_id: str, db: Session) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _run_processing_for_user(user: User, db: Session) -> dict:
    articles = db.query(Article).all()
    created = 0
    for article in articles:
        feature = db.query(ArticleFeature).filter(ArticleFeature.article_id == article.id).one_or_none()
        if not feature:
            feature = persist_article_features(db, article)
        scored = score_with_db(db, str(user.id), article, feature, user.mode)
        if not scored["passes_threshold"]:
            continue
        exists = db.query(Insight).filter(Insight.article_id == article.id, Insight.user_id == user.id).one_or_none()
        if exists:
            continue
        text = generate_insight(article, "portfolio company", feature.event_type or "general")
        db.add(
            Insight(
                article_id=article.id,
                user_id=user.id,
                summary=text["summary"],
                why_it_matters=text["why_it_matters"],
                base_score=scored["base_score"],
                final_score=scored["final_score"],
            )
        )
        created += 1
    db.commit()
    return {"insights_created": created, "threshold": THRESHOLDS[user.mode]}


def _build_reasoning_trace(user: User, db: Session, limit: int) -> dict:
    contexts = db.query(ContextProfile).filter(ContextProfile.user_id == user.id).all()
    preference = db.query(UserPreference).filter(UserPreference.user_id == user.id).one_or_none()
    links = db.query(UserCompany).filter(UserCompany.user_id == user.id).all()

    companies = []
    for link in links:
        company = db.get(Company, link.company_id)
        if not company:
            continue
        companies.append(
            {
                "id": str(company.id),
                "name": company.name,
                "type": link.type.value,
                "sector": company.sector,
                "aliases": company.aliases,
                "description": company.description,
            }
        )

    context_rows = []
    for ctx in contexts:
        raw_event_weights = dict(ctx.event_weights or {})
        business_signals = raw_event_weights.pop("_business_signals", [])
        geography = raw_event_weights.pop("_geography", [])
        subsector = raw_event_weights.pop("_subsector", "")
        context_rows.append(
            {
                "company_id": str(ctx.company_id),
                "sector": ctx.sector,
                "subsector": subsector,
                "keywords": ctx.keywords,
                "competitors": ctx.competitors,
                "event_weights": raw_event_weights,
                "business_signals": business_signals,
                "geography": geography,
                "priority_weight": ctx.priority_weight,
            }
        )

    article_rows = db.query(Article).order_by(Article.published_at.desc()).limit(max(1, min(limit, 100))).all()
    scored_rows = []
    for article in article_rows:
        feature = db.query(ArticleFeature).filter(ArticleFeature.article_id == article.id).one_or_none()
        if not feature:
            feature = persist_article_features(db, article)
        score = score_with_db(db, str(user.id), article, feature, user.mode)
        insight = db.query(Insight).filter(Insight.article_id == article.id, Insight.user_id == user.id).one_or_none()
        scored_rows.append(
            {
                "article_id": str(article.id),
                "title": article.title,
                "source": article.source,
                "url": article.url,
                "published_at": article.published_at.isoformat(),
                "features": {
                    "entities": feature.entities,
                    "sectors": feature.sectors,
                    "event_type": feature.event_type,
                    "sentiment": feature.sentiment,
                    "geography": feature.geography,
                },
                "score": score,
                "insight_created": insight is not None,
                "insight_id": str(insight.id) if insight else None,
            }
        )

    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "mode": user.mode.value,
            "threshold": THRESHOLDS[user.mode],
            "context_provider": (settings.context_provider or "fallback"),
            "context_model": settings.ollama_model if (settings.context_provider or "").lower() == "ollama" else settings.context_model,
        },
        "companies": companies,
        "contexts": context_rows,
        "preferences": {
            "event_weights": preference.event_weights if preference else {},
            "sector_weights": preference.sector_weights if preference else {},
            "company_weights": preference.company_weights if preference else {},
            "sensitivity": preference.sensitivity if preference else 1.0,
        },
        "scored_articles": scored_rows,
    }


def _strictness_to_mode(strictness: str) -> UserMode:
    normalized = strictness.strip().lower()
    if normalized in {"very narrow", "very_narrow", "narrow"}:
        return UserMode.high_signal
    if normalized in {"wide", "broad"}:
        return UserMode.exploratory
    return UserMode.balanced


@app.post("/users")
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    user = User(email=payload.email, mode=payload.mode)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": str(user.id), "email": user.email, "mode": user.mode.value}


@app.post("/companies")
def create_company(payload: CompanyCreate, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    company = Company(**payload.model_dump())
    db.add(company)
    db.commit()
    db.refresh(company)
    return {"id": str(company.id), "name": company.name}


@app.post("/companies/link")
def link_company(payload: UserCompanyCreate, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    company = db.get(Company, payload.company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    row = UserCompany(user_id=user_id, company_id=payload.company_id, type=payload.type)
    db.add(row)
    db.commit()
    return {"linked": True}


@app.post("/context/build")
def build_user_context(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    created = build_context(user_id, db)
    return {"profiles_created_or_updated": created}


@app.post("/pipeline/ingest")
def ingest_articles(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    result = fetch_articles(db)
    return result


@app.post("/pipeline/process")
def process_articles(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _get_user_or_404(user_id, db)
    return _run_processing_for_user(user, db)


@app.get("/insights", response_model=list[InsightOut])
def list_insights(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    rows = (
        db.query(Insight)
        .filter(Insight.user_id == user_id)
        .order_by(Insight.created_at.desc())
        .limit(200)
        .all()
    )
    return rows


@app.get("/articles", response_model=list[ArticleOut])
def list_articles(limit: int = 50, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    rows = db.query(Article).order_by(Article.published_at.desc()).limit(max(1, min(limit, 200))).all()
    return rows


@app.get("/reasoning")
def reasoning_trace(limit: int = 25, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _get_user_or_404(user_id, db)
    return _build_reasoning_trace(user, db, limit)


@app.post("/reasoning/generate")
def reasoning_generate(payload: ReasoningGenerateInput, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _get_user_or_404(user_id, db)
    user.mode = _strictness_to_mode(payload.strictness)

    created_or_linked = 0
    for row in payload.companies:
        name = row.name.strip()
        if not name:
            continue
        company = db.query(Company).filter(Company.name.ilike(name)).first()
        if not company:
            company = Company(
                name=name,
                aliases=[name.lower()],
                sector=(row.industry or "").strip() or None,
                description=(row.description or "").strip() or None,
            )
            db.add(company)
            db.flush()
        else:
            if row.industry and not company.sector:
                company.sector = row.industry.strip()
            if row.description and not company.description:
                company.description = row.description.strip()

        existing_link = (
            db.query(UserCompany)
            .filter(
                UserCompany.user_id == user.id,
                UserCompany.company_id == company.id,
                UserCompany.type == UserCompanyType.portfolio,
            )
            .one_or_none()
        )
        if not existing_link:
            db.add(UserCompany(user_id=user.id, company_id=company.id, type=UserCompanyType.portfolio))
            created_or_linked += 1

    db.commit()
    context_result = {"profiles_created_or_updated": build_context(str(user.id), db)}
    ingest_result = fetch_articles(db)
    process_result = _run_processing_for_user(user, db)
    trace = _build_reasoning_trace(user, db, payload.limit)

    return {
        "strictness": payload.strictness,
        "mode": user.mode.value,
        "context_provider": (settings.context_provider or "fallback"),
        "context_model": settings.ollama_model if (settings.context_provider or "").lower() == "ollama" else settings.context_model,
        "company_rows_received": len(payload.companies),
        "companies_created_or_linked": created_or_linked,
        "context": context_result,
        "ingest": ingest_result,
        "process": process_result,
        "trace": trace,
    }


@app.get("/history", response_model=list[InsightOut])
def history(days: int = 14, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    rows = (
        db.query(Insight)
        .filter(Insight.user_id == user_id, Insight.created_at >= cutoff)
        .order_by(Insight.created_at.desc())
        .all()
    )
    return rows


@app.post("/feedback")
def create_feedback(payload: FeedbackCreate, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    insight = db.get(Insight, payload.insight_id)
    if not insight or str(insight.user_id) != user_id:
        raise HTTPException(status_code=404, detail="Insight not found")
    db.add(UserFeedback(user_id=user_id, insight_id=payload.insight_id, feedback_type=payload.feedback_type))
    db.commit()
    pref = update_user_preferences(user_id, db)
    return {"ok": True, "sensitivity": pref.sensitivity}


@app.post("/feedback/message")
def feedback_message(payload: MessageFeedbackInput, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    insight = db.get(Insight, payload.insight_id)
    if not insight or str(insight.user_id) != user_id:
        raise HTTPException(status_code=404, detail="Insight not found")
    feedback_type = message_to_feedback_type(payload.message)
    db.add(UserFeedback(user_id=user_id, insight_id=payload.insight_id, feedback_type=feedback_type))
    article_feature = db.query(ArticleFeature).filter(ArticleFeature.article_id == insight.article_id).one_or_none()
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).one_or_none()
    if not pref:
        pref = UserPreference(user_id=user_id, event_weights={}, sector_weights={}, company_weights={}, sensitivity=1.0)
        db.add(pref)
    if article_feature:
        apply_message_directive(pref, payload.message, article_feature, str(insight.user_id))
    db.commit()
    pref = update_user_preferences(user_id, db)
    return {"mapped_feedback": feedback_type.value, "sensitivity": pref.sensitivity}


@app.patch("/settings")
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _get_user_or_404(user_id, db)
    if payload.mode:
        user.mode = payload.mode
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).one_or_none()
    if not pref:
        pref = UserPreference(user_id=user_id, event_weights={}, sector_weights={}, company_weights={}, sensitivity=1.0)
        db.add(pref)
    if payload.event_weights is not None:
        pref.event_weights = payload.event_weights
    if payload.sector_weights is not None:
        pref.sector_weights = payload.sector_weights
    if payload.company_weights is not None:
        pref.company_weights = payload.company_weights
    if payload.sensitivity is not None:
        pref.sensitivity = payload.sensitivity
    db.commit()
    return {"mode": user.mode.value, "sensitivity": pref.sensitivity}


@app.post("/report/daily")
def report_daily(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _get_user_or_404(user_id, db)
    report = generate_daily_report(user, db)
    report["max_items"] = DAILY_LIMITS[user.mode]
    return report


@app.post("/query", response_model=QueryResponse)
def query(payload: QueryInput, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    cutoff = datetime.utcnow() - timedelta(days=14)
    insights = (
        db.query(Insight)
        .filter(Insight.user_id == user_id, Insight.created_at >= cutoff)
        .order_by(Insight.final_score.desc())
        .limit(10)
        .all()
    )
    if not insights:
        return QueryResponse(answer="No relevant insights found in the last 14 days.", sources=[])

    # Grounded response over retrieved insight summaries.
    context = "\n".join([f"- {i.summary} | Why: {i.why_it_matters}" for i in insights])
    answer = f"Based on recent intelligence signals: {payload.query}\n\n{context[:2000]}"
    return QueryResponse(answer=answer, sources=[UUID(str(i.id)) for i in insights])

