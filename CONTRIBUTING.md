# 贡献指南

欢迎给 CapsuleCast 提 PR！最常见也最受欢迎的贡献是**新模板**——整套系统就是围着模板库转的。

## 快速上手

```bash
git clone https://github.com/potoior/CapsuleCast.git && cd CapsuleCast
make infra                                   # PG :5433 + Redis :6380
python -m venv .venv && source .venv/bin/activate
pip install -r apps/api/requirements.txt
cp .env.example .env                         # 填 LLM 网关和 Chrome 路径
make api & make worker & make web            # 三个终端跑三个
pytest apps/api/tests -q                     # 57 条回归必须全绿
```

## 添加一个模板（五步）

**第 0 步 · 脚手架**

```bash
.venv/bin/python scripts/new_template.py your-template "中文名" "一句话描述"
```

生成 `templates/your-template/` 三件套骨架，然后往里填肉。

**三件套 =**

| 文件 | 职责 |
|---|---|
| `meta.json` | 元信息 + `example`（示例字段，**必须通过自身 schema**，有测试守着） |
| `schema.json` | 字段约束（LLM 按它合成字段， maxLength 就是硬上限） |
| `template.html` | Jinja + CSS + GSAP 动画 |

**第 1 步 · 写 schema**：先想清楚这个模板讲什么故事、需要哪几个字段。字符串用 `|` 分段（如 `「要点|小注」`）是全库惯例。

**第 2 步 · 写 template.html**，必须遵守的运行时铁律（全部有回归测试）：

- 动画一律 `tl.fromTo(...)`——裸 `to()` 在逐帧 seek 捕获下不可靠
- 动画目标用**稳定 id 的深叶子**（`#s-…-kicker`）；✗ 禁止 tween `.hf-safe` 或其直接子元素（HF 托管层，opacity 会被合成器覆写）
- ✗ 禁 `left/top` tween（整数像素吸附）、禁 `letterSpacing` tween
- 可选字段一律 `fields.get('note')`；✗ `fields.note` 之外的 `fields.items/keys/values` 会撞 dict 方法遮蔽
- 时间轴收尾注册 `window.__timelines["{{ scene_id }}"] = tl`，前面加 `tl.to({}, { duration: D }, 0)` 全跨度锚点
- 布局数学写在 build 期（直接写 `el.style`），动画只碰 `transform/opacity/clipPath` 等可平滑属性
- 竖屏适配：`@container frame (max-aspect-ratio: 10/16) { … }` 断点
- 可用的共享动画库 `window.AM`：`pop / maskRise / wipe / focus / draw / countUp / kenburns / pulse / shimmer / float / exitSoft`（出场一律 `{% if not is_last %}AM.exitSoft(...){% endif %}`）
- 尺寸单位用容器单位 `cqw/cqh`，别用 px

**第 3 步 · 登记四处**（缺一处 LLM 选不到你的模板）：

1. `apps/api/app/services/outline.py` → `TEMPLATE_HINTS` 列表 + 教学文案区（一句话教 LLM 什么时候选它、字段怎么给）
2. `apps/api/app/services/llm.py` → `DIAGRAM_TIDS`（进 LLM 字段合成链）
3. `apps/api/app/services/storyboard.py` → 启发式 regex（关键词触发）+ `_EXTRACTORS` 注册表（结构复杂的直接 `raise _NoFit("交给 LLM 合成")`）
4. `apps/api/tests/test_templates.py` → 模板数锁（`>= 68` 改成新数）

**第 4 步 · 验收**

```bash
.venv/bin/python /tmp/tpl-check.py your-template   # lint/运行时/布局/对比度四道检
pytest apps/api/tests -q                            # 全绿
```

渲染出真帧肉眼看一遍（ComposeResult.project_dir 用 `hf.snapshot_frames`），别只信 check。

## 提 PR 前

- `pytest apps/api/tests -q` 全绿；新模板过了四道检
- commit message 中文/英文均可，说清"加了什么、为什么"
- PR 描述里最好放一张渲染帧截图

## 代码风格

- Python：`ruff check apps/api scripts --fix`（配置见 `pyproject.toml`；line-length 100，其余默认）
- 模板 JS：不跑独立 linter，但测试里有剥 Jinja 后的 `node --check` 语法门
- 命名：模板 id 用 kebab-case；服务层函数 `_` 前缀表内部；日志用中文短句，方便运营读

## 报 bug / 提想法

Issue 里说清楚：主题输入是什么、哪一镜不对、worker 日志（`tail -f` worker 输出）贴一段。有渲染帧截图最好。
