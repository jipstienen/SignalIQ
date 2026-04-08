from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..models import Article, ArticleFeature, ContextProfile, Insight, UserMode, UserPreference

GLOBAL_EVENT_WEIGHTS = {
    "funding": 0.7,
    "m&a": 1.0,
    "layoffs": 0.6,
    "regulatory": 0.8,
    "general": 0.4,
}

THRESHOLDS = {
    UserMode.high_signal: 0.75,
    UserMode.balanced: 0.60,
    UserMode.exploratory: 0.40,
}


def _safe_max(values: list[float], default: float = 0.0) -> float:
    return max(values) if values else default


def _entity_match(feature: ArticleFeature, contexts: list[ContextProfile]) -> float:
    scores = []
    entities = {e.lower() for e in feature.entities}
    sectors = {s.lower() for s in feature.sectors}
    for ctx in contexts:
        company_token = str(ctx.company_id).lower()
        comp_set = {c.lower() for c in ctx.competitors}
        if company_token in entities:
            scores.append(1.0)
        elif entities.intersection(comp_set):
            scores.append(0.7)
        elif ctx.sector and ctx.sector.lower() in sectors:
            scores.append(0.4)
        else:
            scores.append(0.0)
    return _safe_max(scores)


def _event_importance(feature: ArticleFeature, contexts: list[ContextProfile]) -> float:
    event_type = feature.event_type or "general"
    context_weight = _safe_max([float(ctx.event_weights.get(event_type, 0.0)) for ctx in contexts], 0.0)
    return max(GLOBAL_EVENT_WEIGHTS.get(event_type, 0.4), context_weight)


def _context_relevance(feature: ArticleFeature, contexts: list[ContextProfile]) -> float:
    entities = {e.lower() for e in feature.entities}
    sectors = {s.lower() for s in feature.sectors}
    scores = []
    for ctx in contexts:
        keywords = {k.lower() for k in ctx.keywords}
        overlap = len(entities.intersection(keywords)) / max(1, len(keywords))
        sector_hit = 1.0 if ctx.sector and ctx.sector.lower() in sectors else 0.0
        scores.append(min(1.0, (0.7 * overlap) + (0.3 * sector_hit)))
    return _safe_max(scores)


def _proximity(entity_match: float) -> float:
    # Approximate graph distance with entity match tiers.
    if entity_match >= 1.0:
        return 1.0
    if entity_match >= 0.7:
        return 0.7
    if entity_match >= 0.4:
        return 0.4
    return 0.1


def _novelty(db: Session, user_id: str, feature: ArticleFeature) -> float:
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = (
        db.query(Insight, ArticleFeature)
        .join(ArticleFeature, Insight.article_id == ArticleFeature.article_id)
        .filter(Insight.user_id == user_id, Insight.created_at >= cutoff)
        .all()
    )
    repeated = 0
    for _, f in recent:
        if f.event_type == feature.event_type:
            repeated += 1
    return max(0.0, 1.0 - (repeated * 0.1))


def _preference_multiplier(user_pref: UserPreference | None, feature: ArticleFeature, contexts: list[ContextProfile]) -> float:
    if not user_pref:
        return 1.0
    mult = user_pref.sensitivity
    event = feature.event_type or "general"
    mult *= float(user_pref.event_weights.get(event, 1.0))
    for s in feature.sectors:
        mult *= float(user_pref.sector_weights.get(s.lower(), 1.0))
    company_map = defaultdict(lambda: 1.0, user_pref.company_weights or {})
    mult *= _safe_max([float(company_map.get(str(ctx.company_id), 1.0)) for ctx in contexts], 1.0)
    return max(0.5, min(2.0, mult))


def score_article(article: Article, feature: ArticleFeature, contexts: list[ContextProfile], user_pref: UserPreference | None) -> dict[str, Any]:
    entity_match = _entity_match(feature, contexts)
    event_importance = _event_importance(feature, contexts)
    context_relevance = _context_relevance(feature, contexts)
    proximity = _proximity(entity_match)
    novelty = 1.0  # computed in wrapper using DB when available

    base_score = (
        0.35 * entity_match
        + 0.25 * event_importance
        + 0.20 * context_relevance
        + 0.10 * proximity
        + 0.10 * novelty
    )
    final_score = max(0.0, min(1.0, base_score * _preference_multiplier(user_pref, feature, contexts)))
    return {
        "base_score": round(base_score, 4),
        "final_score": round(final_score, 4),
        "components": {
            "entity_match": entity_match,
            "event_importance": event_importance,
            "context_relevance": context_relevance,
            "proximity": proximity,
            "novelty": novelty,
        },
    }


def score_with_db(db: Session, user_id: str, article: Article, feature: ArticleFeature, mode: UserMode) -> dict[str, Any]:
    contexts = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).one_or_none()
    result = score_article(article, feature, contexts, pref)
    result["components"]["novelty"] = _novelty(db, user_id, feature)
    result["base_score"] = (
        0.35 * result["components"]["entity_match"]
        + 0.25 * result["components"]["event_importance"]
        + 0.20 * result["components"]["context_relevance"]
        + 0.10 * result["components"]["proximity"]
        + 0.10 * result["components"]["novelty"]
    )
    result["final_score"] = round(max(0.0, min(1.0, result["base_score"] * _preference_multiplier(pref, feature, contexts))), 4)
    result["passes_threshold"] = result["final_score"] >= THRESHOLDS[mode]
    return result

