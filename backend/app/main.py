import logging
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import get_current_user_id
from .database import Base, engine, get_db
from .models import (
    Article,
    ArticleFeature,
    Company,
    Insight,
    User,
    UserCompany,
    UserFeedback,
    UserMode,
    UserPreference,
)
from .schemas import (
    CompanyCreate,
    FeedbackCreate,
    InsightOut,
    MessageFeedbackInput,
    QueryInput,
    QueryResponse,
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
    inserted = fetch_articles(db)
    return {"inserted": inserted}


@app.post("/pipeline/process")
def process_articles(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _get_user_or_404(user_id, db)
    articles = db.query(Article).all()
    created = 0
    for article in articles:
        feature = persist_article_features(db, article)
        scored = score_with_db(db, user_id, article, feature, user.mode)
        # Enforce deterministic threshold and include exploration budget later in a daily job.
        if not scored["passes_threshold"]:
            continue
        exists = db.query(Insight).filter(Insight.article_id == article.id, Insight.user_id == user_id).one_or_none()
        if exists:
            continue
        text = generate_insight(article, "portfolio company", feature.event_type or "general")
        db.add(
            Insight(
                article_id=article.id,
                user_id=user_id,
                summary=text["summary"],
                why_it_matters=text["why_it_matters"],
                base_score=scored["base_score"],
                final_score=scored["final_score"],
            )
        )
        created += 1
    db.commit()
    return {"insights_created": created, "threshold": THRESHOLDS[user.mode]}


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

