export type Insight = {
  id: string;
  article_id: string;
  summary: string;
  why_it_matters: string;
  base_score: number;
  final_score: number;
  created_at: string;
};

export type ReasoningTrace = {
  user: { id: string; email: string; mode: "high_signal" | "balanced" | "exploratory"; threshold: number };
  companies: Array<{ id: string; name: string; type: string; sector: string | null; aliases: string[] }>;
  contexts: Array<{
    company_id: string;
    sector: string | null;
    keywords: string[];
    competitors: string[];
    event_weights: Record<string, number>;
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
    score: {
      base_score: number;
      final_score: number;
      components: {
        entity_match: number;
        event_importance: number;
        context_relevance: number;
        proximity: number;
        novelty: number;
      };
      passes_threshold: boolean;
    };
    insight_created: boolean;
    insight_id: string | null;
  }>;
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

