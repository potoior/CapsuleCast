"""大纲引擎：主题 → 扩写 → 结构化大纲（JSON Schema 强约束）。

双路径：
  · llm（配置 LLM_BASE_URL 后）：真扩写，产出完整旁白草稿与要点
  · scaffold（无 LLM 兜底）：按"钩子/是什么/为什么/怎么做/收尾"生成
    可编辑的脚手架 —— 大纲阶段本来就要用户过目编辑，兜底不装懂

大纲结构（存 projects.outline）：
{
  "title": "…", "summary": "…",
  "hook":  {"narration": "…"},
  "beats": [{"title": "…", "points": ["…"], "narration": "…", "template_hint": "…"}],
  "outro": {"quote": "…", "cta": "…", "narration": "…"}
}
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from .llm import LLMStoryboardEngine
from .template_engine import TemplateRegistry

log = logging.getLogger(__name__)

TEMPLATE_HINTS = [
    "big-stat", "concept-compare", "bullet-list",
    "process-steps", "term-explainer", "timeline-events", "chart-bars", "term-demo",
    "diagram-flow", "diagram-layers", "diagram-cycle", "diagram-quadrant",
    "diagram-hub", "diagram-pyramid", "diagram-funnel", "diagram-venn",
    "diagram-tree", "chart-line", "chart-donut", "chart-table", "free-blocks",
    "chapter-card", "diagram-radar", "diagram-gauge", "diagram-stairs",
    "myth-buster", "chart-kpi", "diagram-spectrum", "qa-card",
    "checklist", "diagram-balance", "formula-card", "diagram-iceberg",
    "rank-list", "diagram-fishbone", "diagram-journey", "diagram-blocks",
    "dialogue-quotes", "bio-card", "news-card", "chart-scatter",
    "diagram-gantt", "price-compare",
    "before-after", "pros-cons", "facts-trio",
    "diagram-domino", "diagram-onion", "diagram-tug",
    "diagram-swimlane", "diagram-loop-duo",
    "chart-waterfall", "diagram-daywheel", "stat-clash", "stat-equivalent",
    "diagram-callouts", "habit-grid", "headline-barrage", "diagram-bottleneck",
    "chart-histogram", "chart-poll", "tier-list", "chart-splitbar",
    "experiment-log", "bio-timeline",
    "stat-dotmatrix", "stat-thermometer", "compare-trio", "progress-bundle",
    "sort-bins", "chain-distort", "quiz-reveal", "chat-bubbles",
]

_MAX = {"title": 24, "beat_title": 12, "point": 14, "narration": 64,
        "quote": 24, "cta": 12, "hook_narration": 46, "outro_narration": 46}


def _clamp(s: str, n: int) -> str:
    """钳制长度；超长时优先在句读边界截断（不切词中间）。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    # 找最后一个句读边界；找不到再退回硬截
    for i in range(len(cut) - 1, max(2, len(cut) - 12) - 1, -1):
        if cut[i] in "。！？；，、：":
            return cut[: i + 1]
    return cut


def outline_json_schema(beats_n: int | None = None) -> dict:
    """beats_n：钉死层数（min=max），引导解码空间最小化——重型请求赶在网关死线前完成。"""
    bmin, bmax = (beats_n, beats_n) if beats_n else (2, 12)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "video_outline",
            "schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": _MAX["title"]},
                    "summary": {"type": "string", "maxLength": 40},
                    "hook": {
                        "type": "object",
                        "properties": {"narration": {"type": "string", "minLength": 10, "maxLength": _MAX["hook_narration"]}},
                        "required": ["narration"], "additionalProperties": False,
                    },
                    "beats": {
                        "type": "array", "minItems": bmin, "maxItems": bmax,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "minLength": 2, "maxLength": _MAX["beat_title"]},
                                "points": {"type": "array", "minItems": 2, "maxItems": 4,
                                           "items": {"type": "string", "minLength": 2, "maxLength": _MAX["point"]}},
                                "narration": {"type": "string", "minLength": 15, "maxLength": _MAX["narration"]},
                                "template_hint": {"type": "string", "enum": TEMPLATE_HINTS},
                            },
                            "required": ["title", "points", "narration"],
                            "additionalProperties": False,
                        },
                    },
                    "outro": {
                        "type": "object",
                        "properties": {
                            "quote": {"type": "string", "minLength": 4, "maxLength": _MAX["quote"]},
                            "cta": {"type": "string", "minLength": 2, "maxLength": _MAX["cta"]},
                            "narration": {"type": "string", "minLength": 10, "maxLength": _MAX["narration"]},
                        },
                        "required": ["quote", "cta", "narration"],
                        "additionalProperties": False,
                    },
                },
                "required": ["title", "summary", "hook", "beats", "outro"],
                "additionalProperties": False,
            },
            "strict": False,
        },
    }


def beats_for_duration(duration_target_s: int) -> int:
    """目标时长 → 内容层数（hook/outro 另算两镜；单层旁白 ≤64 字 ≈ 18s）。"""
    return max(2, min(12, round((duration_target_s or 75) / 20)))


def _scaffold_outline(topic: str, duration_target_s: int) -> dict:
    """无 LLM 兜底：生成可编辑脚手架（诚实标注"草稿待改"）。"""
    n_beats = beats_for_duration(duration_target_s)
    frames = [
        ("是什么", "一句话定义与适用场景"),
        ("为什么重要", "它解决什么痛点、价值在哪"),
        ("怎么做", "关键步骤或使用要点"),
        ("注意什么", "常见误区与边界"),
    ][:n_beats]
    beats = []
    for i, (t, hint) in enumerate(frames):
        beats.append({
            "title": t,
            "points": [f"{topic}：要点{i + 1}", f"要点{i + 2}", "（编辑我）"],
            "narration": f"关于{topic}的第{i + 1}部分，{hint}。请在编辑大纲时把这一段旁白改写成你想说的话。",
            "template_hint": "bullet-list",
        })
    return {
        "title": _clamp(topic, _MAX["title"]),
        "summary": f"{topic} · 脚手架大纲（未配置 LLM，请在下方编辑）",
        "hook": {"narration": f"{topic}——用一分钟讲清楚它最关键的事。（编辑我）"},
        "beats": beats,
        "outro": {
            "quote": _clamp(f"记住关于{topic}的那句话", _MAX["quote"]),
            "cta": "现在就试试",
            "narration": f"这就是{topic}的核心。按照这些要点去做，你会看到差别。（编辑我）",
        },
    }


def generate_outline(topic: str, *, duration_target_s: int) -> tuple[str, dict]:
    """返回 (引擎, 大纲)。"""
    s = get_settings()
    LLMStoryboardEngine.__new__(LLMStoryboardEngine)  # 只复用 client 逻辑
    base = (s.llm_base_url or "").rstrip("/")
    if base and s.llm_model:
        try:
            outline = _llm_outline(base, s, topic, duration_target_s)
            _sanitize(outline)
            # 自愈防线：主模型偶发不守 schema（零 hint 静默退化）→ 最多自动重试 2 次
            beats = outline.get("beats") or []
            if beats and not any(b.get("template_hint") for b in beats):
                for attempt in (1, 2):
                    log.warning("LLM 大纲零 template_hint，自动重试 %d/2（防全 bullet-list 退化）", attempt)
                    try:
                        retry = _llm_outline(base, s, topic, duration_target_s)
                        _sanitize(retry)
                        if any(b.get("template_hint") for b in (retry.get("beats") or [])):
                            log.warning("第 %d 次重试拿到 hint，采用重试版大纲", attempt)
                            return "llm", retry
                    except Exception as e:
                        log.warning("零 hint 重试 %d 失败: %s", attempt, e)
                log.warning("重试 2 次仍零 hint，保留首版大纲")
            return "llm", outline
        except Exception as e:
            log.warning("LLM 扩写失败，回落脚手架: %s", e)
    return "scaffold", _scaffold_outline(topic, duration_target_s)


def _llm_outline(base: str, s, topic: str, duration_target_s: int) -> dict:
    import json

    from .llm import chat_with_fallback

    n_beats = beats_for_duration(duration_target_s)
    system = (
        f"你是短视频编导。把用户的主题扩写成一个约 {duration_target_s} 秒解说视频的大纲。"
        f"要求：hook 用反直觉或问题开场（旁白≤46字）；beats 是 {n_beats} 个推进层次，每层给出标题、"
        f"2-4 条要点（每条≤14字）和完整旁白草稿（口语化中文，"
        + ("目标时长较长，每层务必写满 45-64 字，要有细节、例子和因果推进，不许干瘪；"
           if duration_target_s >= 120 else "15-64字；层数多时内容层层推进不注水；")
        + "）；"
        "outro 给出点题金句和行动号召（旁白≤46字）。全程事实准确，不编造数据——没把握的具体统计一律用「约、数、近」等模糊表达，宁可少数字也不杜撰精确数；"
        "旁白明说「N 个/条/步」时，后续内容严格给 N 项；"
        "若用户主题本身是英文或明确要求英文，则标题/要点/旁白全部用英文输出（口语自然，不逐字翻译腔）；"
        "单一数字对比优先 big-stat；多组数据（3-4 个数值）优先 chart-bars（bars 用「标签|数值」格式）；"
        "讲机制/步骤链/数据流优先 diagram-flow（nodes 用「节点|小注」，3-5 个）；"
        "讲循环/飞轮/反馈回路优先 diagram-cycle（steps 顺时针「环节|小注」，3-5 个）；"
        "讲四象限/矩阵/权衡优先 diagram-quadrant（quadrants 按左上|右上|左下|右下「名称|小注」，x_label/y_label 为轴名）；"
        "讲中心概念多因素优先 diagram-hub（center ≤8字 + spokes「因素|小注」，4-5 个）；"
        "讲层次/等级/构成优先 diagram-pyramid（levels 从底到顶「层名|小注」，3-4 层）；"
        "讲神经网络/系统架构优先 diagram-layers（layer_labels 从左到右各层名称）；"
        "讲转化/筛选/层层淘汰优先 diagram-funnel（stages 从上到下「数量|阶段名」，3-4 层，数量递减）；"
        "讲两个概念的交集/共同点优先 diagram-venn（left_label/right_label ≤6字 + both_label 交集结论 ≤8字）；"
        "讲一分为N的分类/学派优先 diagram-tree（root ≤8字 + branches「分支|小注」，3-5 支）；"
        "讲时间序列趋势优先 chart-line（points 按时间「标签|数值」，3-6 点）；"
        "讲构成占比优先 chart-donut（parts「标签|百分比」，3-5 份合计≈100 + center_label 中心大字）；"
        "讲参数对照/费用明细优先 chart-table（columns 表头 2-3 列 + rows 单元格用|分隔）；"
        "要点+数字+金句混排的综合页优先 free-blocks（blocks 用「类型|内容」：list|要点1；要点2 · stat|数字|标签 · quote|金句 · tag|标签词）；"
        "目标时长≥150秒的长视频应在内容分节处（约每 4-5 层）插入 chapter-card 章节卡（part_no/part_total 两位数字 + chapter_title）；"
        "多维评测打分优先 diagram-radar（axes「维度|0-100」3-5 个）；单指标评分/指数/健康度优先 diagram-gauge（value_text + value_pct 0-100）；"
        "进阶路径/难度爬升优先 diagram-stairs（steps 从低到高「阶段|小注」3-4 级）；"
        "辟谣/反常识/「你以为…其实…」优先 myth-buster（myth 误区说法 + truth 真相说法）；"
        "复盘/成绩单式多数字并列优先 chart-kpi（cards 2-4 张「数值|标签|涨跌」）；"
        "一维两极分布（立场/口味/定位）优先 diagram-spectrum（left_label/right_label + items「概念|0-100位置」2-5 个）；"
        "FAQ/为什么体优先 qa-card（question 问句 + answer 答案）；"
        "行动自查/照着做优先 checklist（items「动作|方法」3-5 项）；"
        "利弊权衡/值不值/二选一优先 diagram-balance（left/right 概念+小注 + verdict 裁决）；"
        "方法论/心法/要素公式优先 formula-card（terms 2-4 个「要素|小注」+ operator 运算符 + result「结果|小注」）；"
        "「你看到的X vs 背后的Y」显隐性对比优先 diagram-iceberg（above_items 表象 1-2 条 + below_items 真相 2-4 条）；"
        "改造/优化/转变叙事优先 before-after（before_items 之前状态 + after_items 之后状态 2-3 条）；"
        "值不值得/要不要的权衡优先 pros-cons（pros 利 2-4 条 + cons 弊 2-4 条 + verdict 一句裁决）；"
        "冷知识连击用 facts-trio（facts 恰好 3 条，格式「大众认知|真相纠正」）；"
        "因果连锁/多米诺/蝴蝶效应优先 diagram-domino（tiles 3-6 环，从起因到结局）；"
        "层层剥开看本质优先 diagram-onion（layers 2-4 层从表象到内 + core 核心真相）；"
        "两种力量对抗拉扯优先 diagram-tug（left/right 力量名 + 各 1-3 条论据 + verdict 谁赢）；"
        "多角色接力协作优先 diagram-swimlane（lanes 2-3 条道，格式「角色|步骤|步骤」）；"
        "恶性循环vs良性循环对比优先 diagram-loop-duo（bad_loop/good_loop 各 3-4 步闭环）；"
        "层层扣减到终值（工资到手/成本拆解）优先 chart-waterfall（start_value 起点 + steps「项目|±数字」2-5 级 + end_label 终值栏）；"
        "我的一天/作息时间分配优先 diagram-daywheel（slices「活动|小时数|小注」3-6 段，小时数合计接近 24）；"
        "两个数字对比碾压时刻优先 stat-clash（left/right 主体名+数字 + verdict 裁决）；"
        "抽象大数字翻译成等价物优先 stat-equivalent（stat 大数字 + items「画面|小注」2-3 个相当于）；"
        "拆解某物的组成部分优先 diagram-callouts（subject 中央主体 + callouts「部位|说明」3-4 点）；"
        "坚持打卡/习惯战绩优先 habit-grid（done 完成 + total 总数 + streak 连续 + unit 单位 + verdict 战绩）；"
        "三句强观点连发（re-hook 节奏器）用 headline-barrage（statements 恰好 3 短句）；"
        "流程里找堵点/瓶颈诊断优先 diagram-bottleneck（nodes 3-5 环 + break_at 断在第几环 + cost 代价）；"
        "人群分布/你在哪个区间优先 chart-histogram（buckets「区间|相对高度」4-7 柱 + you_at 你在第几柱）；"
        "投票/二选一站队优先 chart-poll（left/right 选项名+百分比 + verdict 裁决）；"
        "评级/红黑榜/梯队分档优先 tier-list（tiers「档名|评语」3-4 档，S 档最神）；"
        "钱的去向/时间瓜分占比优先 chart-splitbar（segments「标签|占比%|小注」2-5 段 + total_label 总量）；"
        "做了实验/实测/亲测优先 experiment-log（hypothesis 猜想 + method 操作 + result 结果 + twist 反常识反转）；"
        "人物生平/一生故事优先 bio-timeline（person 人名 + milestones「年份|事件」3-5 段）；"
        "单一百分比的冲击时刻优先 stat-dotmatrix（pct 百分比 + item_label 指代 + detail 展开）；"
        "单一目标的进度追踪优先 stat-thermometer（current 当前 + goal 目标 + milestones 里程碑刻度 1-3）；"
        "三选一决策优先 compare-trio（options 恰好 3 条「名称|要点|小注」+ winner 胜者 + verdict）；"
        "多目标进度盘点优先 progress-bundle（items「项目|进度0-100|小注」3-4 条）；"
        "信息/任务分类归桶优先 sort-bins（source_label 一堆杂项 + bins「箱名|条目/条目」2-3 箱）；"
        "传话/谣言变味过程优先 chain-distort（steps 恰好 4 条「转述人|话」逐级变形）；"
        "猜猜看悬念钩子优先 quiz-reveal（question + options 恰好 3 项 + answer 正确项 + explain）；"
        "情景对话还原优先 chat-bubbles（messages 2-4 条「L|内容」或「R|内容」左右气泡）；"
        "TOP N/排行/最快最值得优先 rank-list（items 按名次「条目|小注」3-5 条）；"
        "多因归因/为什么出问题优先 diagram-fishbone（problem 鱼头 + bones「类别|原因」4-6 根）；"
        "阶段旅程/心理变化优先 diagram-journey（stages「阶段|事件|情绪0-100」3-5 段，情绪值必须给）；"
        "「A+B+C 构成 X」优先 diagram-blocks（parts 2-4 块「块名|小注」+ whole「整体|小注」）；"
        "两方观点对撞/甲乙方辩论优先 dialogue-quotes（left_speaker/right_speaker「名字|身份」+ 各自观点 + verdict 裁决）；"
        "讲人物/创始人/科学家优先 bio-card（name 大名 + title 头衔 + tags 2-3 个标签 + quote 金句）；"
        "资讯摘要/多条快讯优先 news-card（items 2-3 条「标题|来源|日期」）；"
        "双变量相关/城市产品定位优先 chart-scatter（x_label/y_label + points「名字|X|Y」0-100 各 3-6 个）；"
        "排期/日程/多轨道时间分配优先 diagram-gantt（time_labels 刻度 + tracks「轨道名|起始0-100|时长0-100」2-5 条）；"
        "定价/套餐/会员档位优先 price-compare（tiers 2-3 档「档名|价格|特性1；特性2」+ hot_index 推荐档）。只输出 JSON。"
    )
    user = f"主题：{topic}\n目标时长：约 {duration_target_s} 秒（约 {n_beats} 个内容层次）"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    def _parse(txt: str) -> dict:
        """宽容解析链：标准 → 容忍控制字符 → 剥代码栅栏 → 花括号切片。"""
        for attempt in (txt,):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass
            try:
                return json.loads(attempt, strict=False)   # 字符串内裸换行等
            except json.JSONDecodeError:
                pass
            fence = attempt
            if fence.strip().startswith("```"):
                fence = fence.strip().strip("`")
                if fence.startswith("json"):
                    fence = fence[4:]
                try:
                    return json.loads(fence, strict=False)
                except json.JSONDecodeError:
                    pass
            st, en = attempt.find("{"), attempt.rfind("}")
            if st >= 0 and en > st:
                return json.loads(attempt[st:en + 1], strict=False)
            raise ValueError("无法从模型输出中解析 JSON")

    # 两次尝试：json_schema 约束解码 → 网关不支持/输出损坏时降级 json_object
    last_err: Exception | None = None
    for fmt in (outline_json_schema(n_beats), {"type": "json_object"}):
        try:
            content, model = chat_with_fallback(
                messages, response_format=fmt,
                temperature=0.5, max_tokens=16000,
            )
        except Exception as e:
            last_err = e
            continue
        try:
            data = _parse(content)
            log.info("大纲由模型 %s 生成", model)
            return data
        except Exception as e:
            log.warning("大纲 JSON 解析失败（模型 %s）: %s", model, e)
            last_err = e
    raise RuntimeError(f"两次大纲生成都失败: {last_err}")


def _sanitize(outline: dict) -> None:
    """LLM 输出兜底裁剪：所有字段钳制到安全长度，模板提示收白名单。"""
    outline["title"] = _clamp(outline.get("title"), _MAX["title"])
    outline["summary"] = _clamp(outline.get("summary", ""), 40)
    outline.setdefault("hook", {})["narration"] = _clamp(
        (outline.get("hook") or {}).get("narration"), _MAX["hook_narration"])
    beats = outline.get("beats") or []
    hints_dropped = 0
    for b in beats:
        b["title"] = _clamp(b.get("title"), _MAX["beat_title"])
        b["points"] = [_clamp(p, _MAX["point"]) for p in (b.get("points") or [])][:4]
        b["narration"] = _clamp(b.get("narration"), _MAX["narration"])
        if b.get("template_hint") is not None and b["template_hint"] not in TEMPLATE_HINTS:
            log.warning("大纲 hint 非法已剔除: %r（不在 %d 个模板里）",
                        b["template_hint"], len(TEMPLATE_HINTS))
            hints_dropped += 1
            b.pop("template_hint", None)
    # 静默退化防线：LLM 大纲却零 hint = 大概率撞上不守 schema 的备胎模型，
    # 整条片会退化成 bullet-list——显式报警，方便运营决定是否重生成。
    if beats and not any(b.get("template_hint") for b in beats):
        log.warning("⚠ 大纲 %d 层全部无 template_hint（选图将全部退化为要点列表），"
                    "建议重新生成大纲", len(beats))
    outline["beats"] = beats[:12]
    outro = outline.setdefault("outro", {})
    outro["quote"] = _clamp(outro.get("quote"), _MAX["quote"])
    outro["cta"] = _clamp(outro.get("cta"), _MAX["cta"])
    outro["narration"] = _clamp(outro.get("narration"), _MAX["narration"])


# ── 大纲 → 场景 ──────────────────────────────────────────────

def outline_to_script(outline: dict) -> str:
    """大纲旁白串成完整文案（分镜引擎的输入）。"""
    parts = [outline["hook"]["narration"]]
    parts += [b["narration"] for b in outline.get("beats") or []]
    if outline.get("outro", {}).get("narration"):
        parts.append(outline["outro"]["narration"])
    return "\n".join(p for p in parts if p)


def _points_to_bars(points: list[str]) -> list[str]:
    """要点 → 「标签|数值」条目（复用 storyboard 的配对逻辑，按要点粒度）。"""
    from .storyboard import _chart_pairs

    return [f"{a}|{b}" for a, b in _chart_pairs(points)]


def scenes_from_outline(
    outline: dict, registry: TemplateRegistry,
) -> list[dict[str, Any]]:
    """大纲 → 场景序列（模板 + 字段 + 旁白），字段优先取大纲结构化内容，
    不足处用规则抽取器从旁白补。"""
    from .storyboard import _EXTRACTORS, _choose_body_template, apply_variant

    scenes: list[dict[str, Any]] = []

    # ① hook → title-hook
    hook_n = outline["hook"]["narration"]
    hook_fields = _EXTRACTORS["title-hook"](hook_n)
    hook_fields["headline"] = _clamp(outline.get("title") or hook_fields["headline"], 14)
    apply_variant("title-hook", hook_fields, hook_n)
    scenes.append({"template_id": "title-hook", "voice_text": hook_n,
                   "fields": hook_fields})

    # ② beats → 提示模板或启发式
    from .llm import DIAGRAM_TIDS, synthesize_fields

    for b in outline.get("beats") or []:
        narration = b["narration"]
        tid = b.get("template_hint") or _choose_body_template(narration)
        fields = None
        # 图解/图表类：LLM 从旁白合成字段（质量优于规则抽取）
        if tid in DIAGRAM_TIDS:
            try:
                fields = synthesize_fields(tid, narration, title=b.get("title") or "",
                                           points=b.get("points"), registry=registry)
            except Exception:  # noqa: BLE001
                fields = None
        if fields is None:
            try:
                fields = _EXTRACTORS[tid](narration)
            except Exception:
                tid, fields = "bullet-list", _EXTRACTORS["bullet-list"](narration)
        apply_variant(tid, fields, narration)
        # 大纲的结构化内容优先
        if b.get("title"):
            fields["headline"] = fields.get("headline") or _clamp(b["title"], 16)
            if fields.get("kicker") in ("", None) or fields["kicker"] == narration[:6]:
                fields["kicker"] = _clamp(b["title"], 16)
        if b.get("points") and "bullets" in fields:
            fields["bullets"] = [p for p in b["points"]][:5]
            while len(fields["bullets"]) < 3:
                fields["bullets"].append("……")
        # 图表模板：优先从要点里挑「含数字」的条目建 bars
        if tid == "chart-bars" and b.get("points"):
            bars = _points_to_bars(b["points"])
            if len(bars) >= 3:
                fields["bars"] = bars[:4]
                fields["note"] = _clamp(narration, 24)
        scenes.append({"template_id": tid, "voice_text": narration, "fields": fields})

    # ③ outro → quote-cta
    outro = outline.get("outro") or {}
    outro_n = outro.get("narration") or "记住今天的内容。"
    qf = _EXTRACTORS["quote-cta"](outro_n)
    if outro.get("quote"):
        qf["quote"] = outro["quote"]
    if outro.get("cta"):
        qf["cta"] = outro["cta"]
    apply_variant("quote-cta", qf, outro_n)
    scenes.append({"template_id": "quote-cta", "voice_text": outro_n, "fields": qf})

    return scenes
