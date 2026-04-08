import json
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx
from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, ArticleFeature, ContextProfile, Insight, UserMode, UserPreference

GLOBAL_EVENT_WEIGHTS = {
    "funding": 0.7,
    "m&a": 1.0,
    "layoffs": 0.6,
    "regulatory": 0.8,
    "supply_chain": 0.9,
    "pricing": 0.7,
    "expansion": 0.8,
    "partnerships": 0.6,
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


def _semantic_relevance_fallback(article: Article, contexts: list[ContextProfile], entity_score: float, event_score: float) -> dict[str, Any]:
    text = f"{article.title} {article.content}".lower()
    best_overlap = 0.0
    for ctx in contexts:
        keywords = {k.lower() for k in ctx.keywords}
        if not keywords:
            continue
        overlap = sum(1 for k in keywords if k in text) / max(1, min(20, len(keywords)))
        best_overlap = max(best_overlap, overlap)

    relevance = min(1.0, 0.5 * best_overlap + 0.3 * entity_score + 0.2 * event_score)
    if entity_score >= 0.8:
        category = "direct"
    elif entity_score >= 0.5:
        category = "competitor"
    elif relevance >= 0.35:
        category = "industry"
    else:
        category = "irrelevant"
    return {
        "relevance_score": round(relevance, 4),
        "reason": "Fallback semantic relevance based on keyword overlap, entity match, and event fit.",
        "category": category,
    }


def _semantic_relevance_llm(article: Article, contexts: list[ContextProfile]) -> dict[str, Any] | None:
    provider = (settings.context_provider or "fallback").strip().lower()
    context_payload = []
    for ctx in contexts:
        ew = dict(ctx.event_weights or {})
        context_payload.append(
            {
                "sector": ctx.sector,
                "subsector": ew.get("_subsector", ""),
                "keywords": ctx.keywords,
                "competitors": ctx.competitors,
                "key_drivers": ew.get("_key_drivers", []),
                "risk_factors": ew.get("_risk_factors", []),
                "semantic_signals": ew.get("_semantic_signals", []),
                "event_weights": {k: v for k, v in ew.items() if not str(k).startswith("_")},
            }
        )

    prompt = (
        "You are an investment analyst evaluating whether a news article is relevant to a company.\n"
        "Return STRICT JSON only:\n"
        '{ "relevance_score": 0.0, "reason": "", "category": "direct | competitor | industry | irrelevant" }\n'
        f"Article title: {article.title}\n"
        f"Article content: {article.content[:4000]}\n"
        f"Company context: {json.dumps(context_payload)[:12000]}\n"
    )

    try:
        if provider == "openai" and settings.openai_api_key:
            client = OpenAI(api_key=settings.openai_api_key)
            res = client.chat.completions.create(
                model=settings.context_model or "gpt-4.1-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            content = (res.choices[0].message.content or "{}").strip()
            data = json.loads(content.strip("`").replace("json", "", 1).strip())
            return {
                "relevance_score": max(0.0, min(1.0, float(data.get("relevance_score", 0.0)))),
                "reason": str(data.get("reason", ""))[:500],
                "category": str(data.get("category", "irrelevant")).lower(),
            }
        if provider == "ollama":
            with httpx.Client(timeout=60.0) as client:
                res = client.post(
                    f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                    json={"model": settings.ollama_model, "prompt": prompt, "stream": False, "format": "json"},
                )
                res.raise_for_status()
                payload = res.json()
                data = json.loads((payload.get("response") or "{}").strip())
                return {
                    "relevance_score": max(0.0, min(1.0, float(data.get("relevance_score", 0.0)))),
                    "reason": str(data.get("reason", ""))[:500],
                    "category": str(data.get("category", "irrelevant")).lower(),
                }
    except Exception:
        return None
    return None


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
    semantic = _semantic_relevance_llm(article, contexts) or _semantic_relevance_fallback(
        article, contexts, entity_match, event_importance
    )

    base_score = (0.5 * semantic["relevance_score"]) + (0.3 * entity_match) + (0.2 * event_importance)
    final_score = max(0.0, min(1.0, base_score * _preference_multiplier(user_pref, feature, contexts)))
    return {
        "base_score": round(base_score, 4),
        "final_score": round(final_score, 4),
        "components": {
            "semantic_relevance": semantic["relevance_score"],
            "semantic_category": semantic["category"],
            "semantic_reason": semantic["reason"],
            "entity_match": entity_match,
            "event_importance": event_importance,
        },
    }


def score_with_db(db: Session, user_id: str, article: Article, feature: ArticleFeature, mode: UserMode) -> dict[str, Any]:
    contexts = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).one_or_none()
    result = score_article(article, feature, contexts, pref)
    result["base_score"] = (0.5 * result["components"]["semantic_relevance"]) + (0.3 * result["components"]["entity_match"]) + (
        0.2 * result["components"]["event_importance"]
    )
    result["final_score"] = round(
        max(0.0, min(1.0, result["base_score"] * _preference_multiplier(pref, feature, contexts))),
        4,
    )
    result["passes_threshold"] = result["final_score"] >= THRESHOLDS[mode]
    return result

