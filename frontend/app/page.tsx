"use client";

import { useEffect, useState } from "react";
import { Insight, getInsights, sendFeedback } from "../lib/api";

export default function DashboardPage() {
  const [insights, setInsights] = useState<Insight[]>([]);

  useEffect(() => {
    getInsights().then(setInsights);
  }, []);

  const handleFeedback = async (insightId: string, type: "like" | "dislike") => {
    await sendFeedback(insightId, type);
  };

  return (
    <main>
      <h1>Portfolio Intelligence Dashboard</h1>
      {insights.length === 0 && <p>No insights yet.</p>}
      {insights.map((insight) => (
        <section
          key={insight.id}
          style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12, marginBottom: 12 }}
        >
          <p><strong>Summary:</strong> {insight.summary}</p>
          <p><strong>Why it matters:</strong> {insight.why_it_matters}</p>
          <p><strong>Relevance score:</strong> {insight.final_score.toFixed(3)}</p>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => handleFeedback(insight.id, "like")}>👍</button>
            <button onClick={() => handleFeedback(insight.id, "dislike")}>👎</button>
            <button onClick={() => alert("Use /query endpoint for ask more flow.")}>ask more</button>
          </div>
        </section>
      ))}
    </main>
  );
}

