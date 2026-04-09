import json
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from dateutil.parser import isoparse
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, ArticleFeature, Company, ContextProfile, UserCompany

DEFAULT_FEEDS = [
    {
        "title": "Sample funding event in logistics software",
        "content": "ACME Logistics SaaS competitor raised new growth funding.",
        "source": "sample_feed",
        "url": "https://example.com/articles/1",
        "published_at": datetime.utcnow(),
    }
]

logger = logging.getLogger(__name__)


def _newsapi_hint(status: str) -> str | None:
    if status in ("ok", "missing_key"):
        return None
    if status.startswith("api_error:maximumResultsReached"):
        return (
            "NewsAPI free Developer plan: daily request limit or result cap reached. Wait until tomorrow, "
            "set NEWSAPI_MAX_PAGES=1 in backend/.env to use fewer API calls per ingest, or upgrade at newsapi.org."
        )
    if status.startswith("http_error:426") or status.startswith("http_error:403"):
        return (
            "NewsAPI often rejects calls from Docker (outbound IP is not localhost). The free Developer plan "
            "expects development from your machine. Try: run the API with `npm run dev` (native, port 8011) "
            "and run ingest there, upgrade NewsAPI, or continue with the sample feed."
        )
    if status.startswith("http_error:401"):
        return "Invalid or expired NEWSAPI_KEY — check https://newsapi.org account and backend/.env."
    if status.startswith("api_error:"):
        return "NewsAPI returned an error in JSON (rate limit, bad params, etc.). See API logs."
    if status == "no_newsapi_hits":
        return (
            "Two-phase ingest found no NewsAPI articles for the generated direct/broad terms. "
            "Try increasing STAGE1_DAYS_BACK or check term quality / API limits."
        )
    if status == "no_articles_after_semantic":
        return (
            "All broad-discovery articles were filtered by ingest semantic scoring. "
            "Lower NEWSAPI_SEMANTIC_MIN_SCORE, set NEWSAPI_SEMANTIC_ENABLED=false to skip, or widen contexts."
        )
    return None


def _normalize_newsapi_item(item: dict[str, Any]) -> dict[str, Any] | None:
    url = item.get("url")
    title = item.get("title")
    if not url or not title:
        return None

    content = item.get("content") or item.get("description") or title
    source_name = (item.get("source") or {}).get("name") or "newsapi"
    published_at_raw = item.get("publishedAt")

    published_at = datetime.utcnow()
    if published_at_raw:
        try:
            published_at = isoparse(published_at_raw)
        except (TypeError, ValueError):
            pass

    return {
        "title": title[:500],
        "content": content,
        "source": source_name[:200],
        "url": url[:800],
        "published_at": published_at,
    }


def _strip_json_fence(text: str) -> str:
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    return content.strip()


def _per_context_keyword_quotas(num_profiles: int, budget: int) -> list[int]:
    """Split `budget` across profiles as evenly as possible (first slots get +1 when remainder)."""
    if num_profiles <= 0 or budget <= 0:
        return []
    n = num_profiles
    base = budget // n
    rem = budget % n
    return [base + (1 if i < rem else 0) for i in range(n)]


def _heuristic_terms_single_profile(
    company: Company,
    ctx: ContextProfile,
    limit: int,
) -> list[str]:
    """Up to `limit` unique terms from one profile (same shaping rules as _collect_context_keywords)."""
    terms_ordered: list[str] = []
    seen_lower: set[str] = set()
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "a",
        "an",
        "or",
        "in",
        "on",
        "at",
        "to",
        "of",
        "is",
        "as",
        "by",
    }

    def add_term(raw: str) -> None:
        if len(terms_ordered) >= limit:
            return
        t = (raw or "").strip()
        if len(t) < 2:
            return
        low = t.lower()
        if low in seen_lower:
            return
        if len(t.split()) == 1 and low in stop:
            return
        seen_lower.add(low)
        terms_ordered.append(t)

    add_term(company.name)
    if company.sector:
        add_term(company.sector)
    if ctx.sector:
        add_term(ctx.sector)
    for kw in (ctx.keywords or [])[:30]:
        add_term(kw)
    for comp in (ctx.competitors or [])[:8]:
        add_term(str(comp))
    ew = ctx.event_weights or {}
    for sig in (ew.get("_semantic_signals") or [])[:8]:
        add_term(str(sig))
    for kd in (ew.get("_key_drivers") or [])[:6]:
        add_term(str(kd))
    for rf in (ew.get("_risk_factors") or [])[:6]:
        add_term(str(rf))
    return terms_ordered


def _interleave_keyword_blocks(blocks: list[list[str]]) -> list[str]:
    """Round-robin merge so no single context dominates the start of the NewsAPI `q` chunks."""
    if not blocks:
        return []
    out: list[str] = []
    max_len = max(len(b) for b in blocks)
    for i in range(max_len):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    return out


def _dedupe_terms_preserve_order(terms: list[str], max_total: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        low = (t or "").strip().lower()
        if len(low) < 2 or low in seen:
            continue
        seen.add(low)
        out.append(t.strip())
        if len(out) >= max_total:
            break
    return out


def _build_ollama_keyword_prompt(
    profiles: list[ContextProfile],
    companies: dict[UUID, Company],
    quotas: list[int],
) -> str:
    lines: list[str] = [
        "You generate English news search keywords for NewsAPI (everything search).",
        "Each block below is ONE portfolio monitoring context. You must allocate keywords FAIRLY:",
        "each context has an EXACT quota — produce exactly that many distinct terms for that context_id.",
        "Terms should reflect substance: sectors, themes, regulation, supply chain, competitors, geographies,",
        "and material business drivers — not fluff. Avoid generic words (company, business, industry alone).",
        "Prefer concrete phrases that would appear in headlines (2–6 words) or strong unambiguous tokens.",
        "",
        "Return STRICT JSON only in this shape:",
        '{"contexts":[{"context_id":"<uuid>","terms":["term1","term2"]}]}',
        "The length of each `terms` array MUST equal the quota for that context_id.",
        "",
        "Contexts:",
    ]
    for i, ctx in enumerate(profiles):
        cid = str(ctx.id)
        q = quotas[i] if i < len(quotas) else 0
        co = companies.get(ctx.company_id)
        name = co.name if co else "Unknown"
        ew = ctx.event_weights or {}
        bm = (ew.get("_business_model") or "")[:450]
        sub = (ew.get("_subsector") or "")[:120]
        desc = (co.description if co else "") or ""
        desc = desc[:450]
        kws = ", ".join((ctx.keywords or [])[:25])
        comps = ", ".join((ctx.competitors or [])[:10])
        drivers = ", ".join((ew.get("_key_drivers") or [])[:8])
        risks = ", ".join((ew.get("_risk_factors") or [])[:8])
        sigs = ", ".join((ew.get("_semantic_signals") or [])[:8])
        lines.append(f'--- context_id={cid} company="{name}" quota={q} ---')
        lines.append(f"profile_sector: {(ctx.sector or '')[:120]}")
        lines.append(f"company_sector: {(co.sector if co else '')[:120]} subsector: {sub}")
        lines.append(f"business_model: {bm}")
        lines.append(f"company_description: {desc}")
        lines.append(f"keywords: {kws}")
        lines.append(f"competitors: {comps}")
        lines.append(f"key_drivers: {drivers}")
        lines.append(f"risk_factors: {risks}")
        lines.append(f"semantic_signals: {sigs}")
        lines.append("")
    return "\n".join(lines)


def _ollama_collect_context_keywords(
    db: Session,
    user_id: str,
    budget: int,
) -> tuple[list[str], dict[str, Any]]:
    """
    Ask Ollama for search terms with equal per-context quotas, then interleave and dedupe.
    On failure or shortfall, pad with _heuristic_terms_single_profile / _collect_context_keywords.
    """
    profiles = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    meta: dict[str, Any] = {
        "keyword_llm": "ollama",
        "context_profiles": len(profiles),
    }
    if not profiles:
        return [], {**meta, "reason": "no_context_profiles"}

    profiles = [p for p in profiles if db.get(Company, p.company_id)]
    meta["context_profiles"] = len(profiles)
    if not profiles:
        return [], {**meta, "reason": "no_context_profiles_with_company"}

    quotas = _per_context_keyword_quotas(len(profiles), budget)
    companies: dict[UUID, Company] = {}
    for ctx in profiles:
        c = db.get(Company, ctx.company_id)
        if c:
            companies[ctx.company_id] = c

    prompt = _build_ollama_keyword_prompt(profiles, companies, quotas)
    raw_response = ""
    try:
        with httpx.Client(timeout=max(30.0, settings.newsapi_ollama_keyword_timeout)) as client:
            res = client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            res.raise_for_status()
            payload = res.json()
            raw_response = (payload.get("response") or "").strip()
    except Exception as exc:
        logger.warning("Ollama keyword generation failed (%s); using heuristic keywords.", exc)
        return [], {**meta, "reason": f"ollama_error:{type(exc).__name__}", "ollama_error": str(exc)[:200]}

    by_id: dict[str, list[str]] = {}
    try:
        data = json.loads(_strip_json_fence(raw_response))
        arr = data.get("contexts")
        if not isinstance(arr, list):
            arr = []
        for row in arr:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("context_id") or "").strip()
            terms = row.get("terms")
            if not cid or not isinstance(terms, list):
                continue
            cleaned = []
            for t in terms:
                s = str(t).strip()
                if 2 <= len(s) <= 120:
                    cleaned.append(s)
            by_id[cid] = cleaned
    except json.JSONDecodeError as exc:
        logger.warning("Ollama keyword JSON parse failed: %s", exc)
        return [], {**meta, "reason": "ollama_invalid_json", "ollama_raw_preview": raw_response[:300]}

    blocks: list[list[str]] = []
    for i, ctx in enumerate(profiles):
        cid = str(ctx.id)
        want = quotas[i] if i < len(quotas) else 0
        got = by_id.get(cid, [])
        co = companies[ctx.company_id]
        slot: list[str] = []
        seen_local: set[str] = set()
        for t in got:
            if len(slot) >= want:
                break
            low = t.lower()
            if low in seen_local:
                continue
            seen_local.add(low)
            slot.append(t)
        if len(slot) < want:
            for t in _heuristic_terms_single_profile(co, ctx, want * 2):
                if len(slot) >= want:
                    break
                low = t.lower()
                if low in seen_local:
                    continue
                seen_local.add(low)
                slot.append(t)
        blocks.append(slot[:want])

    merged = _interleave_keyword_blocks(blocks)
    merged = _dedupe_terms_preserve_order(merged, budget)

    if len(merged) < min(budget, 5):
        return [], {**meta, "reason": "ollama_too_few_terms", "terms_used": len(merged)}

    if len(merged) < budget:
        extra, _em = _collect_context_keywords(db, user_id, budget)
        for t in extra:
            if len(merged) >= budget:
                break
            merged = _dedupe_terms_preserve_order(merged + [t], budget)

    meta["terms_used"] = len(merged)
    meta["ollama_quotas"] = quotas
    meta["keyword_source"] = "ollama_balanced"
    return merged[:budget], meta


def _collect_context_keywords(db: Session, user_id: str, budget: int) -> tuple[list[str], dict[str, Any]]:
    """Up to `budget` unique search terms from context profiles (NewsAPI /v2/everything `q` building blocks)."""
    profiles = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    meta: dict[str, Any] = {"context_profiles": len(profiles)}
    if not profiles:
        return [], {**meta, "reason": "no_context_profiles"}

    terms_ordered: list[str] = []
    seen_lower: set[str] = set()
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "a",
        "an",
        "or",
        "in",
        "on",
        "at",
        "to",
        "of",
        "is",
        "as",
        "by",
    }

    def add_term(raw: str) -> None:
        if len(terms_ordered) >= budget:
            return
        t = (raw or "").strip()
        if len(t) < 2:
            return
        low = t.lower()
        if low in seen_lower:
            return
        if len(t.split()) == 1 and low in stop:
            return
        seen_lower.add(low)
        terms_ordered.append(t)

    for ctx in profiles:
        company = db.get(Company, ctx.company_id)
        if not company:
            continue
        add_term(company.name)
        if company.sector:
            add_term(company.sector)
        if ctx.sector:
            add_term(ctx.sector)
        for kw in (ctx.keywords or [])[:30]:
            add_term(kw)
        for comp in (ctx.competitors or [])[:8]:
            add_term(str(comp))
        ew = ctx.event_weights or {}
        for sig in (ew.get("_semantic_signals") or [])[:8]:
            add_term(str(sig))
        for kd in (ew.get("_key_drivers") or [])[:6]:
            add_term(str(kd))
        for rf in (ew.get("_risk_factors") or [])[:6]:
            add_term(str(rf))
        if len(terms_ordered) >= budget:
            break

    meta["terms_used"] = len(terms_ordered)
    if not terms_ordered:
        return [], {**meta, "reason": "no_terms_extracted"}
    return terms_ordered, meta


def _collect_news_keywords(db: Session, user_id: str, budget: int) -> tuple[list[str], dict[str, Any]]:
    """Prefer Ollama-balanced keywords; fall back to heuristic harvest if disabled or Ollama fails."""
    if not settings.newsapi_use_ollama_keywords:
        h, hmeta = _collect_context_keywords(db, user_id, budget)
        return h, {**hmeta, "keyword_source": "heuristic"}
    terms, ometa = _ollama_collect_context_keywords(db, user_id, budget)
    if terms:
        return terms, ometa
    h, hmeta = _collect_context_keywords(db, user_id, budget)
    return h, {
        **hmeta,
        "keyword_source": "heuristic",
        "keyword_fallback_after_ollama": True,
        "ollama_attempt": ometa,
    }


def _ingest_methodology_from_kw_meta(kw_meta: dict[str, Any]) -> str:
    if kw_meta.get("keyword_source") == "ollama_balanced":
        return "ollama_balanced_keyword_batch_everything_then_semantic_process"
    if kw_meta.get("keyword_fallback_after_ollama"):
        return "heuristic_keyword_batch_after_ollama_failure_then_semantic_process"
    return "heuristic_keyword_batch_everything_then_semantic_process"


def _split_keywords_into_q_chunks(keywords: list[str], max_chars: int) -> list[str]:
    """NewsAPI `q` max length 500 chars; split OR-clauses across multiple query strings."""
    chunks: list[str] = []
    current: list[str] = []
    max_chars = max(80, min(max_chars, 500))

    for t in keywords:
        safe = (t or "").replace('"', "").strip()
        if not safe:
            continue
        piece = f'"{safe}"' if (" " in safe or "&" in safe or ":" in safe) else safe
        trial = " OR ".join(current + [piece])
        if len(trial) > max_chars and current:
            chunks.append(" OR ".join(current))
            current = [piece]
            if len(" OR ".join(current)) > max_chars:
                current = [piece[: max(20, max_chars - 20)]]
        else:
            current.append(piece)
    if current:
        chunks.append(" OR ".join(current))
    return [c for c in chunks if c.strip()]


def _newsapi_headers() -> dict[str, str]:
    return {
        "X-Api-Key": settings.newsapi_key,
        "User-Agent": "SignalIQ/1.0 (local dev; https://github.com/)",
        "Accept": "application/json",
    }


def _article_payload_for_db(item: dict[str, Any]) -> dict[str, Any]:
    """SQLAlchemy Article only accepts table columns; strip ingest-only keys."""
    return {
        "title": item["title"],
        "content": item["content"],
        "source": item["source"],
        "url": item["url"],
        "published_at": item["published_at"],
    }


def _ollama_post_json(
    prompt: str,
    *,
    timeout: float,
    num_predict: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST /api/generate with format=json; returns parsed object or None."""
    body: dict[str, Any] = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
    if num_predict is not None:
        body["options"] = {"num_predict": num_predict}
    try:
        with httpx.Client(timeout=timeout) as client:
            res = client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json=body,
            )
            res.raise_for_status()
            payload = res.json()
            raw = (payload.get("response") or "").strip()
            data = json.loads(_strip_json_fence(raw))
            return data, None
    except Exception as exc:
        logger.warning("Ollama JSON call failed: %s", exc)
        return None, str(exc)[:300]


def _context_summary_for_semantic(db: Session, user_id: str) -> str:
    """Compact text of all context profiles for brief relevance judging."""
    profiles = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    parts: list[str] = []
    budget = max(500, settings.newsapi_semantic_context_max_chars)
    used = 0
    for ctx in profiles:
        co = db.get(Company, ctx.company_id)
        if not co:
            continue
        ew = ctx.event_weights or {}
        block = (
            f"Company: {co.name}\n"
            f"Sector: {ctx.sector or co.sector or ''}\n"
            f"Keywords: {', '.join((ctx.keywords or [])[:20])}\n"
            f"Competitors: {', '.join((ctx.competitors or [])[:8])}\n"
            f"Drivers: {', '.join((ew.get('_key_drivers') or [])[:6])}\n"
            f"Risks: {', '.join((ew.get('_risk_factors') or [])[:6])}\n"
            f"Signals: {', '.join((ew.get('_semantic_signals') or [])[:6])}\n"
        )
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "\n---\n".join(parts)[:budget]


def _heuristic_direct_terms(
    db: Session,
    user_id: str,
    cap: int,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    profiles = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    for ctx in profiles:
        co = db.get(Company, ctx.company_id)
        if not co:
            continue
        for t in (co.name, co.name.split()[0] if co.name else ""):
            s = (t or "").strip()
            if len(s) >= 2 and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
            if len(out) >= cap:
                return out
        for c in (ctx.competitors or [])[:8]:
            s = str(c).strip()
            if len(s) >= 2 and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
            if len(out) >= cap:
                return out
    return out[:cap]


def _heuristic_broad_terms_from_context(db: Session, user_id: str, cap: int) -> list[str]:
    """Reuse existing harvest as broad fallback."""
    terms, _ = _collect_context_keywords(db, user_id, cap)
    return terms


def _ollama_direct_hits_terms(
    db: Session,
    user_id: str,
    cap: int,
) -> tuple[list[str], dict[str, Any]]:
    """Company names, product names, competitor names — if in article, treat as high-confidence."""
    profiles = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    profiles = [p for p in profiles if db.get(Company, p.company_id)]
    meta: dict[str, Any] = {"phase": "direct_terms", "ollama": True}
    if not profiles:
        return [], {**meta, "reason": "no_profiles"}

    lines = [
        "You list search terms for NewsAPI /v2/everything.",
        "ONLY output proper nouns and exact names: legal company names, brand names, flagship product names,",
        "and competitor company names. These must be strings that would appear verbatim in a news article when it is about that entity.",
        "Do NOT include generic industry words (e.g. logistics, healthcare SaaS, supply chain) alone.",
        f"Return STRICT JSON: {{\"direct_terms\": [\"...\"]}} with at most {cap} distinct strings.",
        "Contexts:",
    ]
    for ctx in profiles:
        co = db.get(Company, ctx.company_id)
        if not co:
            continue
        ew = ctx.event_weights or {}
        lines.append(
            f'--- company="{co.name}" ---\n'
            f"description: {(co.description or '')[:400]}\n"
            f"competitors: {', '.join((ctx.competitors or [])[:12])}\n"
            f"profile_keywords (may include product hints): {', '.join((ctx.keywords or [])[:15])}\n"
        )
    prompt = "\n".join(lines)
    if not settings.newsapi_use_ollama_keywords:
        h = _heuristic_direct_terms(db, user_id, cap)
        return h, {**meta, "ollama": False, "reason": "ollama_keywords_disabled"}

    data, err = _ollama_post_json(
        prompt,
        timeout=max(30.0, settings.newsapi_ollama_keyword_timeout),
        num_predict=512,
    )
    if not data or err:
        h = _heuristic_direct_terms(db, user_id, cap)
        return h, {**meta, "ollama_error": err or "empty", "fallback": "heuristic_direct"}

    terms_raw = data.get("direct_terms")
    if not isinstance(terms_raw, list):
        h = _heuristic_direct_terms(db, user_id, cap)
        return h, {**meta, "fallback": "heuristic_direct_bad_json"}

    cleaned: list[str] = []
    seen: set[str] = set()
    for t in terms_raw:
        s = str(t).strip()
        if 2 <= len(s) <= 100 and s.lower() not in seen:
            seen.add(s.lower())
            cleaned.append(s)
        if len(cleaned) >= cap:
            break
    if len(cleaned) < 3:
        h = _heuristic_direct_terms(db, user_id, cap)
        return h, {**meta, "fallback": "heuristic_direct_too_few", "ollama_raw_count": len(terms_raw)}
    return cleaned, {**meta, "terms_used": len(cleaned)}


def _ollama_broad_context_terms(
    db: Session,
    user_id: str,
    cap: int,
) -> tuple[list[str], dict[str, Any]]:
    """Broader industry / theme / synonym terms for second NewsAPI pass."""
    profiles = db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all()
    profiles = [p for p in profiles if db.get(Company, p.company_id)]
    meta: dict[str, Any] = {"phase": "broad_terms", "ollama": True}
    if not profiles:
        return [], {**meta, "reason": "no_profiles"}

    lines = [
        "You list search terms for NewsAPI /v2/everything second pass (broad discovery).",
        "Include industry themes, synonyms, regional markets, regulatory topics, technology themes,",
        "and multi-word phrases that match the CONTEXT of each company — NOT duplicate company legal names.",
        "Avoid repeating the same token as the direct-name pass.",
        f"Return STRICT JSON: {{\"broad_terms\": [\"...\"]}} with at most {cap} distinct strings.",
        "Contexts:",
    ]
    for ctx in profiles:
        co = db.get(Company, ctx.company_id)
        if not co:
            continue
        ew = ctx.event_weights or {}
        lines.append(
            f'--- company="{co.name}" ---\n'
            f"sector: {ctx.sector or co.sector or ''}\n"
            f"subsector: {str(ew.get('_subsector') or '')[:120]}\n"
            f"business_model: {(ew.get('_business_model') or '')[:400]}\n"
            f"keywords: {', '.join((ctx.keywords or [])[:20])}\n"
            f"semantic_signals: {', '.join((ew.get('_semantic_signals') or [])[:8])}\n"
            f"key_drivers: {', '.join((ew.get('_key_drivers') or [])[:6])}\n"
            f"risk_factors: {', '.join((ew.get('_risk_factors') or [])[:6])}\n"
        )
    prompt = "\n".join(lines)
    if not settings.newsapi_use_ollama_keywords:
        h = _heuristic_broad_terms_from_context(db, user_id, cap)
        return h, {**meta, "ollama": False}

    data, err = _ollama_post_json(
        prompt,
        timeout=max(30.0, settings.newsapi_ollama_keyword_timeout),
        num_predict=768,
    )
    if not data or err:
        h = _heuristic_broad_terms_from_context(db, user_id, cap)
        return h, {**meta, "ollama_error": err or "empty", "fallback": "heuristic_broad"}

    terms_raw = data.get("broad_terms")
    if not isinstance(terms_raw, list):
        h = _heuristic_broad_terms_from_context(db, user_id, cap)
        return h, {**meta, "fallback": "heuristic_broad_bad_json"}

    cleaned: list[str] = []
    seen: set[str] = set()
    for t in terms_raw:
        s = str(t).strip()
        if 2 <= len(s) <= 100 and s.lower() not in seen:
            seen.add(s.lower())
            cleaned.append(s)
        if len(cleaned) >= cap:
            break
    if len(cleaned) < 4:
        h = _heuristic_broad_terms_from_context(db, user_id, cap)
        return h, {**meta, "fallback": "heuristic_broad_too_few"}
    return cleaned, {**meta, "terms_used": len(cleaned)}


def _newsapi_fetch_terms_until_target(
    keywords: list[str],
    target: int,
    excluded_urls: set[str],
    sort_by: str,
    from_date: str,
    max_batches: int,
    retrieval_tier: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run GET /everything with q chunks built from keywords; skip duplicates and excluded URLs."""
    chunks = _split_keywords_into_q_chunks(keywords, settings.newsapi_query_max_chars)
    meta: dict[str, Any] = {
        "q_chunk_count": len(chunks),
        "requests_made": 0,
        "chunks_preview": [c[:200] for c in chunks[:5]],
    }
    if not chunks:
        return [], meta

    out: list[dict[str, Any]] = []
    local_seen: set[str] = set()
    requests_made = 0
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True, http2=False) as client:
            for q in chunks[:max_batches]:
                if len(out) >= target:
                    break
                need = target - len(out)
                page_size = max(1, min(100, need, settings.newsapi_page_size))
                params = {
                    "q": q,
                    "language": "en",
                    "sortBy": sort_by,
                    "from": from_date,
                    "pageSize": page_size,
                    "page": 1,
                }
                requests_made += 1
                res = client.get(settings.newsapi_url, params=params, headers=_newsapi_headers())
                res.raise_for_status()
                payload = res.json()
                if payload.get("status") != "ok":
                    code = payload.get("code") or payload.get("message") or "unknown"
                    meta["requests_made"] = requests_made
                    meta["last_error"] = str(code)
                    return out, meta
                for item in payload.get("articles", []) or []:
                    row = _normalize_newsapi_item(item)
                    if not row:
                        continue
                    u = row["url"]
                    if u in excluded_urls or u in local_seen:
                        continue
                    local_seen.add(u)
                    row["retrieval_tier"] = retrieval_tier
                    row["retrieval_search"] = "direct_hits" if retrieval_tier == "direct" else "broad_context"
                    out.append(row)
                    if len(out) >= target:
                        meta["requests_made"] = requests_made
                        meta["query_used_preview"] = q[:300]
                        return out, meta
    except httpx.HTTPStatusError as exc:
        meta["http_error"] = exc.response.status_code
        meta["requests_made"] = requests_made
        return out, meta
    except httpx.RequestError as exc:
        meta["network_error"] = type(exc).__name__
        meta["requests_made"] = requests_made
        return out, meta
    except Exception as exc:
        meta["fetch_error"] = type(exc).__name__
        meta["requests_made"] = requests_made
        return out, meta

    meta["requests_made"] = requests_made
    meta["query_used_preview"] = chunks[0][:300] if chunks else ""
    return out, meta


def _semantic_keyword_fallback(
    article: dict[str, Any],
    context_keywords: set[str],
) -> tuple[bool, float, str]:
    """If Ollama fails, keep broad article only if headline/snippet overlaps context keywords."""
    text = f"{article.get('title', '')} {article.get('content', '')}".lower()
    hits = [k for k in context_keywords if k and len(k) > 2 and k.lower() in text]
    if not hits:
        return False, 0.0, "no_keyword_overlap_fallback"
    return True, min(0.85, 0.35 + 0.1 * len(hits)), f"keyword_overlap:{','.join(hits[:4])}"


def _ollama_brief_semantic_batch(
    articles: list[dict[str, Any]],
    context_summary: str,
    context_keywords: set[str],
) -> list[dict[str, Any]]:
    """Brief relevance judgment for broad-only articles. Hard limits on prompt size and output tokens."""
    snip = max(80, settings.newsapi_semantic_snippet_chars)
    lines: list[str] = [
        "You are a strict relevance filter. Each article must be judged against the CONTEXTS below.",
        "Direct entity mentions were already searched separately; these are broad-discovery hits — be conservative.",
        "Return STRICT JSON only: {\"judgments\":[{\"url\":\"\",\"keep\":true,\"score\":0.0,\"note\":\"max 10 words\"}]}",
        "score is 0.0–1.0. keep=true only if clearly relevant to at least one monitoring context.",
        "Be brief. Notes under 10 words.",
        "",
        "CONTEXTS:",
        context_summary[: settings.newsapi_semantic_context_max_chars],
        "",
        "ARTICLES:",
    ]
    for i, a in enumerate(articles, 1):
        u = a.get("url", "")
        title = (a.get("title") or "")[:200]
        body = (a.get("content") or "")[:snip]
        lines.append(f"{i}. url={u}\n   title={title}\n   snippet={body}\n")

    prompt = "\n".join(lines)
    data, err = _ollama_post_json(
        prompt,
        timeout=max(25.0, settings.newsapi_semantic_timeout),
        num_predict=settings.newsapi_semantic_ollama_num_predict,
    )
    by_url: dict[str, dict[str, Any]] = {}
    if data and isinstance(data.get("judgments"), list):
        for row in data["judgments"]:
            if not isinstance(row, dict):
                continue
            u = str(row.get("url") or "").strip()
            if not u:
                continue
            try:
                score = float(row.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            score = max(0.0, min(1.0, score))
            # Score is authoritative; "keep" is a hint when score is borderline.
            keep = score >= settings.newsapi_semantic_min_score
            by_url[u] = {
                "ingest_semantic_keep": keep,
                "ingest_semantic_score": score,
                "ingest_semantic_note": str(row.get("note") or "")[:120],
                "ingest_semantic_source": "ollama",
            }
    else:
        logger.warning("Ingest semantic batch parse failed: %s", err)

    results: list[dict[str, Any]] = []
    for a in articles:
        u = a.get("url", "")
        if u in by_url:
            merged = {**a, **by_url[u]}
            if not merged.get("ingest_semantic_keep"):
                merged["ingest_semantic_keep"] = False
            results.append(merged)
            continue
        ok, sc, note = _semantic_keyword_fallback(a, context_keywords)
        results.append(
            {
                **a,
                "ingest_semantic_keep": ok and sc >= settings.newsapi_semantic_min_score,
                "ingest_semantic_score": sc,
                "ingest_semantic_note": note,
                "ingest_semantic_source": "keyword_fallback",
            }
        )
    return results


def _apply_ingest_semantic_filter(
    db: Session,
    user_id: str,
    merged: list[dict[str, Any]],
    broad_keywords: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Direct tier: always keep. Broad tier: brief Ollama batches (or keyword fallback)."""
    trace: dict[str, Any] = {
        "semantic_enabled": settings.newsapi_semantic_enabled,
        "min_score": settings.newsapi_semantic_min_score,
        "batches": [],
    }
    direct_rows = [dict(x) for x in merged if x.get("retrieval_tier") == "direct"]
    for d in direct_rows:
        d["ingest_semantic_keep"] = True
        d["ingest_semantic_score"] = 1.0
        d["ingest_semantic_note"] = "direct_name_or_entity_tier"
        d["ingest_semantic_source"] = "skipped_direct_tier"

    broad_rows = [dict(x) for x in merged if x.get("retrieval_tier") == "broad"]
    if not broad_rows:
        final = direct_rows
        trace["broad_articles_judged"] = 0
        return final, trace

    if not settings.newsapi_semantic_enabled or not settings.newsapi_use_ollama_keywords:
        for b in broad_rows:
            b["ingest_semantic_keep"] = True
            b["ingest_semantic_score"] = 0.75
            b["ingest_semantic_note"] = "semantic_disabled_pass_through"
            b["ingest_semantic_source"] = "config_skip"
        trace["note"] = "semantic_disabled_pass_through"
        return direct_rows + broad_rows, trace

    ctx_summary = _context_summary_for_semantic(db, user_id)
    kw_set = {k.lower() for k in broad_keywords if k}
    for ctx in db.query(ContextProfile).filter(ContextProfile.user_id == user_id).all():
        for k in ctx.keywords or []:
            kw_set.add(str(k).lower())
        co = db.get(Company, ctx.company_id)
        if co and co.name:
            kw_set.add(co.name.lower())

    bs = max(3, min(20, settings.newsapi_semantic_batch_size))
    judged: list[dict[str, Any]] = []
    start = 0
    while start < len(broad_rows):
        batch = broad_rows[start : start + bs]
        start += bs
        batch_out = _ollama_brief_semantic_batch(batch, ctx_summary, kw_set)
        trace["batches"].append(
            {
                "size": len(batch),
                "urls": [x.get("url") for x in batch],
            }
        )
        judged.extend(batch_out)

    kept = [x for x in judged if x.get("ingest_semantic_keep")]
    dropped = [x for x in judged if not x.get("ingest_semantic_keep")]
    trace["broad_articles_judged"] = len(judged)
    trace["broad_articles_kept"] = len(kept)
    trace["broad_articles_dropped"] = len(dropped)
    trace["dropped_broad_articles"] = [
        {
            "url": x.get("url"),
            "title": (x.get("title") or "")[:200],
            "ingest_semantic_score": x.get("ingest_semantic_score"),
            "ingest_semantic_note": x.get("ingest_semantic_note"),
            "ingest_semantic_source": x.get("ingest_semantic_source"),
        }
        for x in dropped[:60]
    ]
    final = direct_rows + kept
    return final, trace


def _fetch_newsapi_two_phase(
    db: Session,
    user_id: str,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """
    Step 1: Ollama direct-name terms → NewsAPI (high-confidence).
    Step 2: Ollama broad terms → NewsAPI (excluding URLs from step 1).
    Step 3: Brief Ollama semantic filter on broad-only rows.
    """
    cap_d = max(1, min(settings.newsapi_direct_term_max, 60))
    cap_b = max(1, min(settings.newsapi_broad_term_max, 80))
    target_d = max(1, min(settings.newsapi_direct_article_cap, 100))
    target_b = max(1, min(settings.newsapi_broad_article_cap, 100))

    from_date = (datetime.utcnow() - timedelta(days=max(1, settings.stage1_days_back))).date().isoformat()
    raw_sort = (settings.newsapi_context_sort_by or "relevancy").strip().lower()
    sort_map = {"relevancy": "relevancy", "popularity": "popularity", "publishedat": "publishedAt"}
    sort_by = sort_map.get(raw_sort, "relevancy")

    direct_terms, meta_d = _ollama_direct_hits_terms(db, user_id, cap_d)
    broad_terms, meta_b = _ollama_broad_context_terms(db, user_id, cap_b)

    direct_articles: list[dict[str, Any]] = []
    d_fetch_meta: dict[str, Any] = {}
    if direct_terms:
        direct_articles, d_fetch_meta = _newsapi_fetch_terms_until_target(
            direct_terms,
            target_d,
            set(),
            sort_by,
            from_date,
            max(1, settings.newsapi_max_batches_direct),
            "direct",
        )

    direct_urls = {a["url"] for a in direct_articles}
    broad_articles: list[dict[str, Any]] = []
    b_fetch_meta: dict[str, Any] = {}
    if broad_terms:
        broad_articles, b_fetch_meta = _newsapi_fetch_terms_until_target(
            broad_terms,
            target_b,
            direct_urls,
            sort_by,
            from_date,
            max(1, settings.newsapi_max_batches_broad),
            "broad",
        )

    merged = list(direct_articles) + list(broad_articles)
    sem_trace: dict[str, Any] = {}
    final_articles: list[dict[str, Any]] = []
    if merged:
        final_articles, sem_trace = _apply_ingest_semantic_filter(db, user_id, merged, broad_terms)

    retrieval_trace = {
        "merged_before_semantic": [
            {
                "url": a.get("url"),
                "title": (a.get("title") or "")[:240],
                "retrieval_tier": a.get("retrieval_tier"),
                "retrieval_search": a.get("retrieval_search"),
            }
            for a in merged[:80]
        ],
        "step_1_direct_terms": {
            "terms": direct_terms,
            "meta": meta_d,
            "newsapi": d_fetch_meta,
            "articles_retrieved": [
                {
                    "url": a.get("url"),
                    "title": a.get("title"),
                    "retrieval_tier": a.get("retrieval_tier"),
                    "retrieval_search": a.get("retrieval_search"),
                }
                for a in direct_articles
            ],
        },
        "step_2_broad_terms": {
            "terms": broad_terms,
            "meta": meta_b,
            "newsapi": b_fetch_meta,
            "excluded_urls_from_direct_count": len(direct_urls),
            "articles_retrieved": [
                {
                    "url": a.get("url"),
                    "title": a.get("title"),
                    "retrieval_tier": a.get("retrieval_tier"),
                    "retrieval_search": a.get("retrieval_search"),
                }
                for a in broad_articles
            ],
        },
        "step_3_semantic_filter": sem_trace,
        "merged_before_semantic_count": len(merged),
        "final_after_semantic_count": len(final_articles),
        "scored_articles": [
            {
                "url": a.get("url"),
                "title": a.get("title"),
                "retrieval_tier": a.get("retrieval_tier"),
                "retrieval_search": a.get("retrieval_search"),
                "ingest_semantic_score": a.get("ingest_semantic_score"),
                "ingest_semantic_note": a.get("ingest_semantic_note"),
                "ingest_semantic_source": a.get("ingest_semantic_source"),
                "ingest_semantic_keep": a.get("ingest_semantic_keep"),
            }
            for a in final_articles
        ],
    }

    meta: dict[str, Any] = {
        "query_source": "context_two_phase",
        "ingest_methodology": "two_phase_newsapi_direct_then_broad_then_brief_semantic",
        "retrieval_trace": retrieval_trace,
        "newsapi_target_articles": len(final_articles),
    }

    if not direct_terms and not broad_terms:
        return [], "no_context_keywords", meta

    if not merged:
        return [], "no_newsapi_hits", meta

    if not final_articles:
        return [], "no_articles_after_semantic", meta

    return final_articles, "ok", meta


def _fetch_newsapi_items_context_batches(
    db: Session,
    user_id: str,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """
    Methodology: collect keyword budget from context profiles → batch into valid `q` strings
    (see NewsAPI /v2/everything) → one GET per batch until we have `newsapi_ingest_target_articles` unique URLs.
    Company↔article assignment happens later in process (context_profiles_v1 / semantic scoring).
    """
    budget = max(5, min(settings.newsapi_context_keyword_budget, 80))
    target = max(1, min(settings.newsapi_ingest_target_articles, 100))
    keywords, kw_meta = _collect_news_keywords(db, user_id, budget)
    meta: dict[str, Any] = {
        "query_source": "context_keyword_batches",
        "ingest_methodology": _ingest_methodology_from_kw_meta(kw_meta),
        "newsapi_context_keywords": keywords,
        "newsapi_target_articles": target,
        "semantic_routing_note": (
            "Ingest only retrieves candidates; process step scores and picks best company per article "
            "(context_profiles_v1, optional LLM)."
        ),
    }
    meta.update(kw_meta)

    if not keywords:
        return [], "no_context_keywords", meta

    chunks = _split_keywords_into_q_chunks(keywords, settings.newsapi_query_max_chars)
    meta["newsapi_q_chunk_count"] = len(chunks)
    if not chunks:
        return [], "no_q_chunks", meta

    from_date = (datetime.utcnow() - timedelta(days=max(1, settings.stage1_days_back))).date().isoformat()
    raw_sort = (settings.newsapi_context_sort_by or "relevancy").strip().lower()
    sort_map = {"relevancy": "relevancy", "popularity": "popularity", "publishedat": "publishedAt"}
    sort_by = sort_map.get(raw_sort, "relevancy")

    seen_urls: set[str] = set()
    normalized: list[dict[str, Any]] = []
    requests_made = 0
    max_batches = max(1, min(settings.newsapi_max_request_batches, len(chunks)))

    try:
        with httpx.Client(timeout=25.0, follow_redirects=True, http2=False) as client:
            for q in chunks[:max_batches]:
                if len(normalized) >= target:
                    break
                need = target - len(normalized)
                page_size = max(1, min(100, need, settings.newsapi_page_size))
                params = {
                    "q": q,
                    "language": "en",
                    "sortBy": sort_by,
                    "from": from_date,
                    "pageSize": page_size,
                    "page": 1,
                }
                requests_made += 1
                res = client.get(settings.newsapi_url, params=params, headers=_newsapi_headers())
                res.raise_for_status()
                try:
                    payload = res.json()
                except json.JSONDecodeError:
                    snippet = (res.text or "")[:200].replace("\n", " ")
                    logger.warning("NewsAPI returned non-JSON: %s", snippet)
                    return [], f"invalid_response:{snippet[:80]}", {**meta, "requests_made": requests_made}
                if payload.get("status") != "ok":
                    code = payload.get("code") or payload.get("message") or "unknown"
                    logger.warning("NewsAPI non-ok payload: %s", payload)
                    return [], f"api_error:{code}", {**meta, "requests_made": requests_made}
                for item in payload.get("articles", []) or []:
                    row = _normalize_newsapi_item(item)
                    if row and row["url"] not in seen_urls:
                        seen_urls.add(row["url"])
                        normalized.append(row)
                        if len(normalized) >= target:
                            meta["requests_made"] = requests_made
                            meta["query_used_preview"] = q[:300]
                            return normalized, "ok", meta
    except httpx.HTTPStatusError as exc:
        text = ""
        try:
            text = exc.response.text or ""
        except Exception:
            pass
        logger.warning("NewsAPI HTTP %s: %s", exc.response.status_code, text[:500] or exc)
        try:
            err_body = json.loads(text)
            if err_body.get("status") == "error" and err_body.get("code"):
                return [], f"api_error:{err_body['code']}", {**meta, "requests_made": requests_made}
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
        detail = text[:300].replace("\n", " ")
        return [], f"http_error:{exc.response.status_code}:{detail[:120]}", {**meta, "requests_made": requests_made}
    except httpx.RequestError as exc:
        logger.warning("NewsAPI network error: %s", exc)
        return [], f"network_error:{type(exc).__name__}", {**meta, "requests_made": requests_made}
    except Exception as exc:
        logger.warning("NewsAPI batch fetch failed: %s", exc)
        return [], f"fetch_failed:{type(exc).__name__}", {**meta, "requests_made": requests_made}

    meta["requests_made"] = requests_made
    meta["query_used_preview"] = chunks[0][:300] if chunks else ""
    return normalized, "ok", meta


def _fetch_newsapi_items_default_query(meta_base: dict[str, Any]) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Legacy: single broad `q`, paginate with publishedAt."""
    meta = dict(meta_base)
    query = settings.newsapi_query
    meta["query_source"] = meta.get("query_source") or "default"
    meta["query_used"] = query[:500]

    max_fetch = max(1, min(settings.stage1_max_fetch, 1000))
    page_size = max(1, min(settings.newsapi_page_size, 100))
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "from": (datetime.utcnow() - timedelta(days=max(1, settings.stage1_days_back))).date().isoformat(),
        "pageSize": page_size,
    }

    normalized: list[dict[str, Any]] = []
    requested_pages = max(1, (max_fetch + page_size - 1) // page_size)
    pages = min(requested_pages, max(1, settings.newsapi_max_pages))
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, http2=False) as client:
            for page in range(1, pages + 1):
                page_params = {**params, "page": page}
                res = client.get(settings.newsapi_url, params=page_params, headers=_newsapi_headers())
                res.raise_for_status()
                try:
                    payload = res.json()
                except json.JSONDecodeError:
                    snippet = (res.text or "")[:200].replace("\n", " ")
                    logger.warning("NewsAPI returned non-JSON: %s", snippet)
                    return [], f"invalid_response:{snippet[:80]}", meta
                if payload.get("status") != "ok":
                    code = payload.get("code") or payload.get("message") or "unknown"
                    logger.warning("NewsAPI non-ok payload: %s", payload)
                    return [], f"api_error:{code}", meta
                articles = payload.get("articles", [])
                if not articles:
                    break
                for item in articles:
                    row = _normalize_newsapi_item(item)
                    if row:
                        normalized.append(row)
                        if len(normalized) >= max_fetch:
                            return normalized, "ok", meta
    except httpx.HTTPStatusError as exc:
        text = ""
        try:
            text = exc.response.text or ""
        except Exception:
            pass
        logger.warning("NewsAPI HTTP %s: %s", exc.response.status_code, text[:500] or exc)
        try:
            err_body = json.loads(text)
            if err_body.get("status") == "error" and err_body.get("code"):
                return [], f"api_error:{err_body['code']}", meta
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
        detail = text[:300].replace("\n", " ")
        suffix = f":{detail[:120]}" if detail else ""
        return [], f"http_error:{exc.response.status_code}{suffix}", meta
    except httpx.RequestError as exc:
        logger.warning("NewsAPI network error: %s", exc)
        return [], f"network_error:{type(exc).__name__}", meta
    except Exception as exc:
        logger.warning("NewsAPI fetch failed; using default sample feed: %s", exc)
        return [], f"fetch_failed:{type(exc).__name__}", meta
    return normalized, "ok", meta


def _fetch_newsapi_items(
    db: Session | None = None,
    user_id: str | None = None,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    meta: dict[str, Any] = {
        "query_source": "default",
        "query_used": settings.newsapi_query,
    }
    if not settings.newsapi_key:
        logger.info("NEWSAPI_KEY not set; using default sample feed.")
        return [], "missing_key", meta

    if settings.newsapi_use_context_query and db is not None and user_id:
        if settings.newsapi_two_phase_ingest:
            items, status, cmeta = _fetch_newsapi_two_phase(db, user_id)
            if status == "ok":
                return items, status, cmeta
            if status in ("no_articles_after_semantic", "no_newsapi_hits"):
                return items, status, cmeta
            meta["query_source"] = "default_no_context"
            meta["context_note"] = cmeta.get("reason", status)
        else:
            items, status, cmeta = _fetch_newsapi_items_context_batches(db, user_id)
            if status not in ("no_context_keywords", "no_q_chunks"):
                return items, status, cmeta
            meta["query_source"] = "default_no_context"
            meta["context_note"] = cmeta.get("reason", status)

    return _fetch_newsapi_items_default_query(meta)


def _build_generic_terms(db: Session, user_id: str | None) -> set[str]:
    base_terms = {
        "acquisition",
        "merger",
        "funding",
        "capital",
        "expansion",
        "contract",
        "supply chain",
        "pricing",
        "regulatory",
        "partnership",
        "layoff",
    }
    if not user_id:
        return base_terms
    links = db.query(UserCompany).filter(UserCompany.user_id == user_id).all()
    for link in links:
        company = db.get(Company, link.company_id)
        if not company:
            continue
        base_terms.add(company.name.lower())
        if company.sector:
            base_terms.add(company.sector.lower())
        if company.subsector:
            base_terms.add(company.subsector.lower())
    return base_terms


def _broad_filter(items: list[dict[str, Any]], terms: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    evaluations: list[dict[str, Any]] = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('content', '')}".lower()
        hit_count = sum(1 for term in terms if term and term in text)
        evaluations.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "stage1_signal_hits": hit_count,
                "passed_step_1": hit_count > 0,
            }
        )
        if hit_count <= 0:
            continue
        scored.append((hit_count, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    limit = max(1, min(settings.stage1_candidate_limit, 500))
    selected_urls = {item.get("url", "") for _, item in scored[:limit]}
    for row in evaluations:
        row["selected_for_step_2"] = row.get("url", "") in selected_urls
    return [item for _, item in scored[:limit]], evaluations


def fetch_articles(db: Session, user_id: str | None = None, feeds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if feeds:
        items = feeds
        source = "custom"
        newsapi_status = "skipped_custom_feed"
        qmeta: dict[str, Any] = {"query_source": "custom"}
    else:
        newsapi_items, newsapi_status, qmeta = _fetch_newsapi_items(db, user_id)
        if newsapi_items:
            items = newsapi_items
            source = "newsapi"
        elif newsapi_status in ("no_articles_after_semantic", "no_newsapi_hits"):
            items = []
            source = "newsapi"
        else:
            items = DEFAULT_FEEDS
            source = "fallback_sample"

    generic_terms = _build_generic_terms(db, user_id)
    term_fallback_used = False
    context_driven = source == "newsapi" and qmeta.get("query_source") in (
        "context_keyword_batches",
        "context_two_phase",
    )

    if source in ("fallback_sample", "custom"):
        broad_candidates = items
        stage1_evaluations = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "stage1_signal_hits": 1,
                "passed_step_1": True,
                "selected_for_step_2": True,
            }
            for item in items
        ]
    elif context_driven:
        # Context-driven NewsAPI (single- or two-phase); process step assigns companies.
        limit = max(
            1,
            min(
                settings.newsapi_ingest_target_articles,
                settings.stage1_candidate_limit,
                len(items),
            ),
        )
        broad_candidates = items[:limit]
        if qmeta.get("query_source") == "context_two_phase":
            stage1_evaluations = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "stage1_signal_hits": 1,
                    "passed_step_1": True,
                    "selected_for_step_2": True,
                    "retrieval_tier": item.get("retrieval_tier"),
                    "retrieval_search": item.get("retrieval_search"),
                    "ingest_semantic_score": item.get("ingest_semantic_score"),
                    "ingest_semantic_note": item.get("ingest_semantic_note"),
                    "ingest_semantic_source": item.get("ingest_semantic_source"),
                }
                for item in broad_candidates
            ]
        else:
            stage1_evaluations = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "stage1_signal_hits": 1,
                    "passed_step_1": True,
                    "selected_for_step_2": True,
                }
                for item in broad_candidates
            ]
    else:
        broad_candidates, stage1_evaluations = _broad_filter(items, generic_terms)
        if not broad_candidates and items and source == "newsapi":
            limit = max(1, min(settings.stage1_candidate_limit, len(items)))
            broad_candidates = items[:limit]
            term_fallback_used = True
            stage1_evaluations = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "stage1_signal_hits": 0,
                    "passed_step_1": True,
                    "selected_for_step_2": True,
                }
                for item in broad_candidates
            ]

    inserted_article_ids: list[str] = []
    for item in broad_candidates:
        exists = db.query(Article).filter(Article.url == item["url"]).one_or_none()
        if exists:
            continue
        row = Article(**_article_payload_for_db(item))
        db.add(row)
        db.flush()
        inserted_article_ids.append(str(row.id))
    inserted = len(inserted_article_ids)
    db.commit()
    insert_note = None
    if inserted == 0 and broad_candidates:
        insert_note = "No new rows inserted: every candidate URL already exists in the database."

    hint = _newsapi_hint(newsapi_status)

    return {
        "step_1_broad": {
            "fetched": len(items),
            "generic_terms": len(generic_terms),
            "candidates_selected": len(broad_candidates),
            "days_back": max(1, settings.stage1_days_back),
            "evaluations": stage1_evaluations,
            "newsapi_term_fallback": term_fallback_used,
            "context_driven_ingest": context_driven,
            "newsapi_query_meta": qmeta,
            "ingest_methodology": qmeta.get("ingest_methodology"),
            "step_1_retrieval": qmeta.get("retrieval_trace"),
        },
        "inserted": inserted,
        "inserted_article_ids": inserted_article_ids,
        "source": source,
        "fetched": len(items),
        "newsapi_status": newsapi_status,
        "newsapi_hint": hint,
        "insert_note": insert_note,
    }


def extract_features(article: Article) -> dict[str, Any]:
    text = f"{article.title} {article.content}".lower()
    event_type = "general"
    if "funding" in text or "raised" in text:
        event_type = "funding"
    elif "acquire" in text or "m&a" in text:
        event_type = "m&a"
    elif "layoff" in text:
        event_type = "layoffs"

    entities = [token for token in ["acme", "logistics", "saas"] if token in text]
    sectors = [token for token in ["logistics", "fintech", "healthcare", "saas"] if token in text]
    return {
        "entities": entities,
        "sectors": sectors,
        "event_type": event_type,
        "sentiment": "neutral",
        "geography": "global",
    }


def persist_article_features(db: Session, article: Article) -> ArticleFeature:
    features = extract_features(article)
    row = db.query(ArticleFeature).filter(ArticleFeature.article_id == article.id).one_or_none()
    if row:
        row.entities = features["entities"]
        row.sectors = features["sectors"]
        row.event_type = features["event_type"]
        row.sentiment = features["sentiment"]
        row.geography = features["geography"]
    else:
        row = ArticleFeature(article_id=article.id, **features)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row

