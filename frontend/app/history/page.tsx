"use client";

import { useEffect, useState } from "react";
import { Insight, getHistory } from "../../lib/api";

export default function HistoryPage() {
  const [rows, setRows] = useState<Insight[]>([]);

  useEffect(() => {
    getHistory().then(setRows);
  }, []);

  return (
    <main>
      <h1>Insight History</h1>
      {rows.length === 0 && <p>No recent insights found.</p>}
      {rows.map((row) => (
        <article key={row.id} style={{ marginBottom: 16 }}>
          <p><strong>{new Date(row.created_at).toLocaleString()}</strong></p>
          <p>{row.summary}</p>
          <p>{row.why_it_matters}</p>
        </article>
      ))}
    </main>
  );
}

