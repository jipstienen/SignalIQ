import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from dateutil.parser import isoparse
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, ArticleFeature, Company, UserCompany

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


def _fetch_newsapi_items() -> tuple[list[dict[str, Any]], str]:
    if not settings.newsapi_key:
        logger.info("NEWSAPI_KEY not set; using default sample feed.")
        return [], "missing_key"

    max_fetch = max(1, min(settings.stage1_max_fetch, 1000))
    page_size = max(1, min(settings.newsapi_page_size, 100))
    params = {
        "q": settings.newsapi_query,
        "language": "en",
        "sortBy": "publishedAt",
        "from": (datetime.utcnow() - timedelta(days=max(1, settings.stage1_days_back))).date().isoformat(),
        "pageSize": page_size,
    }
    headers = {"X-Api-Key": settings.newsapi_key}

    normalized: list[dict[str, Any]] = []
    pages = max(1, (max_fetch + page_size - 1) // page_size)
    try:
        with httpx.Client(timeout=20.0) as client:
            for page in range(1, pages + 1):
                page_params = {**params, "page": page}
                res = client.get(settings.newsapi_url, params=page_params, headers=headers)
                res.raise_for_status()
                payload = res.json()
                articles = payload.get("articles", [])
                if not articles:
                    break
                for item in articles:
                    row = _normalize_newsapi_item(item)
                    if row:
                        normalized.append(row)
                        if len(normalized) >= max_fetch:
                            return normalized, "ok"
    except Exception as exc:
        logger.warning("NewsAPI fetch failed; using default sample feed: %s", exc)
        return [], "fetch_failed"
    return normalized, "ok"


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
    newsapi_items, newsapi_status = _fetch_newsapi_items()
    if feeds:
        items = feeds
        source = "custom"
    elif newsapi_items:
        items = newsapi_items
        source = "newsapi"
    else:
        items = DEFAULT_FEEDS
        source = "fallback_sample"

    # Step 1: broad funnel (generic terms over 7-day max 1000 fetch)
    generic_terms = _build_generic_terms(db, user_id)
    if source != "fallback_sample":
        broad_candidates, stage1_evaluations = _broad_filter(items, generic_terms)
    else:
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

    inserted = 0
    for item in broad_candidates:
        exists = db.query(Article).filter(Article.url == item["url"]).one_or_none()
        if exists:
            continue
        db.add(Article(**item))
        inserted += 1
    db.commit()
    return {
        "step_1_broad": {
            "fetched": len(items),
            "generic_terms": len(generic_terms),
            "candidates_selected": len(broad_candidates),
            "days_back": max(1, settings.stage1_days_back),
            "evaluations": stage1_evaluations,
        },
        "inserted": inserted,
        "source": source,
        "fetched": len(items),
        "newsapi_status": newsapi_status,
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

