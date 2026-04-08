export type Insight = {
  id: string;
  article_id: string;
  summary: string;
  why_it_matters: string;
  base_score: number;
  final_score: number;
  created_at: string;
};

export type ScoreComponents = {
  semantic_relevance: number;
  semantic_category: string;
  semantic_reason: string;
  entity_match: number;
  /** Legacy field; context-based scoring no longer uses global event weights. */
  event_importance: number;
  driver_risk_triggered?: boolean;
  driver_risk_matches?: string;
};

export type ReasoningTrace = {
  user: {
    id: string;
    email: string;
    mode: "high_signal" | "balanced" | "exploratory";
    threshold: number;
    context_provider?: string;
    context_model?: string;
    /** Present when API is context-based scoring (drivers/risks/keywords), not legacy event-weight blend */
    scoring_framework?: string;
  };
  trace_meta?: {
    article_limit: number;
    assessments_in_view: number;
  };
  companies: Array<{
    id: string;
    name: string;
    type: string;
    sector: string | null;
    aliases: string[];
    description?: string | null;
  }>;
  contexts: Array<{
    company_id: string;
    sector: string | null;
    subsector?: string;
    business_model?: string;
    keywords: string[];
    competitors: string[];
    key_drivers?: string[];
    risk_factors?: string[];
    semantic_signals?: string[];
    event_weights: Record<string, number>;
    business_signals?: string[];
    geography?: string[];
    priority_weight: number;
  }>;
  preferences: {
    event_weights: Record<string, number>;
    sector_weights: Record<string, number>;
    company_weights: Record<string, number>;
    sensitivity: number;
  };
  scored_articles: Array<{
    article_id: string;
    title: string;
    source: string;
    url: string;
    published_at: string;
    features: {
      entities: string[];
      sectors: string[];
      event_type: string | null;
      sentiment: string | null;
      geography: string | null;
    };
    matched_company_id: string | null;
    matched_company_name: string | null;
    relevance_type: string | null;
    conclusion: string | null;
    passed_step_2: boolean | null;
    displayed: boolean | null;
    score:
      | {
          base_score: number;
          final_score: number;
          passes_threshold: boolean;
          components: ScoreComponents | null;
          source: "insight" | "assessment";
        }
      | null;
    insight_created: boolean;
    insight_id: string | null;
  }>;
};

export type ReasoningCompanyInput = {
  name: string;
  industry: string;
  description: string;
};

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const USER_TOKEN = process.env.NEXT_PUBLIC_USER_TOKEN || "";

function headers() {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${USER_TOKEN}`,
  };
}

export async function getInsights(): Promise<Insight[]> {
  const res = await fetch(`${API_URL}/insights`, { headers: headers(), cache: "no-store" });
  if (!res.ok) return [];
  return res.json();
}

export async function getHistory(): Promise<Insight[]> {
  const res = await fetch(`${API_URL}/history`, { headers: headers(), cache: "no-store" });
  if (!res.ok) return [];
  return res.json();
}

export async function sendFeedback(insightId: string, feedbackType: "like" | "dislike" | "click") {
  await fetch(`${API_URL}/feedback`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ insight_id: insightId, feedback_type: feedbackType }),
  });
}

export async function updateMode(mode: "high_signal" | "balanced" | "exploratory") {
  await fetch(`${API_URL}/settings`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({ mode }),
  });
}

export async function getReasoningTrace(limit = 25): Promise<ReasoningTrace | null> {
  const res = await fetch(`${API_URL}/reasoning?limit=${limit}`, { headers: headers(), cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

export async function runIngest(): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_URL}/pipeline/ingest`, { method: "POST", headers: headers() });
  if (!res.ok) throw new Error(`Ingest failed: ${res.status}`);
  return res.json();
}

export async function runContextBuild(): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_URL}/context/build`, { method: "POST", headers: headers() });
  if (!res.ok) throw new Error(`Context build failed: ${res.status}`);
  return res.json();
}

export async function runProcess(): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_URL}/pipeline/process`, { method: "POST", headers: headers() });
  if (!res.ok) throw new Error(`Process failed: ${res.status}`);
  return res.json();
}

export async function runReasoningGenerate(
  companies: ReasoningCompanyInput[],
  strictness: "very_narrow" | "average" | "wide",
  limit = 25
): Promise<{ trace: ReasoningTrace } & Record<string, unknown>> {
  const res = await fetch(`${API_URL}/reasoning/generate`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ companies, strictness, limit }),
  });
  if (!res.ok) throw new Error(`Generate failed: ${res.status}`);
  return res.json();
}

