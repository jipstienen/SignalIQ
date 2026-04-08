import json
import re
from collections import defaultdict
from typing import Any

import httpx
from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, ArticleFeature, ContextProfile, UserMode, UserPreference

# User-tuned sensitivity only; context profile is the primary scoring framework.
THRESHOLDS = {
    UserMode.high_signal: 0.75,
    UserMode.balanced: 0.60,
    UserMode.exploratory: 0.40,
}

# If any key driver or risk phrase matches the article, floor relevance at least this (above typical thresholds).
DRIVER_RISK_TRIGGER_FLOOR = 0.72


def _safe_max(values: list[float], default: float = 0.0) -> float:
    return max(values) if values else default


def _ew_meta(ctx: ContextProfile) -> dict[str, Any]:
    ew = dict(ctx.event_weights or {})
    return {
        "subsector": ew.get("_subsector", ""),
        "key_drivers": list(ew.get("_key_drivers", []) or []),
        "risk_factors": list(ew.get("_risk_factors", []) or []),
        "semantic_signals": list(ew.get("_semantic_signals", []) or []),
        "business_model": str(ew.get("_business_model", "") or ""),
    }


def _phrase_matches_text(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if len(phrase) < 4:
        return False
    if phrase in text:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", phrase) if len(t) >= 3]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in text)
    return hits >= max(1, (len(tokens) + 1) // 2)


def _scan_phrases(text: str, phrases: list[str]) -> list[str]:
    matched: list[str] = []
    for p in phrases:
        if not p or not str(p).strip():
            continue
        if _phrase_matches_text(text, str(p)):
            matched.append(str(p).strip())
    return matched


def driver_risk_trigger_info(article: Article, contexts: list[ContextProfile]) -> dict[str, Any]:
    """Any connection to a context key driver or risk factor is an automatic relevance trigger."""
    text = f"{article.title} {article.content}".lower()
    matched_drivers: list[str] = []
    matched_risks: list[str] = []
    for ctx in contexts:
        meta = _ew_meta(ctx)
        matched_drivers.extend(_scan_phrases(text, meta["key_drivers"]))
        matched_risks.extend(_scan_phrases(text, meta["risk_factors"]))
    # de-dupe preserving order
    def _dedupe(xs: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in xs:
            k = x.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(x)
        return out

    matched_drivers = _dedupe(matched_drivers)
    matched_risks = _dedupe(matched_risks)
    triggered = bool(matched_drivers or matched_risks)
    summary_parts: list[str] = []
    if matched_drivers:
        summary_parts.append("drivers: " + "; ".join(matched_drivers[:8]))
    if matched_risks:
        summary_parts.append("risks: " + "; ".join(matched_risks[:8]))
    return {
        "triggered": triggered,
        "matched_drivers": matched_drivers,
        "matched_risks": matched_risks,
        "summary": " | ".join(summary_parts) if summary_parts else "",
    }


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


def _keyword_coverage(article: Article, contexts: list[ContextProfile]) -> float:
    text = f"{article.title} {article.content}".lower()
    best = 0.0
    for ctx in contexts:
        kws = [k.lower() for k in ctx.keywords if k]
        if not kws:
            continue
        hits = sum(1 for k in kws if k in text)
        ratio = hits / max(1, min(25, len(kws)))
        best = max(best, ratio)
    return min(1.0, best)


def _context_relevance_fallback(
    article: Article,
    contexts: list[ContextProfile],
    trigger_info: dict[str, Any],
) -> dict[str, Any]:
    text = f"{article.title} {article.content}".lower()
    # Signals from context (not event-type weights)
    signal_hits = 0
    signal_total = 0
    for ctx in contexts:
        meta = _ew_meta(ctx)
        for s in meta["semantic_signals"]:
            if not s:
                continue
            signal_total += 1
            if _phrase_matches_text(text, str(s)):
                signal_hits += 1
    signal_score = (signal_hits / signal_total) if signal_total else 0.0

    kw_score = _keyword_coverage(article, contexts)
    tr = 1.0 if trigger_info["triggered"] else 0.0

    # Context-as-framework blend (no global event weights)
    relevance = min(
        1.0,
        0.45 * kw_score + 0.35 * signal_score + 0.20 * tr,
    )
    if trigger_info["triggered"]:
        relevance = max(relevance, DRIVER_RISK_TRIGGER_FLOOR)

    if trigger_info["triggered"]:
        category = "industry"
    elif relevance >= 0.45:
        category = "industry"
    else:
        category = "irrelevant"

    reason = "Context fallback: keyword/signal overlap"
    if trigger_info["summary"]:
        reason = f"Driver/risk trigger match. {trigger_info['summary']}"
    elif signal_hits:
        reason = f"Semantic signal overlap ({signal_hits} matches)."

    return {
        "relevance_score": round(relevance, 4),
        "reason": reason[:500],
        "category": category,
    }


def _context_relevance_llm(article: Article, contexts: list[ContextProfile], trigger_info: dict[str, Any]) -> dict[str, Any] | None:
    provider = (settings.context_provider or "fallback").strip().lower()
    context_payload = []
    for ctx in contexts:
        meta = _ew_meta(ctx)
        ew = dict(ctx.event_weights or {})
        context_payload.append(
            {
                "sector": ctx.sector,
                "subsector": meta["subsector"],
                "business_model": meta["business_model"],
                "keywords": ctx.keywords,
                "competitors": ctx.competitors,
                "key_drivers": meta["key_drivers"],
                "risk_factors": meta["risk_factors"],
                "semantic_signals": meta["semantic_signals"],
            }
        )

    trig_note = ""
    if trigger_info.get("triggered"):
        trig_note = (
            f"\nAutomatic triggers already detected from text overlap with key drivers/risks: "
            f"{trigger_info.get('summary') or 'see matched lists'}. "
            "These must be treated as highly material connections to the context.\n"
        )

    prompt = (
        "You score how relevant a news article is to the portfolio intelligence CONTEXT below.\n"
        "The context (drivers, risks, keywords, semantic signals, sector, competitors) IS the scoring framework.\n"
        "Do NOT use a separate 'event type' taxonomy or generic M&A/funding weights.\n"
        "Return STRICT JSON only:\n"
        '{ "relevance_score": 0.0, "reason": "", "category": "direct | competitor | industry | irrelevant" }\n'
        "Rules:\n"
        "- relevance_score 0.0–1.0: does the article matter for monitoring this context?\n"
        "- If it clearly connects to any key driver or risk factor, relevance_score should be high (typically >= 0.72).\n"
        f"{trig_note}"
        f"Article title: {article.title}\n"
        f"Article content: {article.content[:4000]}\n"
        f"Context profiles: {json.dumps(context_payload)[:12000]}\n"
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
            rel = max(0.0, min(1.0, float(data.get("relevance_score", 0.0))))
            if trigger_info.get("triggered"):
                rel = max(rel, DRIVER_RISK_TRIGGER_FLOOR)
            return {
                "relevance_score": rel,
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
                rel = max(0.0, min(1.0, float(data.get("relevance_score", 0.0))))
                if trigger_info.get("triggered"):
                    rel = max(rel, DRIVER_RISK_TRIGGER_FLOOR)
                return {
                    "relevance_score": rel,
                    "reason": str(data.get("reason", ""))[:500],
                    "category": str(data.get("category", "irrelevant")).lower(),
                }
    except Exception:
        return None
    return None


def _preference_multiplier(user_pref: UserPreference | None, feature: ArticleFeature, contexts: list[ContextProfile]) -> float:
    """Sensitivity + per-company weights only — context profile carries the signal."""
    if not user_pref:
        return 1.0
    mult = float(user_pref.sensitivity)
    company_map = defaultdict(lambda: 1.0, user_pref.company_weights or {})
    mult *= _safe_max([float(company_map.get(str(ctx.company_id), 1.0)) for ctx in contexts], 1.0)
    return max(0.5, min(2.0, mult))


def score_article(article: Article, feature: ArticleFeature, contexts: list[ContextProfile], user_pref: UserPreference | None) -> dict[str, Any]:
    trigger_info = driver_risk_trigger_info(article, contexts)
    cr = _context_relevance_llm(article, contexts, trigger_info) or _context_relevance_fallback(article, contexts, trigger_info)

    base_score = float(cr["relevance_score"])
    if trigger_info["triggered"]:
        base_score = max(base_score, DRIVER_RISK_TRIGGER_FLOOR)

    final_score = max(0.0, min(1.0, base_score * _preference_multiplier(user_pref, feature, contexts)))
    return {
        "base_score": round(base_score, 4),
        "final_score": round(final_score, 4),
        "components": {
            "semantic_relevance": round(base_score, 4),
            "semantic_category": cr["category"],
            "semantic_reason": cr["reason"],
            "entity_match": _entity_match(feature, contexts),
            "event_importance": 0.0,
            "driver_risk_triggered": trigger_info["triggered"],
            "driver_risk_matches": trigger_info.get("summary") or "",
        },
    }


def relevance_type_for_match(semantic_category: str, entity_match: float) -> str:
    if entity_match >= 0.8:
        return "direct"
    if entity_match >= 0.5:
        return "competitor"
    if semantic_category in {"direct", "competitor", "industry"}:
        return semantic_category
    if semantic_category == "irrelevant":
        return "irrelevant"
    return "industry"


def _company_pick_score(
    article: Article,
    feature: ArticleFeature,
    ctx: ContextProfile,
    context_relevance: float,
    trigger_info_ctx: dict[str, Any],
) -> float:
    em = _entity_match(feature, [ctx])
    text = f"{article.title} {article.content}".lower()
    meta = _ew_meta(ctx)
    kw_hits = sum(1 for k in ctx.keywords if k and str(k).lower() in text)
    kw_ratio = kw_hits / max(1, min(20, len(ctx.keywords) or 1))
    tr = 1.0 if trigger_info_ctx["triggered"] else 0.0
    return 0.45 * context_relevance + 0.25 * em + 0.15 * kw_ratio + 0.15 * tr


def pick_best_company_for_article(
    article: Article,
    feature: ArticleFeature,
    contexts: list[ContextProfile],
    user_pref: UserPreference | None,
    semantic_relevance: float,
    semantic_category: str,
) -> tuple[ContextProfile | None, float, float, float, float, str]:
    """Pick portfolio company using context fit (drivers/risks/keywords/entity), not event-weight tables."""
    best_ctx: ContextProfile | None = None
    best_pick = -1.0
    best_em = 0.0
    best_ev = 0.0
    for ctx in contexts:
        ti = driver_risk_trigger_info(article, [ctx])
        pick = _company_pick_score(article, feature, ctx, semantic_relevance, ti)
        final = max(0.0, min(1.0, pick * _preference_multiplier(user_pref, feature, [ctx])))
        if final > best_pick:
            best_pick = final
            best_ctx = ctx
            best_em = _entity_match(feature, [ctx])
            best_ev = 0.0
    rel_type = relevance_type_for_match(semantic_category, best_em) if best_ctx else "irrelevant"
    base_combo = 0.7 * semantic_relevance + 0.3 * best_em if best_ctx else 0.0
    return best_ctx, best_em, best_ev, base_combo, best_pick, rel_type


def score_with_db(db: Session, user_id: str, article: Article, feature: ArticleFeature, mode: UserMode) -> dict[str, Any]:
    contexts = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).one_or_none()
    result = score_article(article, feature, contexts, pref)
    result["base_score"] = round(float(result["components"]["semantic_relevance"]), 4)
    result["final_score"] = round(
        max(0.0, min(1.0, result["base_score"] * _preference_multiplier(pref, feature, contexts))),
        4,
    )
    result["passes_threshold"] = result["final_score"] >= THRESHOLDS[mode]
    return result
