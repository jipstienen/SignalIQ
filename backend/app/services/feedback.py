from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import ArticleFeature, FeedbackType, Insight, UserFeedback, UserPreference


def _adjust(base: float, likes: int, dislikes: int, click: int) -> float:
    return max(0.5, min(2.0, base + likes * 0.1 - dislikes * 0.12 + click * 0.03))


def update_user_preferences(user_id: str, db: Session) -> UserPreference:
    cutoff = datetime.utcnow() - timedelta(days=14)
    feedback_rows = (
        db.query(UserFeedback, Insight, ArticleFeature)
        .join(Insight, UserFeedback.insight_id == Insight.id)
        .join(ArticleFeature, Insight.article_id == ArticleFeature.article_id)
        .filter(UserFeedback.user_id == user_id, UserFeedback.created_at >= cutoff)
        .all()
    )

    event_counts = defaultdict(Counter)
    sector_counts = defaultdict(Counter)
    company_counts = defaultdict(Counter)

    for feedback, insight, feature in feedback_rows:
        label = feedback.feedback_type.value
        event_counts[feature.event_type or "general"][label] += 1
        for sector in feature.sectors:
            sector_counts[sector.lower()][label] += 1
        company_counts[str(insight.user_id)][label] += 1

    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).one_or_none()
    if not pref:
        pref = UserPreference(
            user_id=user_id,
            event_weights={},
            sector_weights={},
            company_weights={},
            sensitivity=1.0,
        )
        db.add(pref)

    for key, counts in event_counts.items():
        pref.event_weights[key] = _adjust(1.0, counts["like"], counts["dislike"], counts["click"])
    for key, counts in sector_counts.items():
        pref.sector_weights[key] = _adjust(1.0, counts["like"], counts["dislike"], counts["click"])
    for key, counts in company_counts.items():
        pref.company_weights[key] = _adjust(1.0, counts["like"], counts["dislike"], counts["click"])

    likes = sum(c["like"] for c in event_counts.values())
    dislikes = sum(c["dislike"] for c in event_counts.values())
    pref.sensitivity = max(0.7, min(1.4, 1.0 + likes * 0.02 - dislikes * 0.02))

    db.commit()
    db.refresh(pref)
    return pref


def message_to_feedback_type(message: str) -> FeedbackType:
    msg = message.lower()
    if any(k in msg for k in ["interesting", "good", "like", "more like this"]):
        return FeedbackType.like
    if any(k in msg for k in ["skip", "dislike", "less of this", "not useful"]):
        return FeedbackType.dislike
    return FeedbackType.click


def apply_message_directive(pref: UserPreference, message: str, feature: ArticleFeature, company_key: str) -> None:
    msg = message.lower()
    if "more like this" in msg:
        event = feature.event_type or "general"
        pref.event_weights[event] = min(2.0, float(pref.event_weights.get(event, 1.0)) + 0.15)
        for sector in feature.sectors:
            key = sector.lower()
            pref.sector_weights[key] = min(2.0, float(pref.sector_weights.get(key, 1.0)) + 0.10)
        pref.company_weights[company_key] = min(2.0, float(pref.company_weights.get(company_key, 1.0)) + 0.10)
    elif "less of this" in msg:
        event = feature.event_type or "general"
        pref.event_weights[event] = max(0.5, float(pref.event_weights.get(event, 1.0)) - 0.15)
        for sector in feature.sectors:
            key = sector.lower()
            pref.sector_weights[key] = max(0.5, float(pref.sector_weights.get(key, 1.0)) - 0.10)
        pref.company_weights[company_key] = max(0.5, float(pref.company_weights.get(company_key, 1.0)) - 0.10)

