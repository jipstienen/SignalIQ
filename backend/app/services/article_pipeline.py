import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from dateutil.parser import isoparse
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Article, ArticleFeature

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

    params = {
        "q": settings.newsapi_query,
        "language": "en",
        "sortBy": "publishedAt",
        "from": (datetime.utcnow() - timedelta(days=2)).date().isoformat(),
        "pageSize": max(1, min(settings.newsapi_page_size, 100)),
    }
    headers = {"X-Api-Key": settings.newsapi_key}

    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(settings.newsapi_url, params=params, headers=headers)
            res.raise_for_status()
            payload = res.json()
    except Exception as exc:
        logger.warning("NewsAPI fetch failed; using default sample feed: %s", exc)
        return [], "fetch_failed"

    articles = payload.get("articles", [])
    normalized: list[dict[str, Any]] = []
    for item in articles:
        row = _normalize_newsapi_item(item)
        if row:
            normalized.append(row)
    return normalized, "ok"


def fetch_articles(db: Session, feeds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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

    inserted = 0
    for item in items:
        exists = db.query(Article).filter(Article.url == item["url"]).one_or_none()
        if exists:
            continue
        db.add(Article(**item))
        inserted += 1
    db.commit()
    return {
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

