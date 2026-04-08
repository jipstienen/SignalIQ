"use client";

import { useEffect, useState } from "react";
import {
  ReasoningCompanyInput,
  ReasoningTrace,
  getReasoningTrace,
  runContextBuild,
  runIngest,
  runProcess,
  runReasoningGenerate,
} from "../../lib/api";

type Strictness = "very_narrow" | "average" | "wide";

export default function ReasoningPage() {
  const [trace, setTrace] = useState<ReasoningTrace | null>(null);
  const [limit, setLimit] = useState(25);
  const [strictness, setStrictness] = useState<Strictness>("average");
  const [tableInput, setTableInput] = useState(
    "company name,industry,description\nACME Logistics,logistics,Freight software platform\nNorthstar Health,healthcare,Operator of outpatient clinics"
  );
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(null);

  const refreshTrace = async () => {
    const data = await getReasoningTrace(limit);
    setTrace(data);
  };

  useEffect(() => {
    refreshTrace();
  }, [limit]);

  const parseTableInput = (): ReasoningCompanyInput[] => {
    const lines = tableInput
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    if (lines.length === 0) return [];

    const startAt = lines[0].toLowerCase().includes("company") ? 1 : 0;
    const rows: ReasoningCompanyInput[] = [];
    for (const line of lines.slice(startAt)) {
      const cells = line.split(",").map((c) => c.trim());
      rows.push({
        name: cells[0] || "",
        industry: cells[1] || "",
        description: cells.slice(2).join(", ") || "",
      });
    }
    return rows.filter((r) => r.name.length > 0);
  };

  const runAction = async (name: string, fn: () => Promise<Record<string, unknown>>) => {
    try {
      setBusy(true);
      setStatus(`${name} running...`);
      const result = await fn();
      setLastResult(result);
      setStatus(`${name} completed`);
      await refreshTrace();
    } catch (err) {
      setStatus(`${name} failed: ${err instanceof Error ? err.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  const runGenerate = async () => {
    const companies = parseTableInput();
    if (companies.length === 0) {
      setStatus("Generate failed: please provide at least one company row.");
      return;
    }
    try {
      setBusy(true);
      setStatus("Generate running: context -> ingest -> process...");
      const result = await runReasoningGenerate(companies, strictness, limit);
      setLastResult(result);
      setTrace(result.trace);
      setStatus("Generate completed");
    } catch (err) {
      setStatus(`Generate failed: ${err instanceof Error ? err.message : "unknown error"}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main>
      <h1>Pipeline Reasoning Trace</h1>
      <p>Paste company rows, choose strictness, then generate a full trace from input to scored output.</p>
      <section style={{ marginBottom: 20, border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>
        <h2>Input Table and Controls</h2>
        <p>Use CSV format: company name, industry, description</p>
        <textarea
          value={tableInput}
          onChange={(e) => setTableInput(e.target.value)}
          rows={6}
          style={{ width: "100%", fontFamily: "monospace", marginBottom: 8 }}
        />
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
          <label>
            Strictness:{" "}
            <select value={strictness} onChange={(e) => setStrictness(e.target.value as Strictness)}>
              <option value="very_narrow">very narrow</option>
              <option value="average">average</option>
              <option value="wide">wide</option>
            </select>
          </label>
          <button disabled={busy} onClick={runGenerate}>Generate Full Trace</button>
        </div>
      </section>
      <section style={{ marginBottom: 20, border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>
        <h2>Manual Pipeline Controls</h2>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
          <button disabled={busy} onClick={() => runAction("Ingest", runIngest)}>Run Ingest</button>
          <button disabled={busy} onClick={() => runAction("Context Build", runContextBuild)}>Build Context</button>
          <button disabled={busy} onClick={() => runAction("Process", runProcess)}>Run Process</button>
          <button disabled={busy} onClick={refreshTrace}>Refresh Trace</button>
        </div>
        <p><strong>Status:</strong> {status || "idle"}</p>
        {lastResult && <pre style={{ whiteSpace: "pre-wrap", background: "#f7f7f7", padding: 8 }}>{JSON.stringify(lastResult, null, 2)}</pre>}
      </section>
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
            <p>
              <strong>Context Provider:</strong> {trace.user.context_provider || "fallback"} | <strong>Model:</strong>{" "}
              {trace.user.context_model || "n/a"}
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
                  <strong>Subsector:</strong> {ctx.subsector || "n/a"}
                </p>
                <p>
                  <strong>Business Model:</strong> {ctx.business_model || "n/a"}
                </p>
                <p>
                  <strong>Keywords:</strong> {ctx.keywords.join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Competitors:</strong> {ctx.competitors.join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Key Drivers:</strong> {(ctx.key_drivers || []).join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Risk Factors:</strong> {(ctx.risk_factors || []).join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Semantic Signals:</strong> {(ctx.semantic_signals || []).join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Business Signals:</strong> {(ctx.business_signals || []).join(", ") || "n/a"}
                </p>
                <p>
                  <strong>Geography:</strong> {(ctx.geography || []).join(", ") || "n/a"}
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
                  Components: semantic {row.score.components.semantic_relevance.toFixed(2)} ({row.score.components.semantic_category}), entity{" "}
                  {row.score.components.entity_match.toFixed(2)}, event {row.score.components.event_importance.toFixed(2)}
                </p>
                <p><strong>Semantic Reason:</strong> {row.score.components.semantic_reason || "n/a"}</p>
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

