"use client";

import { useCallback, useEffect, useState } from "react";
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
  const [progress, setProgress] = useState(0);
  const [traceError, setTraceError] = useState<string | null>(null);
  const [traceLoading, setTraceLoading] = useState(true);

  const refreshTrace = useCallback(async () => {
    setTraceLoading(true);
    try {
      const { trace: data, error } = await getReasoningTrace(limit);
      setTrace(data);
      setTraceError(error);
    } finally {
      setTraceLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    refreshTrace();
  }, [refreshTrace]);

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
      setProgress(10);
      setStatus(`${name} running...`);
      setProgress(45);
      const result = await fn();
      setProgress(85);
      setLastResult(result);
      setStatus(`${name} completed`);
      await refreshTrace();
      setProgress(100);
    } catch (err) {
      setStatus(`${name} failed: ${err instanceof Error ? err.message : "unknown error"}`);
      setProgress(0);
    } finally {
      setBusy(false);
      window.setTimeout(() => setProgress(0), 800);
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
      setProgress(5);
      setStatus("Generate running: context -> ingest -> process...");
      setProgress(20);
      const result = await runReasoningGenerate(companies, strictness, limit);
      setProgress(90);
      setLastResult(result);
      setTrace(result.trace);
      setTraceError(null);
      setStatus("Generate completed");
      setProgress(100);
    } catch (err) {
      setStatus(`Generate failed: ${err instanceof Error ? err.message : "unknown error"}`);
      setProgress(0);
    } finally {
      setBusy(false);
      window.setTimeout(() => setProgress(0), 1000);
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
        <div style={{ marginTop: 8 }}>
          <div style={{ height: 10, background: "#eee", borderRadius: 6, overflow: "hidden" }}>
            <div
              style={{
                width: `${progress}%`,
                height: "100%",
                background: "#2563eb",
                transition: "width 250ms ease",
              }}
            />
          </div>
          <p style={{ marginTop: 6 }}><strong>Progress:</strong> {progress}%</p>
        </div>
        {lastResult && <pre style={{ whiteSpace: "pre-wrap", background: "#f7f7f7", padding: 8 }}>{JSON.stringify(lastResult, null, 2)}</pre>}
      </section>

      {lastResult && (lastResult as { run_summary?: Record<string, unknown> }).run_summary && (
        <section style={{ marginBottom: 20, border: "1px solid #ddd", borderRadius: 8, padding: 12, background: "#fafafa" }}>
          <h2>Run summary (this generate)</h2>
          <p style={{ fontSize: 14, color: "#444" }}>
            {(lastResult as { run_summary?: { note?: string } }).run_summary?.note}
          </p>
          <ul style={{ fontSize: 14 }}>
            <li>
              <strong>Ingest source:</strong> {String((lastResult as { run_summary?: { ingest_source?: string } }).run_summary?.ingest_source ?? "—")} |{" "}
              <strong>NewsAPI:</strong> {String((lastResult as { run_summary?: { newsapi_status?: string } }).run_summary?.newsapi_status ?? "—")}
            </li>
            <li>
              <strong>Step 1 fetched:</strong> {String((lastResult as { run_summary?: { step_1_fetched?: number } }).run_summary?.step_1_fetched ?? "—")} |{" "}
              <strong>Inserted this run:</strong> {String((lastResult as { run_summary?: { ingest_inserted?: number } }).run_summary?.ingest_inserted ?? "—")}
            </li>
            <li>
              <strong>Step 2 evaluated:</strong> {String((lastResult as { run_summary?: { process_evaluated_count?: number } }).run_summary?.process_evaluated_count ?? "—")} |{" "}
              <strong>Insights created:</strong> {String((lastResult as { run_summary?: { insights_created?: number } }).run_summary?.insights_created ?? "—")} |{" "}
              <strong>Threshold:</strong> {String((lastResult as { run_summary?: { threshold?: number } }).run_summary?.threshold ?? "—")}
            </li>
            <li>
              <strong>Trace list limit:</strong> {String((lastResult as { run_summary?: { trace_article_limit?: number } }).run_summary?.trace_article_limit ?? "—")} (table below)
            </li>
          </ul>
          <details style={{ marginTop: 8 }}>
            <summary>Inserted article IDs (this ingest)</summary>
            <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 120 }}>
              {JSON.stringify((lastResult as { run_summary?: { ingest_inserted_article_ids?: string[] } }).run_summary?.ingest_inserted_article_ids ?? [], null, 2)}
            </pre>
          </details>
          <details style={{ marginTop: 8 }}>
            <summary>Process article IDs (scored in step 2)</summary>
            <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 120 }}>
              {JSON.stringify((lastResult as { run_summary?: { process_article_ids?: string[] } }).run_summary?.process_article_ids ?? [], null, 2)}
            </pre>
          </details>
        </section>
      )}

      {lastResult && (
        <section style={{ marginBottom: 20, border: "1px solid #ddd", borderRadius: 8, padding: 12 }}>
          <h2>Funnel Output</h2>
          <h3>Step 1a: NewsAPI retrieval (two-phase)</h3>
          {(lastResult as any)?.ingest?.step_1_broad?.step_1_retrieval ? (
            <div style={{ marginBottom: 12 }}>
              <details open>
                <summary>Direct search (entity / product / competitor names)</summary>
                <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 200, background: "#f9f9f9", padding: 8 }}>
                  {JSON.stringify(
                    (lastResult as any).ingest.step_1_broad.step_1_retrieval.step_1_direct_terms,
                    null,
                    2
                  )}
                </pre>
              </details>
              <details open style={{ marginTop: 8 }}>
                <summary>Broad search (industry / themes / synonyms)</summary>
                <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 200, background: "#f9f9f9", padding: 8 }}>
                  {JSON.stringify(
                    (lastResult as any).ingest.step_1_broad.step_1_retrieval.step_2_broad_terms,
                    null,
                    2
                  )}
                </pre>
              </details>
              <details style={{ marginTop: 8 }}>
                <summary>Brief semantic filter (broad-only)</summary>
                <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 220, background: "#f9f9f9", padding: 8 }}>
                  {JSON.stringify(
                    (lastResult as any).ingest.step_1_broad.step_1_retrieval.step_3_semantic_filter,
                    null,
                    2
                  )}
                </pre>
              </details>
              <details style={{ marginTop: 8 }}>
                <summary>Scored / kept articles (ingest)</summary>
                <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 280, background: "#f9f9f9", padding: 8 }}>
                  {JSON.stringify(
                    (lastResult as any).ingest.step_1_broad.step_1_retrieval.scored_articles,
                    null,
                    2
                  )}
                </pre>
              </details>
            </div>
          ) : (
            <p style={{ color: "#555", fontSize: 14 }}>
              No two-phase retrieval trace (legacy ingest or custom feed). See evaluations below.
            </p>
          )}

          <h3>Step 1b: Candidates inserted (evaluations)</h3>
          {Array.isArray((lastResult as any)?.ingest?.step_1_broad?.evaluations) &&
          (lastResult as any).ingest.step_1_broad.evaluations.length > 0 ? (
            <div style={{ maxHeight: 280, overflow: "auto", border: "1px solid #eee", padding: 8 }}>
              {(lastResult as any).ingest.step_1_broad.evaluations.map((row: any, idx: number) => (
                <p key={`${row.url || row.title}-${idx}`} style={{ margin: "4px 0", fontSize: 14 }}>
                  {row.selected_for_step_2 ? "PASS" : "FAIL"}
                  {row.retrieval_tier != null ? ` [${row.retrieval_tier}]` : ""}
                  {row.ingest_semantic_score != null ? ` score=${row.ingest_semantic_score}` : ""}
                  {row.ingest_semantic_note ? ` — ${row.ingest_semantic_note}` : ""} — {row.title}
                </p>
              ))}
            </div>
          ) : (
            <p>No step 1 evaluation data available yet.</p>
          )}

          <h3 style={{ marginTop: 12 }}>Step 2: Strict Scoring and Display Pick</h3>
          {Array.isArray((lastResult as any)?.process?.step_2_evaluations) && (lastResult as any).process.step_2_evaluations.length > 0 ? (
            <div style={{ maxHeight: 280, overflow: "auto", border: "1px solid #eee", padding: 8 }}>
              {(lastResult as any).process.step_2_evaluations.map((row: any, idx: number) => (
                <p key={`${row.article_id || row.title}-${idx}`} style={{ margin: "4px 0" }}>
                  {row.passed_step_2 ? "PASS" : "FAIL"} / {row.displayed ? "DISPLAYED" : "NOT_DISPLAYED"} - {row.title}
                </p>
              ))}
            </div>
          ) : (
            <p>No step 2 scoring data available yet.</p>
          )}
        </section>
      )}
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

      {busy && !trace && (
        <p style={{ color: "#555", marginBottom: 16 }}>
          Running pipeline… The trace table will appear when this step completes. (A background trace load can wait
          while the API runs this job.)
        </p>
      )}
      {traceLoading && !trace && !busy && (
        <p style={{ color: "#555", marginBottom: 16 }}>Loading reasoning trace…</p>
      )}
      {!traceLoading && !trace && !busy && (
        <div style={{ border: "1px solid #fca", background: "#fff8f0", borderRadius: 8, padding: 12, marginBottom: 16 }}>
          <p style={{ margin: "0 0 8px", fontWeight: 600 }}>Unable to load reasoning trace</p>
          {traceError ? (
            <p style={{ margin: 0, fontFamily: "monospace", fontSize: 13, whiteSpace: "pre-wrap" }}>{traceError}</p>
          ) : (
            <p style={{ margin: 0 }}>Empty response — try Refresh Trace or check the API.</p>
          )}
        </div>
      )}
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
            <p>
              <strong>Scoring:</strong>{" "}
              {trace.user.scoring_framework === "context_profiles_v1"
                ? "Context profile (keywords, semantic signals, key drivers & risk triggers) — not legacy event-weight blend"
                : trace.user.scoring_framework || "See API version; re-run Build Context + Process after upgrading backend"}
            </p>
            {trace.trace_meta && (
              <p>
                <strong>Trace window:</strong> last {trace.trace_meta.article_limit} articles by date ·{" "}
                <strong>With scores in view:</strong> {trace.trace_meta.assessments_in_view}
              </p>
            )}
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
                {"description" in company && company.description ? <p>Description: {company.description}</p> : null}
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
                {(row.matched_company_name || row.matched_company_id) && (
                  <p>
                    <strong>Best match:</strong> {row.matched_company_name || row.matched_company_id}
                    {row.relevance_type ? ` (${row.relevance_type})` : ""}
                  </p>
                )}
                {row.conclusion && (
                  <p>
                    <strong>Conclusion:</strong> {row.conclusion}
                  </p>
                )}
                {row.passed_step_2 != null && (
                  <p>
                    <strong>Step 2 pass:</strong> {row.passed_step_2 ? "yes" : "no"} | <strong>Displayed:</strong>{" "}
                    {row.displayed == null ? "n/a" : row.displayed ? "yes" : "no"}
                  </p>
                )}
                {row.score ? (
                  <>
                    <p>
                      <strong>Score ({row.score.source}):</strong> final {row.score.final_score.toFixed(3)} (base {row.score.base_score.toFixed(3)})
                    </p>
                    <p>
                      <strong>Threshold pass:</strong> {row.score.passes_threshold ? "yes" : "no"} | <strong>Insight created:</strong>{" "}
                      {row.insight_created ? "yes" : "no"}
                    </p>
                    {row.score.components && (
                      <>
                        <p>
                          <strong>Context relevance:</strong> {row.score.components.semantic_relevance.toFixed(3)} ({row.score.components.semantic_category})
                        </p>
                        <p style={{ fontSize: 14, color: "#333" }}>{row.score.components.semantic_reason}</p>
                        <p>
                          <strong>Entity match:</strong> {row.score.components.entity_match.toFixed(3)}
                          {row.score.components.driver_risk_triggered ? (
                            <>
                              {" "}
                              | <strong>Driver/risk trigger:</strong> yes
                              {row.score.components.driver_risk_matches
                                ? ` — ${row.score.components.driver_risk_matches}`
                                : ""}
                            </>
                          ) : null}
                        </p>
                      </>
                    )}
                  </>
                ) : (
                  <p>
                    <strong>Score:</strong> not yet assessed for this user (run <em>Process</em> or <em>Generate Full Trace</em>)
                  </p>
                )}
              </article>
            ))}
          </section>
        </>
      )}
    </main>
  );
}

