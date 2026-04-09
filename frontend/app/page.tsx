"use client";

import { useEffect, useState } from "react";
import { Article, Insight, getArticles, getInsights, sendFeedback } from "../lib/api";

export default function DashboardPage() {
  const [insights, setInsights] = useState<Insight[]>([]);
  const [articles, setArticles] = useState<Article[]>([]);

  const load = () => {
    getInsights().then(setInsights);
    getArticles(100).then(setArticles);
  };

  useEffect(() => {
    load();
  }, []);

  const handleFeedback = async (insightId: string, type: "like" | "dislike") => {
    await sendFeedback(insightId, type);
  };

  return (
    <main>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ margin: 0 }}>Portfolio Intelligence Dashboard</h1>
        <button type="button" onClick={() => load()} style={{ padding: "6px 12px" }}>
          Refresh
        </button>
      </div>

      <section style={{ marginBottom: 28 }}>
        <h2 style={{ marginBottom: 8 }}>Insights</h2>
        <p style={{ color: "#444", maxWidth: "42rem", marginBottom: 12 }}>
          Short AI summaries for articles that cleared your relevance threshold after processing. This list is
          not the same as raw news volume — many ingested articles never become insights.
        </p>
        {insights.length === 0 && <p>No insights yet.</p>}
        {insights.map((insight) => (
          <section
            key={insight.id}
            style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12, marginBottom: 12 }}
          >
            <p>
              <strong>Summary:</strong> {insight.summary}
            </p>
            <p>
              <strong>Why it matters:</strong> {insight.why_it_matters}
            </p>
            <p>
              <strong>Relevance score:</strong> {insight.final_score.toFixed(3)}
            </p>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => handleFeedback(insight.id, "like")}>👍</button>
              <button onClick={() => handleFeedback(insight.id, "dislike")}>👎</button>
              <button onClick={() => alert("Use /query endpoint for ask more flow.")}>ask more</button>
            </div>
          </section>
        ))}
      </section>

      <section>
        <h2 style={{ marginBottom: 8 }}>Ingested articles</h2>
        <p style={{ color: "#444", maxWidth: "42rem", marginBottom: 12 }}>
          News rows stored from ingest (NewsAPI or sample). Use <strong>Reasoning</strong> for scoring detail.
        </p>
        {articles.length === 0 && <p>No articles in the database yet — run ingest from the Reasoning page.</p>}
        {articles.length > 0 && (
          <p style={{ marginBottom: 8 }}>
            <strong>{articles.length}</strong> article{articles.length === 1 ? "" : "s"} (showing up to 100 most
            recent).
          </p>
        )}
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {articles.map((a) => (
            <li
              key={a.id}
              style={{
                border: "1px solid #e8e8e8",
                borderRadius: 6,
                padding: 10,
                marginBottom: 8,
              }}
            >
              <a href={a.url} target="_blank" rel="noreferrer" style={{ fontWeight: 600 }}>
                {a.title}
              </a>
              <div style={{ fontSize: 13, color: "#555", marginTop: 4 }}>
                {a.source} · {new Date(a.published_at).toLocaleString()}
              </div>
              <p style={{ fontSize: 14, margin: "6px 0 0", lineHeight: 1.4 }}>
                {a.content.length > 280 ? `${a.content.slice(0, 280)}…` : a.content}
              </p>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
