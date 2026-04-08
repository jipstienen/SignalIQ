from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Insight, User, UserMode

DAILY_LIMITS = {
    UserMode.high_signal: 5,
    UserMode.balanced: 10,
    UserMode.exploratory: 20,
}


def generate_daily_report(user: User, db: Session) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=1)
    insights = (
        db.query(Insight)
        .filter(Insight.user_id == user.id, Insight.created_at >= cutoff)
        .order_by(Insight.final_score.desc())
        .limit(200)
        .all()
    )
    limit = DAILY_LIMITS[user.mode]
    if not insights:
        return {"user_id": str(user.id), "mode": user.mode.value, "items": []}

    # Keep deterministic high-signal core, then inject 10-20% exploration.
    exploration_ratio = 0.2 if user.mode == UserMode.exploratory else 0.1
    explore_count = min(max(1, int(limit * exploration_ratio)), limit - 1) if limit > 1 else 0
    core_count = max(1, limit - explore_count)

    ranked = insights[:limit * 3]
    core = ranked[:core_count]
    exploratory_pool = ranked[core_count:]
    exploratory = exploratory_pool[:explore_count]
    selected = (core + exploratory)[:limit]

    payload = [
        {
            "summary": i.summary,
            "why_it_matters": i.why_it_matters,
            "score": i.final_score,
        }
        for i in selected
    ]
    return {"user_id": str(user.id), "mode": user.mode.value, "items": payload}


def send_email_report(_email: str, _report: dict) -> None:
    # Hook for Resend/SendGrid integration.
    return None


def send_slack_report(_channel: str, _report: dict) -> None:
    # Hook for Slack integration.
    return None

