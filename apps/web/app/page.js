"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const ASPECTS = [
  { id: "16:9", label: "16:9 横屏" },
  { id: "9:16", label: "9:16 竖屏" },
  { id: "1:1", label: "1:1 方形" },
];

export default function NewProject() {
  const router = useRouter();
  const [mode, setMode] = useState("topic");
  const [topic, setTopic] = useState("");
  const [script, setScript] = useState("");
  const [aspect, setAspect] = useState("16:9");
  const [voice, setVoice] = useState("zf_xiaobei");
  const [theme, setTheme] = useState("warm");
  const [accent, setAccent] = useState("#3B82F6");
  const [bgmChoice, setBgmChoice] = useState("auto");
  const [durationTarget, setDurationTarget] = useState(0);
  const [presets, setPresets] = useState([]);
  const [presetName, setPresetName] = useState("");

  useEffect(() => {
    fetch("/api/presets").then((r) => r.json()).then(setPresets).catch(() => {});
  }, []);

  function applyPreset(id) {
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    const c = p.config || {};
    if (c.theme) setTheme(c.theme);
    if (c.brand_accent) setAccent(c.brand_accent);
    if (c.voice) setVoice(c.voice);
    if (c.bgm) setBgmChoice(c.bgm);
    if (c.aspect) setAspect(c.aspect);
  }

  async function savePreset() {
    if (!presetName.trim()) return;
    const r = await fetch("/api/presets", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: presetName.trim(), theme, brand_accent: accent,
        voice, bgm: bgmChoice, aspect }),
    });
    if (r.ok) {
      setPresetName("");
      setPresets(await fetch("/api/presets").then((x) => x.json()));
    }
  }

  async function delPreset(id) {
    await fetch(`/api/presets/${id}`, { method: "DELETE" });
    setPresets(await fetch("/api/presets").then((x) => x.json()));
  }
  const [voices, setVoices] = useState([]);
  const [previewing, setPreviewing] = useState(false);

  useEffect(() => {
    fetch("/api/system/voices").then((r) => r.json())
      .then((d) => setVoices([...(d.zh || []), ...(d.en || [])])).catch(() => {});
  }, []);

  async function previewVoice() {
    setPreviewing(true);
    try {
      const r = await fetch("/api/system/voices/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice }),
      });
      const blob = await r.blob();
      new Audio(URL.createObjectURL(blob)).play();
    } catch (e) {} finally { setPreviewing(false); }
  }
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  async function create(e) {
    e.preventDefault();
    setError("");
    setCreating(true);
    try {
      const body = mode === "topic"
        ? { mode: "topic", topic: topic.trim(), aspect, voice, theme, bgm: bgmChoice,
            brand_accent: theme === "brand" ? accent : "", duration_target_s: durationTarget }
        : { mode: "script", script_text: script.trim(), aspect, voice, theme, bgm: bgmChoice,
            brand_accent: theme === "brand" ? accent : "", duration_target_s: durationTarget };
      const r = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const d = await r.json();
        throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      }
      const d = await r.json();
      router.push(`/project/${d.id}`);
    } catch (err) {
      setError(String(err.message || err));
      setCreating(false);
    }
  }

  return (
    <main className="wrap">
      <div className="topbar">
        <div className="brand">
          <h1>新建项目</h1>
          <span className="sub">主题 → 大纲 → HTML 分镜 → 1080p MP4</span>
        </div>
      </div>

      <div className="mode-tabs">
        <button className={mode === "topic" ? "active" : ""} onClick={() => setMode("topic")}>
          ✦ 主题模式（AI 扩写大纲）
        </button>
        <button className={mode === "script" ? "active" : ""} onClick={() => setMode("script")}>
          ✎ 文案模式（直接粘贴完整文案）
        </button>
      </div>

      <form className="card" onSubmit={create}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 14,
                      paddingBottom: 14, borderBottom: "1px solid var(--border)" }}>
          <label style={{ margin: 0 }}>风格预设</label>
          <select onChange={(e) => applyPreset(e.target.value)} defaultValue=""
            style={{ width: "auto", flex: "0 0 auto" }}>
            <option value="">— 选择预设 —</option>
            {presets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <span style={{ flex: 1 }} />
          <input value={presetName} onChange={(e) => setPresetName(e.target.value)}
            placeholder="预设名（存当前配置）"
            style={{ width: 180, fontSize: 12.5, padding: "7px 10px" }} />
          <button type="button" className="btn ghost sm" onClick={savePreset}
            disabled={!presetName.trim()}>存为预设</button>
        </div>
        {presets.length > 0 && (
          <div className="hint-line" style={{ marginTop: -8, marginBottom: 12, display: "flex", gap: 12, flexWrap: "wrap" }}>
            {presets.map((p) => (
              <span key={p.id} style={{ display: "inline-flex", gap: 5, alignItems: "center" }}>
                {p.config?.builtin && <span style={{ color: "var(--coral-bright)", fontSize: 10.5, border: "1px solid rgba(204,120,92,0.4)", borderRadius: 999, padding: "1px 7px" }}>内置</span>}
                {p.name}
                {!p.config?.builtin && (
                  <a onClick={() => delPreset(p.id)} style={{ cursor: "pointer", color: "var(--red)" }}>✕</a>
                )}
              </span>
            ))}
          </div>
        )}
        {mode === "topic" ? (
          <>
            <label htmlFor="topic">主题（一句话，系统扩写成大纲后由你确认）</label>
            <input
              id="topic" type="text" value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="例如：token 缓存为什么能省钱 / 团队为什么要写周报 / 什么是向量数据库"
              autoFocus
            />
            <p className="hint-line">
              流程：扩写大纲 → <b>你审阅/编辑</b> → 生成分镜 → <b>你确认</b> → 渲染；大纲页可勾选「跳过分镜预览，直接出片」
            </p>
          </>
        ) : (
          <>
            <label htmlFor="script">完整解说文案（自动切分分镜）</label>
            <textarea
              id="script" value={script}
              onChange={(e) => setScript(e.target.value)}
              placeholder="粘贴你的完整文案，系统自动切分 3-8 个分镜并选模板…"
            />
          </>
        )}
        <div className="spacer" />
        <div className="row">
          <div>
            <label>画幅</label>
            <div className="seg">
              {ASPECTS.map((a) => (
                <button key={a.id} type="button"
                  className={aspect === a.id ? "active" : ""}
                  onClick={() => setAspect(a.id)}>{a.label}</button>
              ))}
            </div>
          </div>
          <div>
            <label>旁白声音</label>
            <div style={{ display: "flex", gap: 8 }}>
              <select value={voice} onChange={(e) => setVoice(e.target.value)}>
                {(voices.length ? voices : [{ id: "zf_xiaobei", label: "小北 · 女声，平稳解说" }])
                  .map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
              </select>
              <button type="button" className="btn ghost sm" onClick={previewVoice}
                disabled={previewing} title="合成一句样例试听">
                {previewing ? "合成中…" : "▶ 试听"}
              </button>
            </div>
          </div>
        </div>
        <div className="spacer" />
        <div>
          <label>视觉主题</label>
            <div className="choice-grid">
              {[["warm", "米白 · 暖光", "#FAF9F5", "#CC785C"],
                ["dark", "夜航 · 深色", "#0E1218", "#3ECFB2"],
                ["broadsheet", "社论 · 报纸", "#F7F1E3", "#B3211E"],
                ["neon", "霓虹 · 赛博", "#04060C", "#FF3DDB"],
                ["journal", "手账 · 暖粉", "#FBEEE0", "#E8695C"],
                ["swiss", "瑞士 · 红黑", "#FFFFFF", "#E4002B"],
                ["inkwash", "水墨 · 国风", "#F4EFE4", "#A63A2B"],
                ["vaporwave", "蒸汽波 · 渐变", "#2B1854", "#FF6EC7"],
                ["film", "默片 · 黑白", "#121212", "#F0F0F0"],
                ["blueprint", "蓝图 · 制图", "#0D2B4E", "#FFD23F"],
                ["chalkboard", "黑板 · 粉笔", "#22392E", "#F7C948"],
                ["brand", "品牌 · 自定义", "#F4EFE9", "#3B82F6"]].map(([v, l, bg, ac]) => (
                <button type="button" key={v}
                  className={"choice-card" + (theme === v ? " active" : "")}
                  onClick={() => setTheme(v)}>
                  <span className="sw" style={{ background: bg, border: "2px solid " + ac }} />
                  {l}
                </button>
              ))}
            </div>
            {theme === "brand" && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                <input type="color" value={accent}
                  onChange={(e) => setAccent(e.target.value)}
                  style={{ width: 44, height: 34, padding: 2, cursor: "pointer",
                           background: "transparent", border: "1px solid var(--border-strong)",
                           borderRadius: 8 }} />
                <span className="hint-line">强调色 {accent} · 应用于标题装饰、高亮与字幕下划线</span>
              </div>
            )}
        </div>
        <div className="spacer" />
        <div>
          <label>目标时长</label>
          <div className="choice-grid fixed">
            {[[0, "自动 ~75s"], [90, "90 秒"], [120, "2 分钟"], [180, "3 分钟"],
              [240, "4 分钟"], [300, "5 分钟"]].map(([v, l]) => (
              <button type="button" key={v}
                className={"choice-card" + (durationTarget === v ? " active" : "")}
                onClick={() => setDurationTarget(v)}>{l}</button>
            ))}
          </div>
          <span className="hint-line">时长决定内容量：更长的视频会自动扩写更多层次（而非拉长停留）</span>
        </div>
        <div className="spacer" />
        <div className="flex-between">
          <span />
          <button className="btn" disabled={creating}>
            {creating ? "提交中…" : mode === "topic" ? "扩写大纲 →" : "生成分镜 →"}
          </button>
        </div>
        {error && <div className="err-box">{error}</div>}
      </form>
    </main>
  );
}
