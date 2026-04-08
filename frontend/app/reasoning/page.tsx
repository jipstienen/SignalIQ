"use client";

import { useEffect, useState } from "react";
import { ReasoningTrace, getReasoningTrace } from "../../lib/api";

export default function ReasoningPage() {
  const [trace, setTrace] = useState<ReasoningTrace | null>(null);
  const [limit, setLimit] = useState(25);

  useEffect(() => {
    getReasoningTrace(limit).then(setTrace);
  }, [limit]);

  return (
    <main>
      <h1>Pipeline Reasoning Trace</h1>
      <p>Inspect how inputs become insights through each deterministic stage.</p>
      <div style={{ marginBottom: 16 }}>
        <label>
          Articles to score:{" "}
          <input
            type="number"
            value={limit}
            min={1}
            max={100}
            onChange={(e) => setLimit(Math.max(1, Math.min(100, Number(e.target.value) || 25)))}
          />
        </label>
      </div>

      {!trace && <p>Unable to load reasoning trace.</p>}
      {trace && (
        <>
          <section style={{ marginBottom: 20 }}>
            <h2>1) User Mode and Threshold</h2>
            <p>
              <strong>User:</strong> {trace.user.email} ({trace.user.id})
            </p>
            <p>
              <strong>Mode:</strong> {trace.user.mode} | <strong>Threshold:</strong> {trace.user.threshold}
            </p>
          </section>

          <section style={{ marginBottom: 20 }}>
            <h2>2) Linked Companies (Input Universe)</h2>
            {trace.companies.length === 0 && <p>No linked companies yet.</p>}
            {trace.companies.map((company) => (
              <article key={company.id} style={{ border: "1px solid #ddd", borderRadius: 8, padding: 10, marginBottom: 8 }}>
                <p>
                  <strong>{company.name}</strong> ({company.type})
                </p>
                <p>Sector: {company.sector || "n/a"}</p>
                <p>Aliases: {company.aliases.join(", ") || "n/a"}</p>
              </article>
            ))}
          </section>

          <section style={{ marginBottom: 20 }}>
            <h2>3) Context Profiles (Generated Context)</h2>
            {trace.contexts.length === 0 && <p>No context profiles yet. Run context build first.</p>}
            {trace.contexts.map((ctx) => (
              <article key={ctx.company_id} style={{ border: "1px solid #ddd", borderRadius: 8, padding: 10, marginBottom: 8 }}>
                <p>
                  <strong>Company ID:</strong> {ctx.company_id}
                </p>
                <p>
                  <strong>Sector:</strong> {ctx.sector || "n/a"}
                </p>
                <p>
                  <strong>Keywords:</strong> {ctx.keywords.join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Competitors:</strong> {ctx.competitors.join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Event Weights:</strong> {JSON.stringify(ctx.event_weights)}
                </p>
              </article>
            ))}
          </section>

          <section style={{ marginBottom: 20 }}>
            <h2>4) Scoring Table (Article-by-Article)</h2>
            {trace.scored_articles.length === 0 && <p>No ingested articles available.</p>}
            {trace.scored_articles.map((row) => (
              <article key={row.article_id} style={{ border: "1px solid #ddd", borderRadius: 8, padding: 10, marginBottom: 10 }}>
                <p>
                  <strong>{row.title}</strong>
                </p>
                <p>
                  <strong>Source:</strong> {row.source} | <strong>Published:</strong> {new Date(row.published_at).toLocaleString()}
                </p>
                <p>
                  <strong>URL:</strong> <a href={row.url} target="_blank" rel="noreferrer">{row.url}</a>
                </p>
                <p>
                  <strong>Features:</strong> entities={row.features.entities.join(", ") || "n/a"}; sectors={row.features.sectors.join(", ") || "n/a"}; event=
                  {row.features.event_type || "general"}
                </p>
                <p>
                  <strong>Score:</strong> final {row.score.final_score.toFixed(3)} (base {row.score.base_score.toFixed(3)})
                </p>
                <p>
                  Components: entity {row.score.components.entity_match.toFixed(2)}, event {row.score.components.event_importance.toFixed(2)}, context{" "}
                  {row.score.components.context_relevance.toFixed(2)}, proximity {row.score.components.proximity.toFixed(2)}, novelty{" "}
                  {row.score.components.novelty.toFixed(2)}
                </p>
                <p>
                  <strong>Threshold pass:</strong> {row.score.passes_threshold ? "yes" : "no"} | <strong>Insight created:</strong>{" "}
                  {row.insight_created ? "yes" : "no"}
                </p>
              </article>
            ))}
          </section>
        </>
      )}
    </main>
  );
}

