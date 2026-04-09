import json
import logging
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import get_current_user_id
from .database import Base, engine, get_db, migrate_article_assessment_columns
from .config import settings
from .models import (
    Article,
    ArticleAssessment,
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
    AssessmentAskInput,
    AssessmentAskResponse,
    AssessmentOut,
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
from .services.scoring import THRESHOLDS, driver_risk_trigger_info, pick_best_company_for_article, score_with_db

app = FastAPI(
    title="Portfolio Intelligence Platform",
    openapi_url="/openapi.json",
    docs_url=None,
    redoc_url="/redoc",
)
logger = logging.getLogger(__name__)


@app.get("/docs", include_in_schema=False)
def swagger_ui() -> HTMLResponse:
    """Use unpkg for Swagger assets (some networks block cdn.jsdelivr.net, which causes a blank /docs)."""
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} — Swagger UI",
        swagger_js_url="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css",
    )


@app.get("/", response_class=HTMLResponse)
def api_home() -> str:
    """Swagger UI loads JS from a CDN; if /docs is blank, use /redoc or openapi.json."""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>Portfolio Intelligence API</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem;">
  <h1>Portfolio Intelligence Platform</h1>
  <p>API is running. Open interactive docs:</p>
  <ul>
    <li><a href="/docs">Swagger UI</a> (<code>/docs</code>) — if this page is blank, try ReDoc or check browser extensions / network blocking CDNs.</li>
    <li><a href="/redoc">ReDoc</a> (<code>/redoc</code>) — often works when Swagger does not.</li>
    <li><a href="/openapi.json"><code>openapi.json</code></a> — raw OpenAPI schema.</li>
  </ul>
</body></html>"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

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
        migrate_article_assessment_columns()
    except SQLAlchemyError as exc:
        logger.warning("Database unavailable on startup; API is up but DB operations will fail until DB is reachable: %s", exc)


def _get_user_or_404(user_id: str, db: Session) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _run_processing_for_user(user: User, db: Session) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=max(1, settings.stage1_days_back))
    articles = (
        db.query(Article)
        .filter(Article.published_at >= cutoff)
        .order_by(Article.published_at.desc())
        .limit(max(1, min(settings.stage1_candidate_limit, 500)))
        .all()
    )
    created = 0
    evaluations = []
    contexts = db.query(ContextProfile).filter(ContextProfile.user_id == user.id).all()
    pref = db.query(UserPreference).filter(UserPreference.user_id == user.id).one_or_none()

    for article in articles:
        feature = db.query(ArticleFeature).filter(ArticleFeature.article_id == article.id).one_or_none()
        if not feature:
            feature = persist_article_features(db, article)
        scored = score_with_db(db, str(user.id), article, feature, user.mode)
        passed = bool(scored["passes_threshold"])
        comp = scored.get("components") or {}
        sem_rel = float(comp.get("semantic_relevance", 0.0))
        sem_cat = str(comp.get("semantic_category", "irrelevant"))
        sem_reason = str(comp.get("semantic_reason", ""))

        db.query(ArticleAssessment).filter(
            ArticleAssessment.user_id == user.id,
            ArticleAssessment.article_id == article.id,
        ).delete(synchronize_session=False)

        if contexts:
            best_ctx, em, ev, _base_pc, _final_pc, rel_type = pick_best_company_for_article(
                article, feature, contexts, pref, sem_rel, sem_cat
            )
            if best_ctx:
                company = db.get(Company, best_ctx.company_id)
                cname = company.name if company else "company"
                trig = comp.get("driver_risk_matches") or ""
                trig_note = f" Driver/risk trigger: {trig}" if comp.get("driver_risk_triggered") and trig else ""
                conclusion = f"{sem_reason}{trig_note} · Best match: {cname} ({rel_type})."
                db.add(
                    ArticleAssessment(
                        user_id=user.id,
                        article_id=article.id,
                        company_id=best_ctx.company_id,
                        article_title=article.title,
                        article_url=article.url,
                        relevance_type=rel_type,
                        relevance_score=sem_rel,
                        base_score=float(scored["base_score"]),
                        final_score=float(scored["final_score"]),
                        semantic_category=sem_cat,
                        semantic_reason=sem_reason,
                        entity_match=em,
                        event_importance=ev,
                        conclusion=conclusion,
                        passed_step_2=passed,
                        displayed=False,
                    )
                )
        if not scored["passes_threshold"]:
            evaluations.append(
                {
                    "article_id": str(article.id),
                    "title": article.title,
                    "passed_step_2": False,
                    "displayed": False,
                }
            )
            continue
        exists = db.query(Insight).filter(Insight.article_id == article.id, Insight.user_id == user.id).one_or_none()
        if exists:
            assessment = (
                db.query(ArticleAssessment)
                .filter(ArticleAssessment.user_id == user.id, ArticleAssessment.article_id == article.id)
                .one_or_none()
            )
            if assessment:
                assessment.displayed = True
            evaluations.append(
                {
                    "article_id": str(article.id),
                    "title": article.title,
                    "passed_step_2": passed,
                    "displayed": True,
                }
            )
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
        assessment = (
            db.query(ArticleAssessment)
            .filter(ArticleAssessment.user_id == user.id, ArticleAssessment.article_id == article.id)
            .one_or_none()
        )
        if assessment:
            assessment.displayed = True
        evaluations.append(
            {
                "article_id": str(article.id),
                "title": article.title,
                "passed_step_2": passed,
                "displayed": True,
            }
        )
    db.commit()
    return {
        "insights_created": created,
        "threshold": THRESHOLDS[user.mode],
        "evaluated_count": len(articles),
        "process_article_ids": [str(a.id) for a in articles],
        "step_2_evaluations": evaluations,
        "scoring_framework": "context_profiles_v1",
    }


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
        business_model = raw_event_weights.pop("_business_model", "")
        key_drivers = raw_event_weights.pop("_key_drivers", [])
        risk_factors = raw_event_weights.pop("_risk_factors", [])
        semantic_signals = raw_event_weights.pop("_semantic_signals", [])
        context_rows.append(
            {
                "company_id": str(ctx.company_id),
                "sector": ctx.sector,
                "subsector": subsector,
                "business_model": business_model,
                "keywords": ctx.keywords,
                "competitors": ctx.competitors,
                "key_drivers": key_drivers,
                "risk_factors": risk_factors,
                "semantic_signals": semantic_signals,
                "event_weights": raw_event_weights,
                "business_signals": business_signals,
                "geography": geography,
                "priority_weight": ctx.priority_weight,
            }
        )

    article_rows = db.query(Article).order_by(Article.published_at.desc()).limit(max(1, min(limit, 100))).all()
    aid_list = [a.id for a in article_rows]
    assessment_rows: list[ArticleAssessment] = []
    if aid_list:
        assessment_rows = (
            db.query(ArticleAssessment)
            .filter(ArticleAssessment.user_id == user.id, ArticleAssessment.article_id.in_(aid_list))
            .order_by(ArticleAssessment.created_at.desc())
            .all()
        )
    assessment_by_article: dict[str, ArticleAssessment] = {}
    for ass in assessment_rows:
        key = str(ass.article_id)
        if key not in assessment_by_article:
            assessment_by_article[key] = ass

    threshold = THRESHOLDS[user.mode]
    scored_rows = []
    for article in article_rows:
        feature = db.query(ArticleFeature).filter(ArticleFeature.article_id == article.id).one_or_none()
        if not feature:
            feature = persist_article_features(db, article)
        insight = db.query(Insight).filter(Insight.article_id == article.id, Insight.user_id == user.id).one_or_none()
        assessment = assessment_by_article.get(str(article.id))

        components = None
        if assessment:
            trig = driver_risk_trigger_info(article, contexts)
            components = {
                "semantic_relevance": float(assessment.relevance_score),
                "semantic_category": assessment.semantic_category,
                "semantic_reason": assessment.semantic_reason,
                "entity_match": float(assessment.entity_match),
                "event_importance": float(assessment.event_importance),
                "driver_risk_triggered": trig["triggered"],
                "driver_risk_matches": trig.get("summary") or "",
            }

        score_payload = None
        if insight:
            score_payload = {
                "base_score": float(insight.base_score),
                "final_score": float(insight.final_score),
                "passes_threshold": float(insight.final_score) >= threshold,
                "components": components,
                "source": "insight",
            }
        elif assessment:
            score_payload = {
                "base_score": float(assessment.base_score),
                "final_score": float(assessment.final_score),
                "passes_threshold": float(assessment.final_score) >= threshold,
                "components": components,
                "source": "assessment",
            }

        matched_company_name = None
        matched_company_id = None
        if assessment:
            matched_company_id = str(assessment.company_id)
            co = db.get(Company, assessment.company_id)
            matched_company_name = co.name if co else None

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
                "matched_company_id": matched_company_id,
                "matched_company_name": matched_company_name,
                "relevance_type": assessment.relevance_type if assessment else None,
                "conclusion": assessment.conclusion if assessment else None,
                "passed_step_2": bool(assessment.passed_step_2) if assessment else None,
                "displayed": bool(assessment.displayed) if assessment else None,
                "score": score_payload,
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
            "scoring_framework": "context_profiles_v1",
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
        "trace_meta": {
            "article_limit": max(1, min(limit, 100)),
            "assessments_in_view": sum(1 for r in scored_rows if r.get("score")),
        },
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
    result = fetch_articles(db, user_id=user_id)
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
    ingest_result = fetch_articles(db, user_id=str(user.id))
    process_result = _run_processing_for_user(user, db)
    trace = _build_reasoning_trace(user, db, payload.limit)

    run_summary = {
        "scoring_framework": "context_profiles_v1",
        "step_1_fetched": ingest_result.get("fetched"),
        "ingest_inserted": ingest_result.get("inserted"),
        "ingest_inserted_article_ids": ingest_result.get("inserted_article_ids") or [],
        "ingest_source": ingest_result.get("source"),
        "newsapi_status": ingest_result.get("newsapi_status"),
        "newsapi_hint": ingest_result.get("newsapi_hint"),
        "ingest_insert_note": ingest_result.get("insert_note"),
        "process_evaluated_count": process_result.get("evaluated_count"),
        "process_article_ids": process_result.get("process_article_ids") or [],
        "insights_created": process_result.get("insights_created"),
        "threshold": process_result.get("threshold"),
        "trace_article_limit": payload.limit,
        "note": (
            "Step 1 is the ingest funnel. Step 2 (process) scores up to stage1_candidate_limit recent articles "
            "in the database, not only rows inserted in this ingest. Compare ingest_inserted_article_ids vs process_article_ids."
        ),
    }

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
        "run_summary": run_summary,
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


@app.get("/assessments", response_model=list[AssessmentOut])
def list_assessments(limit: int = 200, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    rows = (
        db.query(ArticleAssessment)
        .filter(ArticleAssessment.user_id == user_id)
        .order_by(ArticleAssessment.created_at.desc())
        .limit(max(1, min(limit, 1000)))
        .all()
    )
    return rows


@app.post("/assessments/ask", response_model=AssessmentAskResponse)
def ask_assessment_history(payload: AssessmentAskInput, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    _get_user_or_404(user_id, db)
    q = (
        db.query(ArticleAssessment)
        .filter(ArticleAssessment.user_id == user_id)
        .order_by(ArticleAssessment.created_at.desc())
    )
    if payload.company_id:
        q = q.filter(ArticleAssessment.company_id == payload.company_id)
    rows = q.limit(max(1, min(payload.max_items, 500))).all()
    if not rows:
        return AssessmentAskResponse(answer="No assessed articles found for this query.", matched_titles=[])

    context_lines = [
        f"- {r.article_title} | type={r.relevance_type} | score={r.relevance_score:.2f} | why={r.conclusion}"
        for r in rows
    ]
    prompt = (
        "You are an analyst assistant. Use only provided assessed article history.\n"
        f"Question: {payload.question}\nHistory:\n"
        + "\n".join(context_lines[:200])
        + "\nGive concise answer and mention relevant titles."
    )

    provider = (settings.context_provider or "fallback").strip().lower()
    answer = "Fallback answer: review matched titles below."
    if provider == "ollama":
        try:
            with httpx.Client(timeout=60.0) as client:
                res = client.post(
                    f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                    json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
                )
                res.raise_for_status()
                answer = (res.json().get("response") or "").strip() or answer
        except Exception:
            answer = "Ollama unavailable; fallback keyword matching used."

    question_tokens = {t for t in payload.question.lower().split() if len(t) > 2}
    matched = [
        r.article_title
        for r in rows
        if any(token in (r.article_title + " " + r.conclusion).lower() for token in question_tokens)
    ][:20]
    return AssessmentAskResponse(answer=answer, matched_titles=matched)


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

