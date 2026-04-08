import json

from openai import OpenAI

from ..config import settings
from ..models import Article


def generate_insight(article: Article, matched_company: str, event_type: str) -> dict[str, str]:
    if not settings.openai_api_key:
        return {
            "summary": f"{article.title}. Event type: {event_type}.",
            "why_it_matters": f"This may affect {matched_company} through competitive and sector pressure.",
        }

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = f"""
Return JSON with keys: summary, why_it_matters.
Use 2-3 lines for summary.
Article:
Title: {article.title}
Content: {article.content}
Matched company: {matched_company}
Event type: {event_type}
"""
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
        return {
            "summary": parsed.get("summary", article.title),
            "why_it_matters": parsed.get("why_it_matters", "Potential portfolio impact detected."),
        }
    except json.JSONDecodeError:
        return {
            "summary": article.title,
            "why_it_matters": "Potential portfolio impact detected.",
        }

