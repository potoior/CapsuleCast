"use client";

import { useEffect, useState } from "react";

export default function Templates() {
  const [templates, setTemplates] = useState([]);

  useEffect(() => {
    fetch("/api/templates").then((r) => r.json()).then(setTemplates).catch(() => {});
  }, []);

  return (
    <main className="wrap">
      <div className="topbar">
        <div className="brand">
          <h1>模板库</h1>
          <span className="sub">固定模板 · 字段严格校验 · 超限直接拒绝</span>
        </div>
      </div>
      <div className="scene-grid">
        {templates.map((t) => (
          <div className="card" key={t.id} style={{ margin: 0 }}>
            <h2 style={{ marginBottom: 6 }}>
              {t.name}
              <span className="badge">{t.role}</span>
            </h2>
            <p className="hint-line" style={{ marginTop: 0 }}>{t.description}</p>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
              <span className="badge warn">{t.motion}</span>
              <span className="badge">{t.min_duration_s}–{t.max_duration_s}s</span>
              {t.aspects.map((a) => <span className="badge" key={a}>{a}</span>)}
            </div>
            <div style={{ marginTop: 12 }}>
              {t.fields.map((f) => (
                <div key={f.key} style={{
                  fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-dim)",
                  display: "flex", justifyContent: "space-between", padding: "3px 0",
                  borderBottom: "1px dashed var(--border)",
                }}>
                  <span>{f.key}{f.required ? "" : "?"}</span>
                  <span>{f.type === "list"
                    ? `${f.min_items ?? "-"}–${f.max_items ?? "-"} × ≤${f.item_max_length}`
                    : `≤${f.max_length}`}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </main>
  );
}
