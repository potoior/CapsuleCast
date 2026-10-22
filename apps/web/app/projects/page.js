"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

const STATUS_LABEL = {
  draft: "草稿", outlining: "大纲生成中", outline_ready: "大纲就绪",
  storyboarding: "分镜生成中", storyboard_ready: "分镜就绪",
  rendering: "渲染中", done: "已完成", failed: "失败",
};
const STATUS_CLASS = {
  done: "ok", failed: "err", outlining: "run", storyboarding: "run", rendering: "run",
};

export default function ProjectList() {
  const [projects, setProjects] = useState([]);

  useEffect(() => {
    let stop = false;
    async function pull() {
      try {
        const r = await fetch("/api/projects");
        if (!stop) setProjects(await r.json());
      } catch {}
    }
    pull();
    const t = setInterval(pull, 5000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  return (
    <main className="wrap">
      <div className="topbar">
        <div className="brand">
          <h1>项目列表</h1>
          <span className="sub">{projects.length} 个项目</span>
        </div>
        <Link href="/" className="btn sm">＋ 新建</Link>
      </div>
      {projects.length === 0 && <p className="hint-line">还没有项目。<Link href="/">从主题开始 →</Link></p>}
      {projects.map((p) => (
        <Link key={p.id} href={`/project/${p.id}`} style={{ textDecoration: "none" }}>
          <div className="list-item">
            <div>
              <div className="title">{p.title}</div>
              <div className="meta">
                {p.mode === "topic" ? `主题：${p.topic}` : "文案模式"} · {p.aspect}
                {p.scenes?.length ? ` · ${p.scenes.length} 镜` : ""}
                {" · "}{new Date(p.created_at).toLocaleString("zh-CN")}
              </div>
            </div>
            <span className={`badge ${STATUS_CLASS[p.status] || ""}`}>
              {STATUS_LABEL[p.status] || p.status}
            </span>
          </div>
        </Link>
      ))}
    </main>
  );
}
