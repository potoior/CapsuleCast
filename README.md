# CapsuleCast 🎬

> **一句话主题，自动出片。** Text → Faceless Video Factory
>
> 主题 → AI 扩写大纲 → 54 套模板自动分镜 → 无头浏览器渲染 1080p 口播解说视频

每条成片自带「知识胶囊 · CAPSULE」铭牌——名字就是这么来的。

## 它能干什么

你说一句主题（或贴一篇文案），CapsuleCast 产出一条带旁白、词级字幕、配乐、音效、进度条和角标的成片：

- **两种创作模式** — 主题模式（AI 扩写结构化大纲，你审阅/编辑后出片）｜文案模式（粘贴完整文案直接分镜）
- **54 套 HTML 模板** — 图解语义（流程/层级/循环/枢纽/冰山/多米诺/泳道…）、数据图（条形/折线/雷达/甘特/散点…）、叙事载体（误区粉碎/利弊清单/前后对比/冷知识三连…），LLM 按内容自动选型 + 字段合成
- **声音** — 中文旁白 + 13 个实测英文音色，词级卡拉OK字幕，程序化 BGM 生成，numpy 合成音效层（whoosh/pop/ding/chime 按事件自动铺设）
- **运营件** — 顶部进度条、创作者铭牌、logo 水印、栏目 EP 系列（「栏目 · EP.03」角标）、A/B 对照成片（一稿双版比风格）、封面图
- **自定义模板包** — zip 导入即用：路径穿越/外链/eval 安全 lint + 金测渲染不过自动回滚
- **质量管线** — 每次渲染过 `hyperframes check`（lint + 运行时 + 布局 + 对比度），模板库 57 条回归测试

实测：60 秒视频端到端 ≈ 2–3 分钟（Chrome GPU 捕获 + 6 workers）。

## 架构

```
用户 ──► Next.js 控制台 (:3300)
            │  /api/* rewrites
            ▼
        FastAPI (:8300) ──► PostgreSQL (:5433)   项目/场景/任务/日志/产物
            │        └──► Redis (:6380)          RQ 渲染队列
            ▼
        RQ Worker（看门狗：僵死任务回收 + 僵尸浏览器清理）
            ├─ 大纲引擎    LLM 扩写（json_schema 约束解码；瞬时抖动重试 + 零 hint 自愈）
            ├─ 分镜引擎    模板选型启发式 → LLM 字段合成 → 规则抽取器兜底
            ├─ TTS         Kokoro 单进程批量合成 + 内容哈希缓存
            ├─ 组装        54 套 HTML 模板 → HyperFrames 时间线（GSAP）
            │              AD 艺术指导层（版式轴/进场方向/节奏，内容哈希播种）
            ├─ 检查        hyperframes check：lint / 运行时 / 布局 / 对比度
            └─ 渲染        Chrome GPU 捕获 → 1080p H.264 + 三路混音
```

## 快速开始

依赖：Python 3.12+、Node 20+、Docker（PG + Redis）、Chrome（无头捕获，GPU 可选）。

```bash
# 1) 基础设施（PostgreSQL :5433 + Redis :6380）
make infra

# 2) API
python -m venv .venv && source .venv/bin/activate
pip install -r apps/api/requirements.txt
cp .env.example .env   # 填你的 LLM 网关、TTS、路径
make api               # :8300

# 3) 渲染 worker（另开终端，同一个 venv）
make worker

# 4) 控制台（另开终端）
cd apps/web && npm i && cd .. && make web   # :3300
```

打开 http://127.0.0.1:3300 ，输入主题（例如「为什么熬夜会越熬越清醒」），选风格，⚡直出。

## 模板库速览

| 类别 | 模板 |
|---|---|
| 图解语义 | flow · layers · cycle · quadrant · hub · pyramid · funnel · venn · tree · radar · gauge · stairs · spectrum · balance · fishbone · journey · iceberg · blocks · domino · onion · tug · swimlane · loop-duo |
| 数据图 | bars · line · donut · table · kpi · scatter · gantt |
| 叙事载体 | myth-buster · qa-card · checklist · rank-list · dialogue-quotes · bio-card · news-card · formula-card · price-compare · before-after · pros-cons · facts-trio |
| 结构与封面 | title-hook · bullet-list · big-stat · quote-cta · image-focus · free-blocks · chapter-card · process-steps · timeline-events · concept-compare · term-demo · term-explainer |

模板 = `meta.json + schema.json + template.html` 三件套：示例字段必须过自身 schema，动画一律 `fromTo`（seek 捕获安全），可选字段必须 `fields.get()`——违反约定会被测试套件拦下。

## 开发

```bash
make e2e            # 本地端到端（不经 API/DB）
pytest apps/api/tests -q    # 57 条回归
make status         # 服务一览
```

## 说明

- 渲染管线基于 [HyperFrames](https://www.npmjs.com/package/hyperframes) 时间线 + GSAP；模板在自己的 `<template>` 上下文里执行，注入代码必须缝进首个 `<script>`（仓库内有对应回归测试）。
- LLM 走 OpenAI 兼容接口（vLLM / 内网网关均可），带模型链降级与瞬时错误重试。
- 开源协议：待定。
