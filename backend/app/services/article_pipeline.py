from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

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


def fetch_articles(db: Session, feeds: list[dict[str, Any]] | None = None) -> int:
    items = feeds or DEFAULT_FEEDS
    inserted = 0
    for item in items:
        exists = db.query(Article).filter(Article.url == item["url"]).one_or_none()
        if exists:
            continue
        db.add(Article(**item))
        inserted += 1
    db.commit()
    return inserted


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

