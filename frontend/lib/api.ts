export type Insight = {
  id: string;
  article_id: string;
  summary: string;
  why_it_matters: string;
  base_score: number;
  final_score: number;
  created_at: string;
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

