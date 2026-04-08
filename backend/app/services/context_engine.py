import json
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Company, ContextProfile, UserCompany

DEFAULT_EVENT_WEIGHTS = {
    "funding": 0.7,
    "m&a": 1.0,
    "layoffs": 0.6,
    "regulatory": 0.8,
    "product_launch": 0.5,
}


def _fallback_context(company: Company) -> dict[str, Any]:
    return {
        "sector": company.sector or "Unknown",
        "subsector": company.subsector or "Unknown",
        "keywords": [company.name.lower()],
        "competitors": [],
        "event_weights": DEFAULT_EVENT_WEIGHTS,
    }


def _llm_extract_context(company: Company) -> dict[str, Any]:
    if not settings.openai_api_key:
        return _fallback_context(company)
    client = OpenAI(api_key=settings.openai_api_key)
    prompt = f"""
Return JSON only with:
sector, subsector, keywords (10-20), competitors (5-10), event_weights.
Company name: {company.name}
Description: {company.description or ""}
Known sector: {company.sector or ""}
Known subsector: {company.subsector or ""}
"""
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    content = response.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
        data.setdefault("event_weights", DEFAULT_EVENT_WEIGHTS)
        return data
    except json.JSONDecodeError:
        return _fallback_context(company)


def build_context(user_id: str, db: Session) -> int:
    links = db.query(UserCompany).filter(UserCompany.user_id == user_id).all()
    created = 0
    for link in links:
        company = db.get(Company, link.company_id)
        if not company:
            continue
        extracted = _llm_extract_context(company)
        existing = (
            db.query(ContextProfile)
            .filter(ContextProfile.user_id == user_id, ContextProfile.company_id == company.id)
            .one_or_none()
        )
        if existing:
            existing.sector = extracted.get("sector")
            existing.keywords = extracted.get("keywords", [])
            existing.competitors = extracted.get("competitors", [])
            existing.event_weights = extracted.get("event_weights", DEFAULT_EVENT_WEIGHTS)
        else:
            profile = ContextProfile(
                user_id=user_id,
                company_id=company.id,
                sector=extracted.get("sector"),
                keywords=extracted.get("keywords", []),
                competitors=extracted.get("competitors", []),
                event_weights=extracted.get("event_weights", DEFAULT_EVENT_WEIGHTS),
                priority_weight=1.0,
            )
            db.add(profile)
            created += 1
    db.commit()
    return created

