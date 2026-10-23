"use client";

import { useEffect, useMemo, useState } from "react";

const CATS = ["全部", "图解", "数据", "叙事", "装置", "单据", "钩子"];

export default function Templates() {
  const [manifest, setManifest] = useState([]);
  const [meta, setMeta] = useState({});
  const [cat, setCat] = useState("全部");
  const [q, setQ] = useState("");
  const [active, setActive] = useState(null);

  useEffect(() => {
    fetch("/tpl/index.json").then((r) => r.json()).then(setManifest).catch(() => {});
    fetch("/api/templates").then((r) => r.json()).then((list) => {
      const m = {};
      for (const t of list) m[t.id] = t;
      setMeta(m);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") setActive(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 深链：/templates?t=diagram-flow 直接打开详情
  useEffect(() => {
    if (!manifest.length) return;
    const id = new URLSearchParams(window.location.search).get("t");
    const hit = manifest.find((x) => x.id === id);
    if (hit) setActive(hit);
  }, [manifest]);

  const open = (t) => {
    setActive(t);
    window.history.replaceState(null, "", `/templates?t=${t.id}`);
  };
  const close = () => {
    setActive(null);
    window.history.replaceState(null, "", "/templates");
  };

  const items = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return manifest
      .filter((t) => cat === "全部" || t.category === cat)
      .filter((t) => !kw ||
        (t.name + " " + t.id + " " + (t.description || "") + " " + t.motion).toLowerCase().includes(kw))
      .sort((a, b) => a.name.localeCompare(b.name, "zh"));
  }, [manifest, cat, q]);

  const counts = useMemo(() => {
    const c = { 全部: manifest.length };
    for (const t of manifest) c[t.category] = (c[t.category] || 0) + 1;
    return c;
  }, [manifest]);

  const detail = active ? { ...active, fields: (meta[active.id] || {}).fields || [] } : null;

  return (
    <main className="wrap">
      <div className="topbar">
        <div className="brand">
          <h1>模板库</h1>
          <span className="sub">{manifest.length || "…"} 套 · 真实渲染预览 · 字段严格校验</span>
        </div>
        <input
          className="tpl-search"
          placeholder="搜索模板 / 关键词 / motion…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div className="tpl-filters">
        {CATS.map((c) => (
          <button
            key={c}
            className={"tpl-chip" + (cat === c ? " active" : "")}
            onClick={() => setCat(c)}
          >
            {c}
            <span>{counts[c] || 0}</span>
          </button>
        ))}
      </div>

      {items.length === 0 && (
        <p className="hint-line">
          没有匹配的模板。{manifest.length === 0 && "预览清单未生成：运行 make previews。"}
        </p>
      )}

      <div className="tpl-grid">
        {items.map((t) => (
          <button className="tpl-card" key={t.id} onClick={() => open(t)}>
            <span className="tpl-shot">
              <img src={t.preview} alt={`${t.name} 预览`} loading="lazy" />
            </span>
            <span className="tpl-info">
              <span className="tpl-name">{t.name}</span>
              <span className="tpl-id">{t.id}</span>
            </span>
            <span className="tpl-badges">
              <span className="badge warn">{t.category}</span>
              <span className="badge">{t.motion}</span>
            </span>
          </button>
        ))}
      </div>

      {detail && (
        <div className="tpl-modal" onClick={close}>
          <div className="tpl-panel" onClick={(e) => e.stopPropagation()}>
            <button className="tpl-close" onClick={close} aria-label="关闭">×</button>
            <img className="tpl-big" src={detail.preview} alt={`${detail.name} 预览`} />
            <div className="tpl-detail-head">
              <h2>{detail.name}</h2>
              <span className="tpl-id">{detail.id}</span>
            </div>
            <p className="hint-line" style={{ marginTop: 0 }}>{detail.description}</p>
            <div className="tpl-badges" style={{ marginBottom: 14 }}>
              <span className="badge warn">{detail.category}</span>
              <span className="badge">{detail.motion}</span>
              <span className="badge">{detail.role}</span>
              {(detail.aspects || []).map((a) => <span className="badge" key={a}>{a}</span>)}
              {meta[detail.id] && (
                <span className="badge">
                  {meta[detail.id].min_duration_s}–{meta[detail.id].max_duration_s}s
                </span>
              )}
            </div>
            {detail.fields.length > 0 && (
              <div className="tpl-fields">
                {detail.fields.map((f) => (
                  <div className="tpl-field" key={f.key}>
                    <span className="tpl-fkey">{f.key}{f.required ? "" : "?"}</span>
                    <span className="tpl-ftype">
                      {f.type === "list"
                        ? `${f.min_items ?? "-"}–${f.max_items ?? "-"} × ≤${f.item_max_length} 字`
                        : `≤${f.max_length} 字`}
                    </span>
                    <span className="tpl-fdesc">{f.description || f.label || ""}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
