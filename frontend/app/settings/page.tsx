"use client";

import { useState } from "react";
import { updateMode } from "../../lib/api";

export default function SettingsPage() {
  const [mode, setMode] = useState<"high_signal" | "balanced" | "exploratory">("balanced");

  const save = async () => {
    await updateMode(mode);
    alert("Saved");
  };

  return (
    <main>
      <h1>Settings</h1>
      <p>Mode selector</p>
      <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
        <option value="high_signal">high_signal</option>
        <option value="balanced">balanced</option>
        <option value="exploratory">exploratory</option>
      </select>
      <div style={{ marginTop: 12 }}>
        <button onClick={save}>Save</button>
      </div>
      <p style={{ marginTop: 16 }}>Company management is backed by `/companies` and `/companies/link` endpoints.</p>
    </main>
  );
}

