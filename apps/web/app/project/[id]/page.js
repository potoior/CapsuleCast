"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

const STATUS_LABEL = {
  draft: "草稿", outlining: "大纲生成中", outline_ready: "大纲就绪",
  storyboarding: "分镜生成中", storyboard_ready: "分镜就绪",
  rendering: "渲染中", done: "已完成", failed: "失败",
};
const STAGES = ["tts", "compose", "check", "render", "publish"];
const STAGE_LABEL = { tts: "语音合成", compose: "组装时间线", check: "质检", render: "渲染", publish: "发布" };
const HINTS = ["", "concept-compare", "bullet-list", "big-stat", "process-steps", "term-explainer", "timeline-events"];
const HINT_LABEL = {
  "": "自动选择", "concept-compare": "概念对比", "bullet-list": "要点清单",
  "big-stat": "大数字", "process-steps": "流程步骤", "term-explainer": "术语解释",
  "timeline-events": "时间线",
};

function fmtDur(s) { return s ? `${s.toFixed(1)}s` : "—"; }

/* ── 流水线步骤条 ───────────────────────────────────────── */
function PipelineSteps({ status }) {
  const order = ["topic", "outline", "storyboard", "video"];
  const pos = {
    draft: 0, outlining: 0, outline_ready: 1,
    storyboarding: 1, storyboard_ready: 2,
    rendering: 3, done: 4, failed: -1,
  }[status] ?? 0;
  const labels = ["主题", "大纲", "HTML 分镜", "视频"];
  return (
    <div className="pipeline-steps">
      {labels.map((l, i) => {
        const cls = i < pos ? "done" : i === pos ? "now" : "wait";
        return (
          <span key={l} style={{ display: "contents" }}>
            <span className={`pstep ${cls}`}>
              <span className="n">{i < pos ? "✓" : i + 1}</span> {l}
            </span>
            {i < labels.length - 1 && <span className="plink" />}
          </span>
        );
      })}
    </div>
  );
}

/* ── 大纲编辑器 ─────────────────────────────────────────── */
function OutlineEditor({ project, onSaved, onConfirm, busy }) {
  const [o, setO] = useState(project.outline || { title: "", summary: "", hook: { narration: "" }, beats: [], outro: {} });
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [hookOpts, setHookOpts] = useState(null);
  const [hookLoading, setHookLoading] = useState(false);

  async function genHookVariants() {
    setHookLoading(true); setHookOpts(null);
    try {
      const r = await fetch(`/api/projects/${project.id}/outline/hook-variants`, { method: "POST" });
      const d = await r.json();
      if (r.ok) setHookOpts(d.hooks || []);
      else setMsg("钩子生成失败：" + (d.detail || ""));
    } catch (e) { setMsg("钩子生成失败：" + e.message); }
    finally { setHookLoading(false); }
  }

  function set(path, value) {
    setO((prev) => {
      const next = structuredClone(prev);
      let cur = next;
      const keys = path.split(".");
      for (let i = 0; i < keys.length - 1; i++) cur = cur[keys[i]];
      cur[keys[keys.length - 1]] = value;
      return next;
    });
  }

  async function save() {
    setSaving(true); setMsg("");
    try {
      const r = await fetch(`/api/projects/${project.id}/outline`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(o),
      });
      const d = await r.json();
      if (!r.ok) { setMsg(typeof d.detail === "string" ? d.detail : "保存失败"); return; }
      setMsg("已保存");
      onSaved();
    } finally { setSaving(false); }
  }

  async function confirm(direct) {
    // 先保存当前编辑内容，再确认
    setSaving(true); setMsg("");
    try {
      let sr = await fetch(`/api/projects/${project.id}/outline`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(o),
      });
      if (!sr.ok) {
        const d = await sr.json();
        setMsg(typeof d.detail === "string" ? d.detail : "大纲校验失败"); return;
      }
      onConfirm(direct);
    } finally { setSaving(false); }
  }

  return (
    <div className="outline-editor">
      {project.engine === "scaffold" && (
        <div className="err-box" style={{ marginBottom: 14 }}>
          ⚠ 大模型生成失败，当前显示的是<b>占位骨架</b>（要点/旁白都是模板文字）。
          点右上的「↻ 重新扩写」即可重试——通常是偶发的输出格式问题，重试一般就能成功。
        </div>
      )}
      <div className="o-head">
        <input value={o.title || ""} onChange={(e) => set("title", e.target.value)}
          placeholder="视频标题（≤24 字）" />
        <span className="badge">{(o.beats || []).length + 2} 镜 · 约 {Math.round(((o.hook?.narration || "").length + (o.beats || []).reduce((a, b) => a + (b.narration || "").length, 0) + (o.outro?.narration || "").length) / 3.4)}s</span>
      </div>

      <div className="o-block">
        <label>① 开场钩子（旁白）
          <button type="button" className="btn ghost sm" onClick={genHookVariants}
            disabled={hookLoading} style={{ marginLeft: 10 }}>
            {hookLoading ? "生成中…" : "⚡ 换 3 个说法"}
          </button>
        </label>
        <textarea value={o.hook?.narration || ""} rows={2}
          onChange={(e) => set("hook.narration", e.target.value)} />
        {hookOpts && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
            {hookOpts.map((h, i) => (
              <button key={i} type="button" className="card"
                style={{ textAlign: "left", padding: "10px 14px", cursor: "pointer",
                         borderColor: "var(--border-strong)", fontSize: 13.5, fontWeight: 500 }}
                onClick={() => { set("hook.narration", h); setHookOpts(null); }}>
                <span style={{ color: "var(--coral-bright)", fontFamily: "var(--mono)",
                  fontSize: 11, marginRight: 8 }}>{["反直觉", "提问式", "场景代入"][i] || "变体" + (i + 1)}</span>
                {h}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="o-block">
        <label>② 内容层次（每层 = 一镜）</label>
        {(o.beats || []).map((b, i) => (
          <div className="beat" key={i}>
            <div className="beat-head">
              <span className="no">{String(i + 1).padStart(2, "0")}</span>
              <input value={b.title || ""} placeholder="层次标题（≤12 字）"
                onChange={(e) => set(`beats.${i}.title`, e.target.value)} />
              <select value={b.template_hint || ""}
                onChange={(e) => set(`beats.${i}.template_hint`, e.target.value)}>
                {HINTS.map((h) => <option key={h} value={h}>{HINT_LABEL[h]}</option>)}
              </select>
              <button type="button" className="icon-btn" title="删除这一层"
                onClick={() => set("beats", o.beats.filter((_, j) => j !== i))}>✕</button>
            </div>
            <div className="pts">
              {(b.points || []).map((p, j) => (
                <div className="pts-row" key={j}>
                  <span className="dot" />
                  <input className="quiet" value={p} placeholder={`要点 ${j + 1}（≤14 字）`}
                    onChange={(e) => set(`beats.${i}.points.${j}`, e.target.value)} />
                </div>
              ))}
            </div>
            <textarea value={b.narration || ""} rows={2}
              placeholder="这一层的旁白（口语化中文，15-60 字，将用于 TTS）"
              onChange={(e) => set(`beats.${i}.narration`, e.target.value)} />
          </div>
        ))}
        {(o.beats || []).length < 4 && (
          <button type="button" className="btn ghost sm"
            onClick={() => set("beats", [...(o.beats || []), { title: "", points: ["", ""], narration: "", template_hint: "" }])}>
            ＋ 添加一层
          </button>
        )}
      </div>

      <div className="o-block">
        <label>③ 收尾（金句 + 行动号召）</label>
        <div className="row" style={{ marginBottom: 8 }}>
          <input value={o.outro?.quote || ""} placeholder="金句（≤24 字）"
            onChange={(e) => set("outro.quote", e.target.value)} />
          <input value={o.outro?.cta || ""} placeholder="行动号召（≤12 字）"
            onChange={(e) => set("outro.cta", e.target.value)} />
        </div>
        <textarea value={o.outro?.narration || ""} rows={2}
          placeholder="收尾旁白"
          onChange={(e) => set("outro.narration", e.target.value)} />
      </div>

      {msg && <div className="hint-line">{msg}</div>}
      <div className="spacer" />
      <div className="flex-between">
        <button className="btn ghost" disabled={saving} onClick={save}>保存大纲</button>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn" disabled={saving || busy}
            onClick={() => confirm(false)}>确认，生成分镜 →</button>
          <button className="btn" style={{ background: "var(--green)" }} disabled={saving || busy}
            onClick={() => confirm(true)} title="跳过 HTML 分镜预览，分镜生成后自动渲染">
            ⚡ 跳过预览，直接生成视频
          </button>
        </div>
      </div>
      <p className="hint-line">⚡ 直出模式仍会经过完整质检与分镜生成，只是不再等你确认分镜页面。</p>
    </div>
  );
}

/* ── 渲染任务监控 ───────────────────────────────────────── */
function JobMonitor({ job, projectId }) {
  const [logs, setLogs] = useState([]);
  const afterId = useRef(0);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!job) return;
    let stop = false;
    async function pull() {
      try {
        const r = await fetch(`/api/projects/${projectId}/jobs/${job.id}/logs?after_id=${afterId.current}&limit=300`);
        const d = await r.json();
        if (stop || !Array.isArray(d) || d.length === 0) return;
        afterId.current = d[d.length - 1].id;
        setLogs((prev) => [...prev, ...d]);
        requestAnimationFrame(() => {
          if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
        });
      } catch {}
    }
    pull();
    const t = setInterval(pull, 2000);
    return () => { stop = true; clearInterval(t); };
  }, [job?.id, projectId]);

  if (!job) return null;
  const stageIdx = STAGES.indexOf(job.stage);
  return (
    <div className="card">
      <h2>
        渲染进度
        <span className={`badge ${job.status === "succeeded" ? "ok" : job.status === "failed" ? "err" : "run"}`}>
          {job.status === "succeeded" ? "完成" : job.status === "failed" ? "失败" : job.status === "running" ? "进行中" : "排队中"}
        </span>
        {job.attempts > 1 && <span className="badge">尝试 {job.attempts} 次</span>}
      </h2>
      <div className="stage-steps">
        {STAGES.map((s, i) => (
          <div key={s} className={`st ${i < stageIdx || job.status === "succeeded" ? "done" : i === stageIdx ? "now" : ""}`}>
            {STAGE_LABEL[s]}
          </div>
        ))}
      </div>
      <div className="progress-track"><div className="progress-fill" style={{ width: `${job.progress}%` }} /></div>
      <div className="spacer" />
      <div className="logbox" ref={boxRef}>
        {logs.map((l) => (
          <div key={l.id} className={l.level === "error" ? "lv-error" : l.level === "warn" ? "lv-warn" : ""}>
            <span className="ts">{new Date(l.ts).toLocaleTimeString("zh-CN", { hour12: false })}</span>
            {l.stage && <span className="st">[{l.stage}]</span>}
            {l.message}
          </div>
        ))}
        {logs.length === 0 && <div style={{ color: "#8b949e" }}>等待日志…</div>}
      </div>
      {job.error && <div className="err-box">{job.error}</div>}
    </div>
  );
}

/* ── 场景编辑抽屉 ───────────────────────────────────────── */
function SceneEditor({ scene, templates, onClose, onSaved }) {
  const [fields, setFields] = useState(scene.fields || {});
  const [voiceText, setVoiceText] = useState(scene.voice_text);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [tplId, setTplId] = useState(scene.template_id);
  const tpl = (templates || []).find((t) => t.id === tplId) || null;
  const fdefs = tpl?.fields || [];
  const setField = (k, v) => setFields((f) => ({ ...f, [k]: v }));

  async function save() {
    setSaving(true); setError("");
    try {
      const body = { fields, voice_text: voiceText };
      if (tplId !== scene.template_id) body.template_id = tplId;
      const r = await fetch(`/api/projects/${scene.projectId}/scenes/${scene.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) {
        const det = d.detail;
        setError(typeof det === "string" ? det : (det?.issues || [det]).join("\n"));
        return;
      }
      onSaved(); onClose();
    } finally { setSaving(false); }
  }

  function renderItemField(f) {
    const val = fields[f.key] || [];
    const list = Array.isArray(val) ? val : [];
    const isObj = list.length > 0 && typeof list[0] === "object";
    return (
      <div className="field" key={f.key}>
        <label>{f.label} <span className="limit">{list.length}/{f.max_items ?? "–"} 项</span></label>
        <div className="list-editor">
          {list.map((it, i) => (
            <div className="item" key={i}>
              <span className="no">{String(i + 1).padStart(2, "0")}</span>
              {isObj ? (
                <div style={{ flex: 1, display: "flex", gap: 6 }}>
                  {Object.keys(it).map((k) => (
                    <input key={k} type="text" value={it[k] || ""} placeholder={k}
                      onChange={(e) => setField(f.key, list.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)))} />
                  ))}
                </div>
              ) : (
                <input style={{ flex: 1 }} type="text" value={it} maxLength={f.item_max_length}
                  onChange={(e) => setField(f.key, list.map((x, j) => (j === i ? e.target.value : x)))} />
              )}
              <button type="button" className="btn ghost sm"
                onClick={() => setField(f.key, list.filter((_, j) => j !== i))}>删除</button>
            </div>
          ))}
          {list.length < (f.max_items || 99) && (
            <button type="button" className="btn ghost sm"
              onClick={() => setField(f.key, [...list, isObj ? { label: "", text: "" } : ""])}>
              ＋ 添加一条
            </button>
          )}
        </div>
      </div>
    );
  }

  const [uploading, setUploading] = useState(false);

  async function uploadImage(file) {
    setUploading(true); setError("");
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch(`/api/projects/${scene.projectId}/media`, { method: "POST", body: fd });
      const d = await r.json();
      if (r.ok) setField("image", `media:${d.key}`);
      else setError(d.detail || "上传失败");
    } catch (e) { setError("上传失败：" + e.message); }
    finally { setUploading(false); }
  }

  function renderImageField(f) {
    const v = fields[f.key] || "";
    const previewUrl = v.startsWith("media:")
      ? `/api/files/${v.slice(6)}` : "";
    return (
      <div className="field" key={f.key}>
        <label>{f.label}</label>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {previewUrl && (
            <img src={previewUrl} alt="预览" style={{ width: 108, height: 66, objectFit: "cover",
              borderRadius: 8, border: "1px solid var(--border-strong)" }} />
          )}
          <label className="btn ghost sm" style={{ cursor: "pointer" }}>
            {uploading ? "上传中…" : "＋ 上传图片"}
            <input type="file" accept="image/png,image/jpeg,image/webp" style={{ display: "none" }}
              onChange={(e) => e.target.files[0] && uploadImage(e.target.files[0])} />
          </label>
          <span className="hint-line" style={{ fontSize: 11.5 }}>
            {v ? "已就绪（渲染时自动进画面 · ken-burns 运镜）" : "支持 PNG/JPG/WebP ≤10MB · 请确认拥有图片版权"}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="drawer-mask" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="drawer">
        <h3>编辑场景 {String(scene.idx + 1).padStart(2, "0")}</h3>
        <div className="field">
          <label>模板（可切换）</label>
          <select value={tplId} onChange={(e) => setTplId(e.target.value)}>
            {(templates || []).map((t) => (
              <option key={t.id} value={t.id}>{t.name}{t.user_only ? " · 需素材" : ""}</option>
            ))}
          </select>
          <div className="hint" style={{ marginTop: 6 }}>{tpl?.description || ""} · 动效 {tpl?.motion || "—"} · 字段超限会被拒绝</div>
        </div>
        <div className="field">
          <label>旁白文案（用于 TTS，{voiceText.length} 字）</label>
          <textarea value={voiceText} rows={3} onChange={(e) => setVoiceText(e.target.value)} />
        </div>
        {fdefs.map((f) => {
          if (f.type === "list") return renderItemField(f);
          if (f.key === "image") return renderImageField(f);
          if (f.type === "select") return (
            <div className="field" key={f.key}>
              <label>{f.label}</label>
              <select value={fields[f.key] ?? ""} onChange={(e) => setField(f.key, Number(e.target.value))}>
                {(f.options || []).map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
          );
          const v = fields[f.key] ?? "";
          const over = f.max_length && String(v).length > f.max_length;
          return (
            <div className="field" key={f.key}>
              <label>{f.label} <span className="limit">{String(v).length}/{f.max_length ?? "–"}</span></label>
              {f.type === "textarea" ? (
                <textarea rows={2} value={v} className={over ? "over" : ""} maxLength={f.max_length || undefined}
                  onChange={(e) => setField(f.key, e.target.value)} />
              ) : (
                <input type="text" value={v} className={over ? "over" : ""} maxLength={f.max_length || undefined}
                  onChange={(e) => setField(f.key, e.target.value)} />
              )}
            </div>
          );
        })}
        {error && <div className="err-box">{error}</div>}
        <div className="spacer" />
        <div className="flex-between">
          <button className="btn ghost" onClick={onClose}>取消</button>
          <button className="btn" disabled={saving} onClick={save}>{saving ? "保存中…" : "保存场景"}</button>
        </div>
      </div>
    </div>
  );
}

/* ── 预览播放器：逐镜快照 + TTS 试听（免渲染看片） ─────────── */
function PreviewPlayer({ scenes, templates }) {
  const [cur, setCur] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [frac, setFrac] = useState(0);
  const audioRef = useRef(null);

  const total = (scenes || []).reduce((a, s) => a + (s.duration_s || s.voice_s || 6), 0) || 1;

  const playFrom = (i) => {
    const s = scenes[i];
    if (!s || !s.voice_url) { setPlaying(false); return; }
    setCur(i);
    setPlaying(true);
    if (!audioRef.current) audioRef.current = new Audio();
    audioRef.current.src = s.voice_url;
    audioRef.current.play().catch(() => setPlaying(false));
  };

  useEffect(() => {
    const a = audioRef.current;
    if (!a) return;
    const onTime = () => setFrac(a.duration ? a.currentTime / a.duration : 0);
    const onEnd = () => {
      if (cur + 1 < scenes.length) playFrom(cur + 1);
      else { setPlaying(false); setFrac(0); }
    };
    a.addEventListener("timeupdate", onTime);
    a.addEventListener("ended", onEnd);
    return () => { a.removeEventListener("timeupdate", onTime); a.removeEventListener("ended", onEnd); };
  }, [cur, scenes]);

  useEffect(() => () => { if (audioRef.current) audioRef.current.pause(); }, []);

  const s = scenes[Math.min(cur, scenes.length - 1)] || {};
  const anyAudio = (scenes || []).some((x) => x.voice_url);
  if (!anyAudio) return null;

  return (
    <div className="pv-player" style={{ marginBottom: 16, padding: "14px 16px", border: "1px solid var(--hairline)", borderRadius: 12, background: "var(--panel)" }}>
      <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
        <button className="btn" style={{ flex: "none" }}
          onClick={() => { if (playing) { audioRef.current?.pause(); setPlaying(false); } else playFrom(cur); }}>
          {playing ? "⏸" : "▶"} 试听全片
        </button>
        <div style={{ flex: 1, display: "flex", gap: 2, height: 34 }}>
          {scenes.map((sc, i) => {
            const w = ((sc.duration_s || sc.voice_s || 6) / total) * 100;
            const active = i === cur;
            return (
              <button key={sc.id} title={`镜${i + 1} · ${sc.template_id}`}
                onClick={() => { setFrac(0); playFrom(i); }}
                style={{
                  width: w + "%", flex: "none", padding: 0, cursor: "pointer",
                  borderRadius: 4, border: "none", overflow: "hidden", position: "relative",
                  background: active ? "color-mix(in srgb, #CC785C 22%, transparent)" : "color-mix(in srgb, currentColor 8%, transparent)",
                  borderTop: active ? "2px solid #CC785C" : "2px solid transparent",
                }}>
                <span style={{
                  position: "absolute", inset: 0, width: (active ? frac * 100 : (i < cur ? 100 : 0)) + "%",
                  background: "color-mix(in srgb, #CC785C 45%, transparent)", transition: "width .12s linear",
                }} />
                <span style={{ position: "relative", fontSize: 10.5, opacity: 0.8 }}>
                  {String(i + 1).padStart(2, "0")} {templates.find((t) => t.id === sc.template_id)?.name || sc.template_id}
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="hint-line" style={{ marginTop: 8, fontSize: 12 }}>
        当前：镜 {cur + 1} · {templates.find((t) => t.id === s.template_id)?.name || s.template_id} · 「{s.voice_text?.slice(0, 30)}…」 预览为镜内静态帧 + 语音；最终成片另含动画/字幕/音效
      </div>
    </div>
  );
}

/* ── 项目详情（流水线控制台） ───────────────────────────── */
export default function ProjectPage() {
  const { id } = useParams();
  const [project, setProject] = useState(null);
  const [templates, setTemplates] = useState([]);
  const [editing, setEditing] = useState(null);
  const [rendering, setRendering] = useState(false);
  const [pacingHint, setPacingHint] = useState("");
  const [bgm, setBgm] = useState("auto");
  const [sfxOn, setSfxOn] = useState(true);
  const [progressOn, setProgressOn] = useState(true);
  const [logoPos, setLogoPos] = useState("br");
  const [capStyle, setCapStyle] = useState("standard");
  const [series, setSeries] = useState("");
  const [epNo, setEpNo] = useState("");
  const [abVoice, setAbVoice] = useState("");
  const [abTheme, setAbTheme] = useState("");
  const [voices, setVoices] = useState([]);

  useEffect(() => {
    fetch("/api/system/voices").then((r) => r.json()).then((d) => {
      const arr = Array.isArray(d) ? d : (d.zh || []).concat(d.en || [], d.others || []);
      setVoices(arr);
    }).catch(() => {});
  }, []);
  const patchSettings = async (patch) => {
    await fetch(`/api/projects/${id}/settings`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    load();
  };
  const importTemplatePack = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch("/api/templates/import", { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { alert(`模板「${j.name}」已导入，编辑场景时可选`); load(); }
    else alert(`导入失败：${j.detail || "未知错误"}`);
    e.target.value = "";
  };
  const uploadLogo = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    await fetch(`/api/projects/${id}/logo`, { method: "POST", body: fd });
    load();
  };
  const [extraAspects, setExtraAspects] = useState([]);

  useEffect(() => {
    if (project?.bgm) setBgm(project.bgm);
    if (project?.sfx_enabled !== undefined) setSfxOn(project.sfx_enabled);
    if (project?.progress_bar !== undefined) setProgressOn(project.progress_bar);
    if (project?.logo_pos) setLogoPos(project.logo_pos);
    if (project?.cap_style) setCapStyle(project.cap_style);
    if (project?.series !== undefined) setSeries(project.series || "");
    if (project?.episode_no) setEpNo(String(project.episode_no));
  }, [project?.bgm, project?.sfx_enabled, project?.progress_bar, project?.logo_pos]);

  const refresh = useCallback(async () => {
    try {
      const [pr, tr] = await Promise.all([
        fetch(`/api/projects/${id}`).then((r) => r.json()),
        fetch(`/api/templates`).then((r) => r.json()),
      ]);
      setProject(pr);
      setTemplates(tr);
    } catch {}
  }, [id]);

  useEffect(() => {
    refresh();
    const active = project && ["outlining", "storyboarding", "rendering"].includes(project.status);
    const t = setInterval(refresh, active ? 2500 : 8000);
    return () => clearInterval(t);
  }, [refresh, project?.status, project?.status === "rendering" ? 0 : 1]);

  if (!project) return <main className="wrap"><p className="hint-line">加载中…</p></main>;

  const latestRender = (project.jobs || []).find((j) => j.kind === "render");
  const latestStoryboard = (project.jobs || []).find((j) => j.kind === "storyboard");
  const latestOutline = (project.jobs || []).find((j) => j.kind === "outline");
  const mp4 = (project.artifacts || []).find((a) => a.kind === "mp4");
  const covers = (project.artifacts || []).filter((a) => a.kind === "cover");
  const copyPack = (project.artifacts || []).find((a) => a.kind === "copy");
  const poster = (project.artifacts || []).find((a) => a.kind === "poster");
  const busy = ["outlining", "storyboarding", "rendering"].includes(project.status);

  async function confirmOutline(direct) {
    await fetch(`/api/projects/${id}/outline/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direct }),
    });
    refresh();
  }

  async function regenerateOutline() {
    await fetch(`/api/projects/${id}/outline/regenerate`, { method: "POST" });
    refresh();
  }

  async function choosePacing(o) {
    setPacingHint("保存中…");
    try {
      const r = await fetch(`/api/projects/${id}/pacing`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: o.mode, target_s: o.target_s, bgm }),
      });
      const d = await r.json();
      if (r.ok) {
        setPacingHint(`${o.label}：预计成片 ${Math.round(d.estimated_s)}s（${o.hint}）`);
        refresh();
      } else setPacingHint("保存失败：" + (d.detail || ""));
    } catch (e) { setPacingHint("保存失败：" + e.message); }
  }

  async function chooseBgm(v) {
    setBgm(v);
    try {
      await fetch(`/api/projects/${id}/pacing`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: project.pacing_mode || "standard",
          target_s: project.duration_target_s || 0,
          bgm: v,
        }),
      });
    } catch {}
  }

  async function startRender(force = false) {
    setRendering(true);
    try {
      await fetch(`/api/projects/${id}/jobs/render`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force, extra_aspects: extraAspects,
          ab_variants: [
            ...(abVoice ? [{ voice: abVoice }] : []),
            ...(abTheme ? [{ theme: abTheme }] : []),
          ] }),
      });
      refresh();
    } finally { setRendering(false); }
  }

  const showOutline = project.mode === "topic" && project.outline &&
    (["outline_ready", "failed"].includes(project.status) ||
      ["storyboard_ready", "done"].includes(project.status));

  return (
    <main className="wrap">
      <div className="topbar">
        <div className="brand">
          <h1>{project.title}</h1>
          <span className="sub">
            {project.mode === "topic" ? `主题：${project.topic}` : "文案模式"} · {project.aspect} · {project.voice}
          </span>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {project.direct_to_video && <span className="badge warn">⚡ 直出模式</span>}
          <span className={`badge ${project.status === "done" ? "ok" : project.status === "failed" ? "err" : "run"}`}>
            {STATUS_LABEL[project.status] || project.status}
          </span>
          <Link href="/projects" className="btn ghost sm">← 列表</Link>
        </div>
      </div>

      <PipelineSteps status={project.status} />

      {project.status === "outlining" && (
        <div className="card"><p>正在扩写主题 → 生成大纲…（LLM 或脚手架）</p></div>
      )}
      {project.status === "storyboarding" && (
        <div className="card"><p>正在生成分镜：切分旁白 → 中文 TTS → 组装时间线 → 截取预览图…{project.direct_to_video && "（直出模式：完成后自动渲染）"}</p></div>
      )}
      {project.status === "failed" && project.error && (
        <div className="err-box">失败原因：{project.error}</div>
      )}

      {showOutline && (
        <div className="card">
          <h2>
            <span className="num">01</span> 大纲
            {["outline_ready", "failed"].includes(project.status) && project.mode === "topic" && (
              <button className="btn ghost sm" style={{ marginLeft: 8 }} disabled={busy}
                onClick={regenerateOutline}>↻ 重新扩写</button>
            )}
            {!["outline_ready", "failed"].includes(project.status) && (
              <span className="badge ok" style={{ marginLeft: 8 }}>已确认</span>
            )}
          </h2>
          {["outline_ready", "failed"].includes(project.status) ? (
            <OutlineEditor project={project} busy={busy}
              onSaved={() => refresh()} onConfirm={(direct) => confirmOutline(direct)} />
          ) : (
            <div className="outline-readonly">
              <p style={{ fontSize: 15, marginBottom: 4 }}><b>{project.outline.title}</b>
                <span style={{ color: "var(--text-dim)", fontSize: 12.5 }}> — {project.outline.summary}</span></p>
              <div className="ro-beat"><span className="no">✦</span>
                <span className="tx"><b>开场</b>：{project.outline.hook?.narration}</span></div>
              {(project.outline.beats || []).map((b, i) => (
                <div className="ro-beat" key={i}><span className="no">{String(i + 1).padStart(2, "0")}</span>
                  <span className="tx"><b>{b.title}</b>：{b.narration}</span></div>
              ))}
              <div className="ro-beat" style={{ borderBottom: "none" }}><span className="no">✓</span>
                <span className="tx"><b>收尾</b>：{project.outline.outro?.narration}</span></div>
            </div>
          )}
        </div>
      )}

      {(project.scenes || []).length > 0 && (
        <div className="card">
          <h2>
            <span className="num">02</span> HTML 分镜
            <span className="hint-line" style={{ fontWeight: 400 }}>
              共 {project.scenes.length} 镜 · 旁白自然时长 {Math.round(project.natural_duration_s || 0)}s
            </span>
          </h2>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
            <label className="btn ghost" style={{ cursor: "pointer", padding: "4px 12px", fontSize: 12.5 }}>
              导入模板包(.zip)
              <input type="file" accept=".zip" style={{ display: "none" }} onChange={importTemplatePack} />
            </label>
          </div>
          <PreviewPlayer scenes={project.scenes} templates={templates} />
          <div className="scene-grid">
            {project.scenes.map((s) => (
              <div className="scene-card" key={s.id}>
                <div className="shot">
                  {s.snapshot_url ? <img src={s.snapshot_url} alt={`镜 ${s.idx + 1}`} /> : <span className="placeholder">SNAPSHOT…</span>}
                  <span className="idx">SHOT {String(s.idx + 1).padStart(2, "0")}</span>
                </div>
                <div className="body">
                  <div className="tpl">
                    <span className="name">{templates.find((t) => t.id === s.template_id)?.name || s.template_id}</span>
                    <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <span className="dur">{fmtDur(s.voice_s || s.duration_s)}</span>
                      <button className="btn ghost sm" onClick={() => setEditing({ ...s, projectId: id })}>编辑</button>
                    </span>
                  </div>
                  <div className="vo">旁白：{s.voice_text}</div>
                </div>
              </div>
            ))}
          </div>
          <div className="spacer" />

          {(project.duration_options || []).length > 0 && (
            <>
              <h2><span className="num">03</span> 选择时长</h2>
              <div className="seg time-choice">
                {project.duration_options.map((o) => (
                  <button key={o.mode} type="button"
                    className={project.pacing_mode === o.mode ? "active" : ""}
                    onClick={() => choosePacing(o)}>
                    {o.label} · {o.target_s}s
                  </button>
                ))}
              </div>
              <p className="hint-line">{pacingHint} · 紧凑/舒缓 首次渲染会重配旁白（约多 20-30 秒）</p>
              <div className="seg time-choice" style={{ marginTop: 10 }}>
                {[["auto", "BGM · 自动"], ["none", "无音乐"], ["warm", "暖光 · 钢琴垫"],
                  ["pulse", "夜航 · 电子"], ["ambient", "薄雾 · 氛围"]].map(([v, l]) => (
                  <button key={v} type="button" className={bgm === v ? "active" : ""}
                    onClick={() => chooseBgm(v)}>{l}</button>
                ))}
              </div>
              <p className="hint-line">背景音乐随旁白自动闪避（语音出现时压低 10dB），无需手动调音</p>
              <div style={{ display: "flex", gap: 16, marginTop: 10, flexWrap: "wrap" }}>
                <label className="hint-line" style={{ display: "flex", alignItems: "center", gap: 5, cursor: "pointer", fontSize: 12.5 }}>
                  <input type="checkbox" checked={sfxOn} style={{ width: "auto", padding: 0, accentColor: "#CC785C" }}
                    onChange={(e) => { setSfxOn(e.target.checked); patchSettings({ sfx_enabled: e.target.checked }); }} />
                  音效层（转场/要点/数据）
                </label>
                <label className="hint-line" style={{ display: "flex", alignItems: "center", gap: 5, cursor: "pointer", fontSize: 12.5 }}>
                  <input type="checkbox" checked={progressOn} style={{ width: "auto", padding: 0, accentColor: "#CC785C" }}
                    onChange={(e) => { setProgressOn(e.target.checked); patchSettings({ progress_bar: e.target.checked }); }} />
                  顶部进度条
                </label>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8, flexWrap: "wrap" }}>
                <span className="hint-line" style={{ fontSize: 12.5 }}>品牌水印：</span>
                <label className="btn ghost" style={{ cursor: "pointer", padding: "4px 12px", fontSize: 12.5 }}>
                  {project?.logo_set ? "更换 logo" : "上传 logo"}
                  <input type="file" accept="image/png,image/jpeg,image/webp"
                    style={{ display: "none" }} onChange={uploadLogo} />
                </label>
                <span className="hint-line" style={{ fontSize: 12.5 }}>栏目：</span>
                <input type="text" placeholder="系列名（选填）" value={series}
                  style={{ fontSize: 12.5, padding: "4px 8px", width: 120 }}
                  onChange={(e) => setSeries(e.target.value)}
                  onBlur={() => patchSettings({ series })} />
                {series && (
                  <input type="number" min="1" max="999" placeholder="期数" value={epNo}
                    style={{ fontSize: 12.5, padding: "4px 8px", width: 64 }}
                    onChange={(e) => setEpNo(e.target.value)}
                    onBlur={() => patchSettings({ episode_no: epNo ? Number(epNo) : null })} />
                )}
                <span className="hint-line" style={{ fontSize: 12.5 }}>字幕：</span>
                <select value={capStyle} style={{ fontSize: 12.5, padding: "4px 8px" }}
                  onChange={(e) => { setCapStyle(e.target.value); patchSettings({ cap_style: e.target.value }); }}>
                  <option value="standard">标准</option>
                  <option value="large">大字</option>
                  <option value="high">位置偏高</option>
                </select>
                {project?.logo_set ? (
                  <>
                    <select value={logoPos} style={{ fontSize: 12.5, padding: "4px 8px" }}
                      onChange={(e) => { setLogoPos(e.target.value); patchSettings({ logo_pos: e.target.value }); }}>
                      <option value="br">右下</option>
                      <option value="tr">右上（替换角标）</option>
                      <option value="bl">左下</option>
                    </select>
                    <button className="btn ghost" style={{ padding: "4px 12px", fontSize: 12.5 }}
                      onClick={() => patchSettings({ logo_clear: true })}>移除</button>
                  </>
                ) : null}
              </div>
              <div className="spacer" />
            </>
          )}

          <h2><span className="num">04</span> 渲染</h2>
          <div className="flex-between">
            <details className="tech-details">
              <summary>渲染管线详情</summary>
              <span className="hint-line" style={{ display: "block" }}>
                TTS → 组装 → <code>hyperframes check</code>（布局/对比度/空白帧质检）→ Chrome GPU 捕获 → 1080p H.264 · GPU 失败自动降级软件渲染 · 支持断点续渲
              </span>
            </details>
            <div style={{ display: "flex", gap: 10 }}>
              {mp4 && <button className="btn ghost" disabled={rendering || busy} onClick={() => startRender(true)}>强制重渲</button>}
              {["9:16", "1:1"].map((a) => (
                <label key={a} className="hint-line" style={{ display: "flex", alignItems: "center", gap: 5, cursor: "pointer", fontSize: 12.5 }}>
                  <input type="checkbox" checked={extraAspects.includes(a)}
                    disabled={rendering}
                    onChange={(e) => setExtraAspects(e.target.checked
                      ? [...extraAspects, a] : extraAspects.filter((x) => x !== a))}
                    style={{ width: "auto", padding: 0, accentColor: "#CC785C" }} />
                  {a === "9:16" ? "竖版" : "方版"} {a}
                </label>
              ))}
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <select value={abVoice} style={{ fontSize: 12, padding: "4px 6px" }}
                  title="A/B 备选音色（留空 = 不出对照版）"
                  onChange={(e) => setAbVoice(e.target.value)}>
                  <option value="">A/B 音色…</option>
                  {(voices || []).map((v) => (
                    v.id === project.voice ? null :
                    <option key={v.id} value={v.id}>{v.label}</option>
                  ))}
                </select>
                <select value={abTheme} style={{ fontSize: 12, padding: "4px 6px" }}
                  title="A/B 备选风格（留空 = 不出对照版）"
                  onChange={(e) => setAbTheme(e.target.value)}>
                  <option value="">A/B 风格…</option>
                  {["dark", "broadsheet", "neon", "journal", "swiss", "inkwash", "vaporwave", "film", "blueprint", "chalkboard"].map((t) => (
                    t === project.theme ? null : <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>
              <button className={(project.status === "storyboard_ready" || project.status === "done") ? "btn" : "btn ghost"}
                disabled={rendering || busy || project.status === "storyboarding"}
                onClick={() => startRender(false)}>
                {rendering ? "提交中…" : project.status === "done" ? "重新渲染" : "确认并渲染 →"}
              </button>
            </div>
          </div>
        </div>
      )}

      {latestRender && ["rendering", "done", "failed"].includes(project.status) && (
        <JobMonitor job={latestRender} projectId={id} />
      )}
      {!latestRender && latestStoryboard && project.status === "storyboarding" && (
        <JobMonitor job={latestStoryboard} projectId={id} />
      )}

      {mp4 && project.status === "done" && (
        <div className="card player">
          <h2>
            <span className="num">05</span> 成片
            <span className="badge ok">1080p · H.264 · AAC</span>
            <span className="badge">{(mp4.meta?.duration_s || 0).toFixed(1)}s · {(mp4.bytes / 1e6).toFixed(1)}MB</span>
            {mp4.meta?.gpu_capture && <span className="badge ok">GPU 捕获</span>}
          </h2>
          <video controls poster={poster?.url} src={mp4.url} preload="metadata" />
          <div className="spacer" />
          {covers.length > 0 && (
            <div style={{ marginTop: 18 }}>
              <h2 style={{ marginBottom: 10 }}>封面候选（{covers.length}）</h2>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                {covers.map((c) => (
                  <a key={c.id} href={c.url} target="_blank" rel="noreferrer"
                    title={"点击看大图 · " + (c.meta?.variant || "")}>
                    <img src={c.url} alt={c.meta?.variant}
                      style={{ width: 284, borderRadius: 10, border: "1px solid var(--border-strong)",
                               boxShadow: "0 6px 20px rgba(0,0,0,0.35)" }} />
                  </a>
                ))}
              </div>
              <p className="hint-line" style={{ marginTop: 8 }}>三版封面随主题生成：大字标题 / 数据冲击 / 悬念问句 · 点击放大，右键另存</p>
            </div>
          )}
          {copyPack?.meta?.platforms && (
            <div style={{ marginTop: 18 }}>
              <h2 style={{ marginBottom: 10 }}>平台发布文案</h2>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
                {Object.entries(copyPack.meta.platforms).map(([pf, d]) => (
                  <div key={pf} className="card" style={{ padding: "14px 16px", margin: 0 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                      <b style={{ color: "var(--coral-bright)" }}>
                        {pf === "douyin" ? "抖音" : pf === "bilibili" ? "B站" : "视频号"}
                      </b>
                      <button className="btn ghost sm" onClick={() => {
                        const t = `标题：${d.title}\n${d.caption || d.desc || ""}\n标签：${(d.tags || []).join(" ")}`;
                        navigator.clipboard?.writeText(t);
                      }}>复制</button>
                    </div>
                    <div style={{ fontWeight: 700, fontSize: 14.5 }}>{d.title}</div>
                    <div className="hint-line" style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>
                      {d.caption || d.desc || ""}
                    </div>
                    <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6 }}>
                      {(d.tags || []).map((tg, i) => (
                        <span key={i} style={{ fontSize: 11.5, color: "var(--coral-bright)",
                          border: "1px solid rgba(204,120,92,0.45)", borderRadius: 999,
                          padding: "2px 9px" }}>#{tg}</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="flex-between">
            <span className="hint-line">已通过 hyperframes check（无超框 / 对比度 / 空白帧错误）</span>
            <span style={{ display: "flex", gap: 8 }}>
              <a className="btn" href={mp4.url} download>下载 MP4 ⬇</a>
              <a className="btn ghost" href={`/api/projects/${id}/captions.srt`} download>下载字幕 SRT</a>
            </span>
          </div>
        </div>
      )}

      {editing && (
        <SceneEditor scene={editing} templates={templates}
          onClose={() => setEditing(null)} onSaved={() => refresh()} />
      )}
    </main>
  );
}
