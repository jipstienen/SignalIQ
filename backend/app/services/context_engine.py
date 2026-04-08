import json
from typing import Any

import httpx
from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Company, ContextProfile, UserCompany

DEFAULT_EVENT_WEIGHTS = {
    "m&a": 1.0,
    "funding": 0.7,
    "layoffs": 0.6,
    "regulatory": 0.8,
    "supply_chain": 0.9,
    "pricing": 0.7,
    "expansion": 0.8,
    "partnerships": 0.6,
}


def _fallback_context(company: Company) -> dict[str, Any]:
    keywords = []
    if company.sector:
        keywords.append(company.sector.lower())
    if company.subsector:
        keywords.append(company.subsector.lower())
    if company.description:
        keywords.extend([token.strip().lower() for token in company.description.split()[:15] if token.strip()])
    return {
        "sector": company.sector or "Unknown",
        "subsector": company.subsector or "Unknown",
        "keywords": list(dict.fromkeys(keywords))[:20] or ["operations", "demand", "contracts"],
        "competitors": [],
        "event_weights": dict(DEFAULT_EVENT_WEIGHTS),
        "business_signals": ["new contracts", "capacity changes", "pricing changes"],
        "geography": [],
    }


def _strip_json_fence(text: str) -> str:
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    return content.strip()


def _normalize_context_payload(company: Company, data: dict[str, Any]) -> dict[str, Any]:
    fallback = _fallback_context(company)
    merged = dict(fallback)
    merged.update({k: v for k, v in data.items() if v is not None})

    merged["sector"] = str(merged.get("sector") or fallback["sector"])[:120]
    merged["subsector"] = str(merged.get("subsector") or fallback["subsector"])[:120]

    keywords = [str(x).strip().lower() for x in (merged.get("keywords") or []) if str(x).strip()]
    keywords = [k for k in keywords if k not in {"company", "business", company.name.lower()}]
    merged["keywords"] = list(dict.fromkeys(keywords))[:25] or fallback["keywords"]

    competitors = [str(x).strip() for x in (merged.get("competitors") or []) if str(x).strip()]
    merged["competitors"] = list(dict.fromkeys(competitors))[:10]

    signals = [str(x).strip().lower() for x in (merged.get("business_signals") or []) if str(x).strip()]
    merged["business_signals"] = list(dict.fromkeys(signals))[:10]

    geography = [str(x).strip() for x in (merged.get("geography") or []) if str(x).strip()]
    merged["geography"] = list(dict.fromkeys(geography))[:10]

    weights = dict(DEFAULT_EVENT_WEIGHTS)
    raw_weights = merged.get("event_weights") or {}
    for key in DEFAULT_EVENT_WEIGHTS:
        try:
            value = float(raw_weights.get(key, DEFAULT_EVENT_WEIGHTS[key]))
            weights[key] = max(0.0, min(1.0, value))
        except (TypeError, ValueError):
            weights[key] = DEFAULT_EVENT_WEIGHTS[key]
    merged["event_weights"] = weights
    return merged


def _llm_extract_context(company: Company) -> dict[str, Any]:
    prompt = f"""
You are a private equity analyst building an intelligence profile.
Return STRICT JSON only in this exact shape:
{{
  "sector": "",
  "subsector": "",
  "keywords": [],
  "competitors": [],
  "event_weights": {{
    "m&a": 1.0,
    "funding": 0.7,
    "layoffs": 0.6,
    "regulatory": 0.8,
    "supply_chain": 0.9,
    "pricing": 0.7,
    "expansion": 0.8,
    "partnerships": 0.6
  }},
  "business_signals": [],
  "geography": []
}}

Rules:
- Keywords: 15-25 high-signal terms/phrases for real-world news matching.
- Competitors: 5-10 realistic direct competitors or close comparable companies.
- Avoid generic terms and avoid repeating the company name in keywords.
- Event weights must be between 0 and 1 and business-model aware.
- Business signals: 5-10 specific indicators.

Company name: {company.name}
Industry: {company.sector or ""}
Description: {company.description or ""}
Known subsector: {company.subsector or ""}
"""
    provider = (settings.context_provider or "fallback").strip().lower()

    content = "{}"
    if provider == "openai":
        if not settings.openai_api_key:
            return _fallback_context(company)
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model=settings.context_model or "gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = response.choices[0].message.content or "{}"
    elif provider == "ollama":
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                    json={
                        "model": settings.ollama_model,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                    },
                )
                response.raise_for_status()
                payload = response.json()
                content = payload.get("response") or "{}"
        except Exception:
            return _fallback_context(company)
    else:
        return _fallback_context(company)

    try:
        data = json.loads(_strip_json_fence(content))
        return _normalize_context_payload(company, data)
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
            existing.event_weights = {
                **extracted.get("event_weights", DEFAULT_EVENT_WEIGHTS),
                "_business_signals": extracted.get("business_signals", []),
                "_geography": extracted.get("geography", []),
                "_subsector": extracted.get("subsector", ""),
            }
        else:
            profile = ContextProfile(
                user_id=user_id,
                company_id=company.id,
                sector=extracted.get("sector"),
                keywords=extracted.get("keywords", []),
                competitors=extracted.get("competitors", []),
                event_weights={
                    **extracted.get("event_weights", DEFAULT_EVENT_WEIGHTS),
                    "_business_signals": extracted.get("business_signals", []),
                    "_geography": extracted.get("geography", []),
                    "_subsector": extracted.get("subsector", ""),
                },
                priority_weight=1.0,
            )
            db.add(profile)
            created += 1
        if extracted.get("subsector"):
            company.subsector = extracted["subsector"][:120]
    db.commit()
    return created

