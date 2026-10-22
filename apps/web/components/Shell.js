"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

const NAV = [
  { href: "/", label: "新建项目", icon: "＋" },
  { href: "/projects", label: "项目列表", icon: "▤" },
  { href: "/templates", label: "模板库", icon: "◈" },
];

export default function Shell({ children }) {
  const path = usePathname();
  const [sys, setSys] = useState(null);

  useEffect(() => {
    let stop = false;
    async function pull() {
      try {
        const r = await fetch("/api/system/status");
        if (!stop) setSys(await r.json());
      } catch {}
    }
    pull();
    const t = setInterval(pull, 10000);
    return () => { stop = true; clearInterval(t); };
  }, []);

  return (
    <div className="console">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand">
            <h1><span className="star">✱</span> llm2video</h1>
          </div>
          <div className="brand-sub">TEXT→OUTLINE→HTML→MP4</div>
        </div>

        <nav className="nav">
          {NAV.map((n) => {
            const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
            return (
              <Link key={n.href} href={n.href}
                className={`nav-item ${active ? "active" : ""}`}>
                <span className="ico">{n.icon}</span> {n.label}
              </Link>
            );
          })}
        </nav>

        <div className="sidebar-status">
          <div className="status-title">SYSTEM</div>
          <div className="chip-row">
            <span className={`chip ${sys?.workers > 0 ? "ok" : "err"}`}>
              worker {sys ? sys.workers : "…"}
            </span>
            <span className="chip">
              queue {sys?.queue_depth ?? "…"}
            </span>
          </div>
          <div className="chip-row">
            <span className="chip" title={sys?.gpu || ""}>
              gpu {sys?.gpu ? sys.gpu.split(",")[0].replace("NVIDIA ", "") : "…"}
            </span>
          </div>
          <div className="chip-row">
            <span className={`chip ${sys?.llm_configured ? "ok" : "warn"}`}
              title={sys?.llm_configured ? "LLM 已配置：主题扩写真扩写" : "未配置 LLM：主题模式生成可编辑脚手架"}>
              llm {sys?.llm_configured ? "on" : "scaffold"}
            </span>
            <span className="chip">tpl {sys?.templates ?? "…"}</span>
          </div>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}
