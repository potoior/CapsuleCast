import "./globals.css";
import Shell from "../components/Shell";

export const metadata = {
  title: "llm2video · 文本转视频控制台",
  description: "主题 → 大纲 → HTML 分镜 → 1080p 成片",
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh-CN">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
