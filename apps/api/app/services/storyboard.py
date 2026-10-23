"""分镜引擎：中文文案 → 场景序列（模板 + 字段 + 旁白）。

双路径：
  · rule（默认，确定性）：句子切分 + 贪心装桶 + 启发式模板选择 + 字段抽取
  · llm（可选，配置 LLM_BASE_URL 后优先）：结构化输出，失败自动回落 rule
两条路径产物都经模板 schema 强校验，不合规字段直接拒绝。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from .llm import DIAGRAM_TIDS, LLMStoryboardEngine
from .template_engine import TemplateRegistry, TemplateValidationError

log = logging.getLogger(__name__)

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_CLAUSE_SPLIT = re.compile(r"[，,。！？!?；;、：:\s]+")
_NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|×|倍|成|折|分之一)")
# 中文分数/倍数 → 数值（转成 big-stat 的 count-up 目标）
_CN_FRACTIONS = {
    "十分之一": ("0.1×", "1.0"),
    "十分之二": ("0.2×", "1.0"),
    "三分之一": ("0.33×", "1.0"),
    "一半": ("50%", "100%"),
    "九成": ("90%", "100%"),
    "翻倍": ("2×", "1"),
    "翻一番": ("2×", "1"),
    "翻两番": ("4×", "1"),
}


@dataclass
class StoryboardScene:
    template_id: str
    voice_text: str
    fields: dict[str, Any] = field(default_factory=dict)


def _sentences(text: str) -> list[str]:
    raw = [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip()]
    # 过短句合并
    out: list[str] = []
    for s in raw:
        if out and (len(out[-1]) + len(s) <= 24 or len(s) <= 6):
            out[-1] = out[-1] + s
        else:
            out.append(s)
    return out


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]


def _pick_clause(clauses: list[str], *, max_len: int, min_len: int = 2,
                 contains: str | None = None) -> str | None:
    """选一个长度合适的子句；contains 命中优先。"""
    hits = [c for c in clauses if contains and contains in c] if contains else []
    for pool in (hits, clauses):
        for c in pool:
            if min_len <= len(c) <= max_len:
                return c
    # 退化：截断最接近的
    for c in (hits or clauses):
        if len(c) >= min_len:
            return c[:max_len]
    return None


# ── 各模板的字段抽取器 ────────────────────────────────────────

def _fields_hook(text: str) -> dict:
    cl = _clauses(text)
    headline = _pick_clause(cl, max_len=14, min_len=2) or text[:10]
    subline = _pick_clause(cl, max_len=24, min_len=4) or text[:20]
    if subline == headline:
        subline = text[:24]
    return {
        "kicker": (cl[0] or text)[:8] if cl else text[:8],
        "headline": headline[:14],
        "subline": subline[:24],
    }


def _fields_compare(text: str) -> dict:
    cl = _clauses(text)
    left = _pick_clause(cl, max_len=8, min_len=2) or "一方"
    right = _pick_clause([c for c in cl if c != left], max_len=8, min_len=2) or "另一方"
    pts = [c for c in cl if c not in (left, right) and len(c) <= 14]
    lp = [p[:14] for p in pts[:3]]
    rp = [p[:14] for p in pts[3:6]]
    # 不足 2 条时用破折号占位（schema minLength=2）
    lp = (lp + ["——"] * 3)[:3]
    rp = (rp + ["——"] * 3)[:3]
    return {
        "kicker": text[:6] or "对比",
        "headline": (left + " 与 " + right)[:16],
        "left_title": left[:8],
        "left_points": lp,
        "right_title": right[:8],
        "right_points": rp,
        "verdict": (_pick_clause(cl, max_len=18) or text)[:18],
    }


def _chart_pairs(clause_list: list[str]) -> list[tuple[str, str]]:
    """子句 → 「标签|数值」对。数值须带单位/百分号或非年份短数字；
    标签优先取数值后方中文（剥连接词/单位），否则取前方。"""
    pairs: list[tuple[str, str]] = []
    strip_chars = " ，。、；：的了是在约平均每周高达个时间花用精力"
    units = "%|％|倍|小时|h|分钟|天|秒|ms|次|人|条|篇|万字|字|万|亿"
    for cl in clause_list:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(" + units + ")", cl)
        if m:
            value = m.group(1) + m.group(2)
        else:
            m = re.search(r"(?<![0-9.])([0-9]{1,3}(?:\.[0-9]+)?)(?![0-9])", cl)
            if not m or len(m.group(1)) == 4:  # 无单位四位数字 ≈ 年份
                continue
            value = m.group(1)
        after = cl[m.end():].strip(strip_chars)
        before = cl[:m.start()].strip(strip_chars)
        label = after if re.search(r"[\u4e00-\u9fff]", after) else before
        # 「维度70分」句式：后方只剩「分/得分」→ 标签取前方的维度名
        if label in ("分", "得分", "分值", "评分") and re.search(r"[\u4e00-\u9fff]", before):
            label = before
        if not re.search(r"[\u4e00-\u9fff]", label):
            continue
        pairs.append((label[:8], value))
    # 去重（同标签只留第一个）
    seen: set[str] = set()
    out = []
    for a, b in pairs:
        if a not in seen:
            seen.add(a)
            out.append((a, b))
    return out


def _fields_tty(text: str) -> dict:
    """终端演示：抓命令（反引号/引号内或 $ 后的片段）与输出行。"""
    m = re.search(r"[`\u201c\u2018\"']([\w\-./= :@]+)[`\u201d\u2019\"']", text) \
        or re.search(r"\$\s*([\w\-./= :@]+)", text) \
        or re.search(r"((?:git|npm|pip|docker|curl|python|node|cargo|grep|make)\s+[\w\-./= :@]+)", text)
    if not m:
        raise _NoFit("没找到可演示的命令")
    command = m.group(1).strip()[:48]
    coarse = [c.strip() for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    # 命令所在句的位置 → 输出行只取其后的句子
    cmd_pos = next((i for i, c in enumerate(coarse) if command[:8] in c), 0)
    outs = [c[:34] for c in coarse[cmd_pos + 1:]
            if len(c) >= 3 and re.search(r"[\u4e00-\u9fff]", c)][:4]
    if not outs:
        outs = [c[:34] for c in coarse if len(c) >= 2][:3]
    while len(outs) < 2:
        outs.append("done ✓")
    return {
        "kicker": (coarse[0] or text)[:8] if coarse else text[:8],
        "headline": next((c[:14] for c in coarse[1:] if len(c) >= 3),
                         (coarse[0] if coarse else text)[:14]),
        "command": command,
        "output_lines": outs,
        "note": "",
    }


def _pair_nodes(text: str, *, min_n: int, max_n: int) -> list[str]:
    """子句 → 「标签|小注」节点列表；不足 min_n 抛 _NoFit。"""
    cl = [c for c in _clauses(text) if len(c) >= 2]
    # 残片过滤：连接词开头 / 助词结尾的句子不适合当图解节点
    def _is_fragment(c: str) -> bool:
        return bool(re.match(r"^(是|而|的|了|让|使|又|也|还|但|就|才|都)", c)) or c[-1] in "的了"
    nodes = []
    for c in cl[:max_n]:
        if _is_fragment(c):
            continue
        if len(c) <= 8:
            nodes.append(c)
        else:
            cut = -1
            for k in range(min(8, len(c)) - 1, 1, -1):
                if c[k] in "，、；, " or c[k - 1] in "的了是在和与":
                    cut = k
                    break
            if cut <= 0:
                continue
            label = c[:cut].strip("，、；, ")
            sub = c[cut:].strip("，、；, ")[:12]
            nodes.append(f"{label}|{sub}" if sub else label)
    if len(nodes) < min_n:
        raise _NoFit(f"可画节点不足 {min_n} 个")
    return nodes[:max_n]


def _fields_cycle(text: str) -> dict:
    nodes = _pair_nodes(text, min_n=3, max_n=5)
    cl = _clauses(text)
    return {"kicker": (cl[0] or text)[:8] if cl else text[:8],
            "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
            "steps": nodes, "note": ""}


def _fields_quadrant(text: str) -> dict:
    nodes = _pair_nodes(text, min_n=4, max_n=4)
    cl = _clauses(text)
    return {"kicker": (cl[0] or text)[:8] if cl else text[:8],
            "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
            "x_label": "紧急 →" if "紧急" in text else "程度 →",
            "y_label": "重要 ↑" if "重要" in text else "价值 ↑",
            "quadrants": nodes, "note": ""}


def _fields_hub(text: str) -> dict:
    nodes = _pair_nodes(text, min_n=4, max_n=5)
    cl = _clauses(text)
    center = (_pick_clause(cl, max_len=6, min_len=2) or text[:6])[:6]
    return {"kicker": (cl[0] or text)[:8] if cl else text[:8],
            "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
            "center": center, "spokes": nodes, "note": ""}


def _fields_pyramid(text: str) -> dict:
    nodes = _pair_nodes(text, min_n=3, max_n=4)
    cl = _clauses(text)
    return {"kicker": (cl[0] or text)[:8] if cl else text[:8],
            "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
            "levels": nodes, "note": ""}


def _fields_flow(text: str) -> dict:
    """流程图解：子句 → 节点「标签|小注」；不足 3 个 → NoFit 回落 bullet-list。"""
    cl = [c for c in _clauses(text) if len(c) >= 2]
    nodes = []
    for c in cl[:5]:
        if len(c) <= 8:
            nodes.append(c)
        else:
            cut = -1
            for k in range(min(8, len(c)) - 1, 1, -1):
                if c[k] in "，、；, " or c[k - 1] in "的了是在和与":
                    cut = k
                    break
            if cut <= 0:
                continue
            label = c[:cut].strip("，、；, ")
            sub = c[cut:].strip("，、；, ")[:12]
            nodes.append(f"{label}|{sub}" if sub else label)
    if len(nodes) < 3:
        raise _NoFit("可画节点不足 3 个")
    return {
        "kicker": (cl[0] or text)[:8] if cl else text[:8],
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "nodes": nodes[:5],
        "note": "",
    }


def _fields_layers(text: str) -> dict:
    """网络层级图：hint 驱动（LLM 明确要画层级时），标签取标准四层，注释放正文。"""
    cl = _clauses(text)
    note = next((c for c in cl if 4 <= len(c) <= 20), "")
    return {
        "kicker": (cl[0] or text)[:8] if cl else text[:8],
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "layer_labels": ["输入层", "隐藏层", "隐藏层", "输出层"],
        "flow_note": "深度 = 隐藏层数量",
        "note": note[:20],
    }


def _fields_chart(text: str) -> dict:
    """数据条形图：抓「标签+数值」对（≥3 组成图，否则 _NoFit 回落）。
    注意用粗粒度句读切分——_clauses 会把数字与标签拆散。"""
    coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    pairs = _chart_pairs(coarse)
    if len(pairs) < 3:
        raise _NoFit("数据对不足 3 组")
    pairs = pairs[:4]
    cl = _clauses(text)
    return {
        "kicker": (cl[0] or text)[:8] if cl else text[:8],
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "bars": [f"{a}|{b}" for a, b in pairs],
        "note": text[:24],
    }


def _fields_bullets(text: str) -> dict:
    cl = [c for c in _clauses(text) if len(c) >= 2]
    items = [c[:18] for c in cl[:5]] or [text[:18]]
    while len(items) < 3:
        items.append("……")
    hot = min(1, len(items) - 1)
    return {
        "kicker": text[:6] or "要点",
        "headline": (_pick_clause(cl, max_len=16) or text[:12])[:16],
        "bullets": items[:5],
        "highlight_index": hot,
    }


def _fields_stat(text: str) -> dict:
    cl = _clauses(text)
    num = None
    stat_from = None
    # 优先：显式数字 + 单位
    m = _NUM_UNIT.search(text)
    if m:
        num = m.group(1) + m.group(2)
        others = [x.group(1) + (x.group(2) or "") for x in _NUM_UNIT.finditer(text)]
        stat_from = others[0].rstrip("%×倍") if others else "0"
        if stat_from == num:
            stat_from = "0"
    else:
        # 其次：中文分数/倍数表达
        for phrase, (val, frm) in _CN_FRACTIONS.items():
            if phrase in text:
                num = val
                stat_from = frm
                break
    if not num:
        # 没有可放大的数字：退化成要点模板（调用方处理）
        raise _NoFit()
    return {
        "kicker": text[:6] or "数据",
        "stat_value": num[:10],
        "stat_unit": "VALUE",
        "stat_from": re.sub(r"[^\d.]", "", stat_from) or "0",
        "headline": (_pick_clause(cl, max_len=20, contains=num) or text[:16])[:20],
        "note": (cl[-1] if cl else text)[:26],
    }


class _NoFit(Exception):
    pass


def _fields_steps(text: str) -> dict:
    """流程步骤：label 须在自然边界截断；长句硬切会烂词 → NoFit 回落 bullet-list。"""
    cl = [c for c in _clauses(text) if len(c) >= 2]
    steps = []
    i = 0
    while i < len(cl) and len(steps) < 5:
        c = cl[i]
        nxt = cl[i + 1] if i + 1 < len(cl) else ""
        if len(c) <= 4 and nxt:
            # 短前导（第一步/首先）+ 内容句 → label=前导 detail=内容
            steps.append({"label": c, "detail": nxt[:14]})
            i += 2
            continue
        if len(c) <= 8:
            steps.append({"label": c, "detail": c[:14]})
            i += 1
            continue
        # 长句：8 字内找自然边界，找不到就整段放弃（NoFit 回落 bullet-list）
        cut = -1
        for k in range(min(8, len(c)) - 1, 1, -1):
            if c[k] in "，、；, " or c[k - 1] in "的了是在和与":
                cut = k
                break
        if cut <= 0:
            raise _NoFit("步骤句过长，无法干净切 label")
        steps.append({"label": c[:cut].strip("，、；, "), "detail": c[:14]})
        i += 1
    if len(steps) < 3:
        raise _NoFit("可用步骤不足 3 步")
    return {
        "kicker": text[:6] or "流程",
        "headline": (_pick_clause(cl, max_len=16) or text[:12])[:16],
        "steps": steps[:5],
    }


def _fields_term(text: str) -> dict:
    cl = _clauses(text)
    term = _pick_clause(cl, max_len=10, min_len=1) or text[:8]
    definition = next((c for c in cl if len(c) >= 8 and c != term), text)[:44]
    return {
        "kicker": text[:6] or "概念",
        "term": term[:10],
        "term_en": "CONCEPT",
        "definition": definition,
        "tag": "DEFINITION"[:12],
    }


def _fields_timeline(text: str) -> dict:
    cl = [c for c in _clauses(text) if len(c) >= 2]
    events = []
    for i, c in enumerate(cl[:4]):
        parts = c.split("：", 1) or c.split(":", 1)
        label = parts[0][:8] if parts[0] else f"阶段{i + 1}"
        tx = parts[1][:18] if len(parts) > 1 else c[:18]
        events.append({"label": label, "text": tx})
    while len(events) < 3:
        events.append({"label": "后续", "text": "……"})
    return {
        "kicker": text[:6] or "时间线",
        "headline": (_pick_clause(cl, max_len=16) or text[:12])[:16],
        "events": events[:4],
    }


def _fields_quote(text: str) -> dict:
    cl = _clauses(text)
    quote = _pick_clause(cl, max_len=32, min_len=4) or text[:30]
    cta = _pick_clause([c for c in cl if c != quote], max_len=14, min_len=2) or text[:14]
    return {
        "kicker": "一句话记住"[:16],
        "quote": quote[:32],
        "cta": cta[:14],
        "footnote": "LLM2VIDEO · EXPLAINER"[:22],
    }


# ── 模板选择启发式 ──────────────────────────────────────────

_TTY_HINT = re.compile(r"(?:git|npm|pip|docker|kubectl|curl|python3?|node|cargo|grep|make)\s+[\w.-]+|[$`]\s*[\w.-]+\s|命令行|终端里|敲个命令|一行命令|跑一下|执行")

_CHART_PAIR = re.compile(
    r"[^，。；]{2,6}[是达为有占花用仅才]{0,1}\s*[0-9]+(\.[0-9]+)?\s*[%％倍小时分钟天秒ms次人条篇]")

_STEP_HINT = re.compile(r"首先|然后|其次|接着|最后|步骤|流程|阶段|第一步|再|接着")
_TERM_HINT = re.compile(r"是指|指的是|叫做|定义为|所谓|定义是")
_CMP_HINT = re.compile(r"而|相比|但是| versus |vs|对比|不同|区别|前者|后者")
_FUNNEL_HINT = re.compile(r"转化率|漏斗|层层筛选|层层淘汰|从\d+个|最终只剩|幸存")
_VENN_HINT = re.compile(r"交集|重叠|共同点|既是.{1,8}又是|重合|交叉地带")
_TREE_HINT = re.compile(r"分为|分成|三种类型|三大类|三大学派|四类|五类|分类学|一分为")

_TL_HINT = re.compile(r"\d{4}\s*年|开始|后来|最终|起初|早期|后来|直到|第一阶段|上线后")


def _choose_body_template(text: str) -> str:
    # 大数字：显式数字/中文分数表达，且能成功抽取字段
    if _NUM_UNIT.search(text) or any(p in text for p in _CN_FRACTIONS):
        try:
            _fields_stat(text)
            return "big-stat"
        except _NoFit:
            pass
    # 命令/工具演示 → 终端
    if _TTY_HINT.search(text):
        try:
            _fields_tty(text)
            return "term-demo"
        except _NoFit:
            pass
    # 多组「标签+数值」→ 条形图（单一数字优先 big-stat，已在上面处理）
    if _CHART_PAIR.search(text):
        try:
            _fields_chart(text)
            coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
            pairs = _chart_pairs(coarse)
            nums = []
            for _k, v in pairs:
                m2 = re.match(r"([0-9]+(?:\.[0-9]+)?)", v)
                if m2:
                    nums.append(float(m2.group(1)))
            if len(nums) >= 3 and all(nums[i] > nums[i + 1] for i in range(len(nums) - 1)):
                return "diagram-funnel"
            if sum(1 for k, _v in pairs if _TEMPORAL.search(k)) >= 2:
                return "chart-line"
            if sum(1 for _k, v in pairs if "%" in v or "％" in v) >= 3:
                return "chart-donut"
            return "chart-bars"
        except _NoFit:
            pass
    if _STEP_HINT.search(text):
        return "process-steps"
    # 交集 / 树状分类 / 漏斗（字段合成优先 LLM，规则 NoFit 由调用方兜底）
    if _VENN_HINT.search(text):
        return "diagram-venn"
    # 对话 / 快讯 / 甘特 / 定价
    if re.search(r"有人说|网友[说说议]|甲方.{0,12}乙方|正方.{0,10}反方|支持者.{0,10}反对", text):
        return "dialogue-quotes"
    if re.search(r"快讯|本周.{0,4}(大事|动态)|今日.{0,3}条|行业动态", text):
        return "news-card"
    if re.search(r"甘特|排期|日程表|时间表|我的一天怎么", text):
        return "diagram-gantt"
    if re.search(r"定价|多少钱一|套餐|免费版|专业版|会员档", text):
        return "price-compare"
    # ── 第 6 批语义位 ──
    if re.search(r"改造前|前后对比|爆改|改造之后|换了之后|之前之后|翻新", text):
        return "before-after"
    if re.search(r"值不值得|要不要|划不划算|利弊|优缺点|好处和坏处| pros ?cons", text, re.I):
        return "pros-cons"
    if re.search(r"冷知识|反直觉|反常识|你不知道的|三个颠覆", text):
        return "facts-trio"
    if re.search(r"多米诺|连锁反应|一环扣一环|蝴蝶效应|骨牌", text):
        return "diagram-domino"
    if re.search(r"剥开|表象之下|层层递进|洋葱|往里层|本质其实是", text):
        return "diagram-onion"
    if re.search(r"拔河|拉扯|两个声音|两个小人|在打架|博弈|较量", text):
        return "diagram-tug"
    if re.search(r"泳道|接力|交接|怎么配合|怎么协作|谁负责|经过谁的手", text):
        return "diagram-swimlane"
    if re.search(r"恶性循环|良性循环|两个循环|越陷越深|滚雪球", text):
        return "diagram-loop-duo"
    # ── 第 7 批语义位 ──
    if re.search(r"工资到手|到手多少|层层扣|扣完|成本怎么拆|扣税|五险一金", text):
        return "chart-waterfall"
    if re.search(r"我的一天|一天怎么过|24\s?小时|作息|时间都花在", text):
        return "diagram-daywheel"
    if re.search(r"是.{0,8}的.{0,4}倍|相差多少|多了多少倍|碾压|吊打", text):
        return "stat-clash"
    if re.search(r"到底有多[大少长]|相当于|换算一下|等价于", text):
        return "stat-equivalent"
    if re.search(r"拆解|拆开看|解剖|组成部分|配置单|值在哪", text):
        return "diagram-callouts"
    if re.search(r"打卡|连续.{0,4}天|坚持了.{0,4}天|习惯养成|完成率", text):
        return "habit-grid"
    if re.search(r"三句话|记住这.{0,3}句|划重点|三连击", text):
        return "headline-barrage"
    if re.search(r"卡在哪|堵在|瓶颈|哪里断了|卡点|拖后腿", text):
        return "diagram-bottleneck"
    # ── 第 8 批语义位 ──
    if re.search(r"分布|正态|长尾|你在哪个区间|大部分人都|人群里", text):
        return "chart-histogram"
    if re.search(r"多少人选|你选哪个|投票|二选一|站哪边|站队", text):
        return "chart-poll"
    if re.search(r"分级|段位|梯队|红黑榜|评级|S级|榜单分档", text):
        return "tier-list"
    if re.search(r"钱花在哪|花在哪了|怎么瓜分|分成几份|占比多少", text):
        return "chart-splitbar"
    if re.search(r"我们做了个实验|实测|亲测|试了试|做了实验", text):
        return "experiment-log"
    if re.search(r"一生|生平|他的故事|传奇|从.*到.*逆袭", text):
        return "bio-timeline"
    # ── 第 9 批语义位 ──
    if re.search(r"分之一|一半.{0,4}(人生|时间|工资)|百分之\d+", text):
        return "stat-dotmatrix"
    if re.search(r"存钱进度|攒到|目标还差|还剩多少到目标", text):
        return "stat-thermometer"
    if re.search(r"三种方案|三个选|三选一|三路|哪个更划算", text):
        return "compare-trio"
    if re.search(r"几个习惯|进度盘点|进度一览|多目标|各有进度", text):
        return "progress-bundle"
    if re.search(r"怎么分类|分桶|归到哪|收纳|分成三类", text):
        return "sort-bins"
    if re.search(r"传话|以讹传讹|越传越|变味|谣言怎么长", text):
        return "chain-distort"
    if re.search(r"猜猜|哪个是真的|考考你|你能答对|来测一测", text):
        return "quiz-reveal"
    if re.search(r"聊天记录|微信对话|当我说|老板说|爸妈的对话", text):
        return "chat-bubbles"
    # ── 第 10 批视觉装置位 ──
    if re.search(r"换个词|一词之差|只改一个词|这个词改成", text):
        return "word-slot"
    if re.search(r"账单|小票|合计|这笔账|消费记录|结账", text):
        return "receipt-strip"
    if re.search(r"车票|登机|出发到|开往|启程|下一站", text):
        return "ticket-stub"
    if re.search(r"成分表|成分检测|含量|配比检测|成分分析", text):
        return "nutrition-label"
    if re.search(r"命令行|终端|报错|exit|debug|诊断一下", text, re.I):
        return "terminal-window"
    # ── 第 12 批（思维导图/轨道/三角/秩变/气泡/面积/单据/证书/便签/卡牌）──
    if re.search(r"思维导图|发散|拆解成|分成几块|要素拆", text):
        return "diagram-mindmap"
    if re.search(r"生态系统|围绕|外围|轨道|能力圈|一圈", text):
        return "diagram-orbit"
    if re.search(r"三角|三难|不可能三角|只能选两|三者只能", text):
        return "diagram-triangle"
    if re.search(r"排名变化|洗牌|反超|名次|谁上谁下|掉队", text):
        return "chart-bump"
    if re.search(r"气泡图|客单价|复购率|三维对比|人群气泡", text):
        return "chart-bubble"
    if re.search(r"增长曲线|累积|总面积|总量趋势|面积图", text):
        return "chart-area"
    if re.search(r"工资条|到手多少|实发|五险一金|扣完", text):
        return "payslip-stub"
    if re.search(r"发票|开票|报销|费用明细单", text):
        return "invoice-sheet"
    if re.search(r"登机牌|起飞|单程|落地.{0,6}转机", text):
        return "boarding-pass"
    if re.search(r"证书|奖状|认证|荣誉|颁发|达标", text):
        return "certificate-award"
    if re.search(r"便利贴|便签|头脑风暴|贴满|灵感墙", text):
        return "sticky-notes"
    if re.search(r"卡牌|属性卡|稀有度|抽卡|能力值|这张牌", text):
        return "trading-card"
    if re.search(r"翻牌|登机口|航班|终点站|人生航站", text):
        return "split-flap"
    if re.search(r"主线任务|打怪|升级|任务面板|经验值", text):
        return "game-quest"
    if re.search(r"四格|漫画|吐槽剧场|名场面还原", text):
        return "comic-panel"
    # ── 第 11 批行业单据位 ──
    if re.search(r"K线|看盘|涨跌|大盘|基金净值|行情", text):
        return "finance-candle"
    if re.search(r"体检|化验单|血常规|指标偏高|参考范围", text):
        return "medical-report"
    if re.search(r"合同|条款|违约|押金不退|协议陷阱", text):
        return "legal-clause"
    if re.search(r"成绩单|期末成绩|放榜|出分了", text):
        return "grade-card"
    if re.search(r"简历|求职|面试经历|工作经历怎么写", text):
        return "resume-card"
    if re.search(r"行程单|旅行攻略|几日游|Day1|路线怎么排", text, re.I):
        return "itinerary-card"
    if re.search(r"快递|物流|运单|包裹到哪|派送", text):
        return "waybill-track"
    if re.search(r"服务器|宕机|告警|线上事故|值班", text):
        return "monitor-dash"
    if re.search(r"菜单|点菜|招牌菜|探店必点", text):
        return "menu-board"
    if re.search(r"户型|两室一厅|平面图|房间布局|看房", text):
        return "floor-plan"
    # 公式卡 / 冰山 / 排行榜
    if re.search(r"公式|心法|法门|成功[的方程式]*=", text):
        return "formula-card"
    if re.search(r"你看到|表面[上是]?.{0,8}(背后|其实|真实)|冰山|看不见的", text):
        return "diagram-iceberg"
    if re.search(r"排行|榜单|前三|前五|十大|Top\s?\d", text, re.I):
        return "rank-list"
    # 误区粉碎：「以为/认为…其实/真相」
    if re.search(r"(?:以为|认为|觉得|都说).{2,20}(?:其实|实际上|真相)", text):
        return "myth-buster"
    # 问答卡：有问句且有明确答语标记
    if re.search(r"[^。！？]{6,40}[？?][^。！？]{0,10}(?:因为|答案是|其实是)", text):
        return "qa-card"
    if _TREE_HINT.search(text) and len(_clauses(text)) >= 3:
        return "diagram-tree"
    if _FUNNEL_HINT.search(text):
        return "diagram-funnel"
    if _TERM_HINT.search(text):
        return "term-explainer"
    if _TL_HINT.search(text) and len(_clauses(text)) >= 3:
        return "timeline-events"
    # 对比：提示词 或 分号两侧的正反结构（"没命中…；命中…"）
    cl = _clauses(text)
    if (_CMP_HINT.search(text) and len(cl) >= 3) or (
        "；" in text and re.search(r"没|不|未", text) and re.search(r"命中|需要|必须|得", text)
    ):
        return "concept-compare"
    return "bullet-list"


# ── 版式变体：高频模板的可选 variant 字段，缺省按旁白哈希播种 ──
_VARIANT_MAX = {"bullet-list": 2, "title-hook": 2, "big-stat": 1,
                "quote-cta": 1, "free-blocks": 3}
_VARIANT_KEY = {"free-blocks": "layout"}


def apply_variant(tid: str, fields: dict, narration: str) -> dict:
    """给支持变体的模板播种版式/布局字段（已手动指定则不动）。"""
    vmax = _VARIANT_MAX.get(tid)
    if vmax is None:
        return fields
    key = _VARIANT_KEY.get(tid, "variant")
    if fields.get(key) is not None:
        return fields
    import hashlib as _h
    fields[key] = _h.sha1(narration.encode()).digest()[0] % (vmax + 1)
    return fields


_TEMPORAL = re.compile(r"\d{4}\s*年|\d+\s*月|季度|第[一二三四五六七八九十N]+|早年|后期")


def _fields_funnel(text: str) -> dict:
    """漏斗：数字递减的「数量|阶段」对（≥3 层）。"""
    coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    pairs = _chart_pairs(coarse)
    if len(pairs) < 3:
        raise _NoFit("漏斗层数不足")
    stages = [f"{v}|{k}" for k, v in pairs[:4]]
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "转化",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "stages": stages,
        "note": text[:24],
    }


def _fields_venn(text: str) -> dict:
    """交集：「既是A又是B」/「A 与 B 的交集」结构。"""
    m = (re.search(r"既(?:是|要)([\u4e00-\u9fff]{1,6})[，,、]?又(?:是|要)([\u4e00-\u9fff]{1,6})", text)
         or re.search(r"([\u4e00-\u9fff]{1,6})\s*(?:与|和|×|跟)\s*([\u4e00-\u9fff]{1,6})[^。！？]{0,6}(?:交集|重叠|共同|重合)", text))
    if not m:
        raise _NoFit("没找到两元交集结构")
    cl = _clauses(text)
    both = (_pick_clause([c for c in cl if re.search(r"交集|共同|重合|答案", c)], max_len=8)
            or _pick_clause(cl[1:], max_len=8) or "黄金交叉")[:8]
    return {
        "kicker": text[:6] or "交集",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "left_label": m.group(1)[:6],
        "right_label": m.group(2)[:6],
        "both_label": both,
        "note": text[:24],
    }


def _fields_tree(text: str) -> dict:
    """树状：根取标题短语，分支用节点对（3-5 支）。"""
    branches = _pair_nodes(text, min_n=3, max_n=5)
    cl = _clauses(text)
    root = (_pick_clause(cl[:1], max_len=8) or text[:8])[:8]
    return {
        "kicker": text[:6] or "分类",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "root": root,
        "branches": branches,
        "note": text[:24],
    }


def _fields_line(text: str) -> dict:
    """折线：时序标签的「标签|数值」对（≥3 点）。"""
    coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    pairs = _chart_pairs(coarse)
    temporal = [(k, v) for k, v in pairs if _TEMPORAL.search(k)]
    if len(temporal) < 3:
        raise _NoFit("时序数据点不足")
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "趋势",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "series_label": (_pick_clause(cl, max_len=6) or "数值")[:8],
        "points": [f"{k}|{v}" for k, v in temporal[:6]],
        "note": text[:24],
    }


def _fields_donut(text: str) -> dict:
    """占比环：百分比「标签|数值」对（≥3 份且合计接近 100）。"""
    coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    pairs = _chart_pairs(coarse)
    pct = [(k, v) for k, v in pairs if "%" in v or "％" in v]
    if len(pct) < 3:
        raise _NoFit("占比数据不足")
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "构成",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:14],
        "parts": [f"{k}|{v}" for k, v in pct[:5]],
        "note": text[:24],
    }


def _fields_dialogue(text: str) -> dict:
    """对话卡：两方立场语义，交给 LLM 合成。"""
    raise _NoFit("两方立场交给 LLM 合成")


def _fields_bio(text: str) -> dict:
    """人物卡：人物语义，交给 LLM 合成。"""
    raise _NoFit("人物信息交给 LLM 合成")


def _fields_news(text: str) -> dict:
    """快讯组：子句 → 标题|来源。"""
    cl = [c for c in _clauses(text) if len(c) >= 4]
    if len(cl) < 2:
        raise _NoFit("快讯不足 2 条")
    items = [c[:26] for c in cl[:3]]
    return {
        "kicker": text[:6] or "快讯",
        "headline": (_pick_clause(cl, max_len=14) or text[:14])[:16],
        "items": items,
        "note": text[:24],
    }


def _fields_scatter(text: str) -> dict:
    """散点：双变量语义，交给 LLM 合成。"""
    raise _NoFit("双变量定位交给 LLM 合成")


def _fields_gantt(text: str) -> dict:
    """甘特：起止百分比语义，交给 LLM 合成。"""
    raise _NoFit("排期轨道交给 LLM 合成")


def _fields_price(text: str) -> dict:
    """定价卡：档位语义，交给 LLM 合成。"""
    raise _NoFit("定价档位交给 LLM 合成")


def _fields_formula(text: str) -> dict:
    """公式卡：构成语义太结构化，交给 LLM 合成。"""
    raise _NoFit("公式结构交给 LLM 合成")


def _fields_iceberg(text: str) -> dict:
    """冰山图：显性/隐性分层，交给 LLM 合成。"""
    raise _NoFit("显性/隐性分层交给 LLM 合成")


def _fields_batch6(text: str) -> dict:
    """第 6 批语义位（前后/利弊/三连/多米诺/洋葱/拔河/泳道/双环）：结构复杂，交给 LLM 合成。"""
    raise _NoFit("第 6 批结构化字段交给 LLM 合成")


def _fields_batch7(text: str) -> dict:
    """第 7 批语义位（瀑布/日轮/对撞/具象/解剖/打卡/三连砸/卡点）：交给 LLM 合成。"""
    raise _NoFit("第 7 批结构化字段交给 LLM 合成")


def _fields_batch8(text: str) -> dict:
    """第 8 批语义位（直方/民意/分级/分割条/实验/生平）：交给 LLM 合成。"""
    raise _NoFit("第 8 批结构化字段交给 LLM 合成")


def _fields_batch9(text: str) -> dict:
    """第 9 批语义位（点阵/温度计/三对比/多进度/分桶/传话/猜猜看/气泡）：交给 LLM 合成。"""
    raise _NoFit("第 9 批结构化字段交给 LLM 合成")


def _fields_batch10(text: str) -> dict:
    """第 10 批视觉装置位（换词/小票/车票/成分表/终端/翻牌/任务面板/四格）：交给 LLM 合成。"""
    raise _NoFit("第 10 批结构化字段交给 LLM 合成")


def _fields_batch11(text: str) -> dict:
    """第 11 批行业单据位（K线/体检/合同/成绩单/简历/行程/快递/监控/菜单/户型）：交给 LLM 合成。"""
    raise _NoFit("第 11 批结构化字段交给 LLM 合成")


def _fields_batch12(text: str) -> dict:
    """第 12 批（导图/轨道/三角/秩变/气泡/面积/工资条/发票/登机牌/证书/便签/卡牌）：交给 LLM 合成。"""
    raise _NoFit("第 12 批结构化字段交给 LLM 合成")


def _fields_rank(text: str) -> dict:
    """排行榜：名次节点对（3-5 条）。"""
    items = _pair_nodes(text, min_n=3, max_n=5)
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "排行",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "items": items,
        "note": text[:24],
    }


def _fields_fishbone(text: str) -> dict:
    """鱼骨图：问题+多因结构，交给 LLM 合成。"""
    raise _NoFit("归因结构交给 LLM 合成")


def _fields_journey(text: str) -> dict:
    """用户旅程：阶段节点对（3-5 段，情绪交给 LLM）。"""
    stages = _pair_nodes(text, min_n=3, max_n=5)
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "旅程",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "stages": stages,
        "note": text[:24],
    }


def _fields_blocks(text: str) -> dict:
    """拼图构成：整体/部分语义，交给 LLM 合成。"""
    raise _NoFit("构成结构交给 LLM 合成")


def _fields_myth(text: str) -> dict:
    """误区粉碎：「你以为X…其实Y」结构。"""
    m = (re.search(r"(?:以为|以为?是|认为|觉得|都说|传言)(.{4,26}?)[，。；].{0,8}?(?:其实|实际上|真相[是为]?|事实上|并不是)(.{4,28})", text)
         or re.search(r"(.{4,26}?)[，。；]\s*(?:其实|实际上|真相[是为]?|事实上)(.{4,28})", text))
    if not m:
        raise _NoFit("没找到误区→真相结构")
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "辟谣",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "myth": m.group(1).strip()[:30],
        "truth": m.group(2).strip()[:34],
        "note": text[:24],
    }


def _fields_kpi(text: str) -> dict:
    """数据卡片组：数字对 ≥2 张卡。"""
    coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    pairs = _chart_pairs(coarse)
    if len(pairs) < 2:
        raise _NoFit("数据卡不足 2 张")
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "成绩单",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "cards": [f"{v}|{k}" for k, v in pairs[:4]],
        "note": text[:24],
    }


def _fields_spectrum(text: str) -> dict:
    """光谱轴：位置语义太抽象，交给 LLM 合成。"""
    raise _NoFit("光谱落点交给 LLM 合成")


def _fields_qa(text: str) -> dict:
    """问答卡：问句 + 其后内容为答。"""
    m = re.search(r"([^。！；]{6,40}[？?])", text)
    if not m:
        raise _NoFit("没有问句")
    q = m.group(1)
    after = text[m.end():].strip()
    after = re.sub(r"^[，。、：:\s因为]+", "", after)
    if len(after) < 4:
        raise _NoFit("问句后没有答案")
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "快问快答",
        "headline": (_pick_clause(cl, max_len=14) or q[:14])[:16],
        "question": q[:44],
        "answer": after[:48],
        "note": text[:24],
    }


def _fields_checklist(text: str) -> dict:
    """打勾清单：动作节点对（3-5 项）。"""
    items = _pair_nodes(text, min_n=3, max_n=5)
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "自查",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "items": items,
        "note": text[:24],
    }


def _fields_balance(text: str) -> dict:
    """天平：两方权衡语义，交给 LLM 合成。"""
    raise _NoFit("权衡结构交给 LLM 合成")


def _fields_radar(text: str) -> dict:
    """雷达：维度+数值对（≥3 轴）。"""
    coarse = [c for c in re.split(r"(?<=[。！？；，])", text) if c.strip()]
    pairs = _chart_pairs(coarse)
    if len(pairs) < 3:
        raise _NoFit("雷达维度不足")
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "评测",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "axes": [f"{k}|{v}" for k, v in pairs[:5]],
        "note": text[:24],
    }


def _fields_gauge(text: str) -> dict:
    """仪表：单指标（评分/百分比/指数）。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:分|点|%)", text)
    if not m:
        raise _NoFit("没找到单一指标")
    val = float(m.group(1))
    pct = int(val if "%" in m.group(0) or val <= 100 else min(100, val / 10))
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "打分",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "value_text": m.group(1) + ("分" if "分" in m.group(0) else ""),
        "value_pct": pct,
        "note": text[:24],
    }


def _fields_stairs(text: str) -> dict:
    """阶梯：递进节点对（3-4 级）。"""
    steps = _pair_nodes(text, min_n=3, max_n=4)
    cl = _clauses(text)
    return {
        "kicker": text[:6] or "进阶",
        "headline": (_pick_clause(cl[1:], max_len=14) or text[:14])[:16],
        "steps": steps,
        "note": text[:24],
    }


def _fields_freeblocks(text: str) -> dict:
    """自由拼装：子句 → list 块，含数字的子句升级 stat 块。"""
    cl = [c for c in _clauses(text) if len(c) >= 3]
    if len(cl) < 2:
        raise _NoFit("可拼装内容不足")
    blocks = []
    lis = [c[:16] for c in cl[:3]]
    blocks.append("list|" + "；".join(lis))
    for c in cl:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?\s*[%％倍万Xx×]?)", c)
        if m and len(blocks) < 3:
            lab = re.sub(r"[。；，、的了是]", "", c)[:8]
            blocks.append(f"stat|{m.group(1)}|{lab}")
            break
    if len(blocks) < 2:
        blocks.append("quote|" + _pick_clause(cl, max_len=14))
    return {
        "kicker": text[:6] or "综合",
        "headline": (_pick_clause(cl[1:], max_len=16) or text[:16])[:18],
        "blocks": blocks[:4],
        "note": text[:24],
    }


def _fields_table(text: str) -> dict:
    """表格自由度高，规则不猜——交给 LLM 合成。"""
    raise _NoFit("表格结构交给 LLM 合成")


_EXTRACTORS = {
    "title-hook": _fields_hook,
    "concept-compare": _fields_compare,
    "bullet-list": _fields_bullets,
    "big-stat": _fields_stat,
    "chart-bars": _fields_chart,
    "term-demo": _fields_tty,
    "diagram-flow": _fields_flow,
    "diagram-layers": _fields_layers,
    "diagram-cycle": _fields_cycle,
    "diagram-quadrant": _fields_quadrant,
    "diagram-hub": _fields_hub,
    "diagram-pyramid": _fields_pyramid,
    "diagram-funnel": _fields_funnel,
    "diagram-venn": _fields_venn,
    "diagram-tree": _fields_tree,
    "chart-line": _fields_line,
    "chart-donut": _fields_donut,
    "chart-table": _fields_table,
    "free-blocks": _fields_freeblocks,
    "chapter-card": _fields_freeblocks,  # 兜底：NoFit → bullet（章节卡通常由 hint 指定）
    "diagram-radar": _fields_radar,
    "diagram-gauge": _fields_gauge,
    "diagram-stairs": _fields_stairs,
    "myth-buster": _fields_myth,
    "chart-kpi": _fields_kpi,
    "diagram-spectrum": _fields_spectrum,
    "qa-card": _fields_qa,
    "checklist": _fields_checklist,
    "diagram-balance": _fields_balance,
    "formula-card": _fields_formula,
    "diagram-iceberg": _fields_iceberg,
    "before-after": _fields_batch6, "pros-cons": _fields_batch6,
    "facts-trio": _fields_batch6, "diagram-domino": _fields_batch6,
    "diagram-onion": _fields_batch6, "diagram-tug": _fields_batch6,
    "diagram-swimlane": _fields_batch6, "diagram-loop-duo": _fields_batch6,
    "chart-waterfall": _fields_batch7, "diagram-daywheel": _fields_batch7,
    "stat-clash": _fields_batch7, "stat-equivalent": _fields_batch7,
    "diagram-callouts": _fields_batch7, "habit-grid": _fields_batch7,
    "headline-barrage": _fields_batch7, "diagram-bottleneck": _fields_batch7,
    "chart-histogram": _fields_batch8, "chart-poll": _fields_batch8,
    "tier-list": _fields_batch8, "chart-splitbar": _fields_batch8,
    "experiment-log": _fields_batch8, "bio-timeline": _fields_batch8,
    "stat-dotmatrix": _fields_batch9, "stat-thermometer": _fields_batch9,
    "compare-trio": _fields_batch9, "progress-bundle": _fields_batch9,
    "sort-bins": _fields_batch9, "chain-distort": _fields_batch9,
    "quiz-reveal": _fields_batch9, "chat-bubbles": _fields_batch9,
    "word-slot": _fields_batch10, "receipt-strip": _fields_batch10,
    "ticket-stub": _fields_batch10, "nutrition-label": _fields_batch10,
    "terminal-window": _fields_batch10, "split-flap": _fields_batch10,
    "game-quest": _fields_batch10, "comic-panel": _fields_batch10,
    "finance-candle": _fields_batch11, "medical-report": _fields_batch11,
    "legal-clause": _fields_batch11, "grade-card": _fields_batch11,
    "resume-card": _fields_batch11, "itinerary-card": _fields_batch11,
    "waybill-track": _fields_batch11, "monitor-dash": _fields_batch11,
    "menu-board": _fields_batch11, "floor-plan": _fields_batch11,
    "diagram-mindmap": _fields_batch12,
    "diagram-orbit": _fields_batch12,
    "diagram-triangle": _fields_batch12,
    "chart-bump": _fields_batch12,
    "chart-bubble": _fields_batch12,
    "chart-area": _fields_batch12,
    "payslip-stub": _fields_batch12,
    "invoice-sheet": _fields_batch12,
    "boarding-pass": _fields_batch12,
    "certificate-award": _fields_batch12,
    "sticky-notes": _fields_batch12,
    "trading-card": _fields_batch12,
    "rank-list": _fields_rank,
    "diagram-fishbone": _fields_fishbone,
    "diagram-journey": _fields_journey,
    "diagram-blocks": _fields_blocks,
    "dialogue-quotes": _fields_dialogue,
    "bio-card": _fields_bio,
    "news-card": _fields_news,
    "chart-scatter": _fields_scatter,
    "diagram-gantt": _fields_gantt,
    "price-compare": _fields_price,
    "process-steps": _fields_steps,
    "term-explainer": _fields_term,
    "timeline-events": _fields_timeline,
    "quote-cta": _fields_quote,
}


# ── 装桶：句子 → N 个场景（按字数均衡）────────────────────────

# 单镜旁白预算（字）：≈ 46 字 ≈ 13.5s 语音（3.4 字/s），保证所有模板的
# max_duration_s（16–22s）都有余量；quote-cta（16s）也安全
_SCENE_CHAR_BUDGET = 46


def _bucket(sentences: list[str], n: int) -> list[list[str]]:
    total = sum(len(s) for s in sentences)
    target = total / max(1, n)
    buckets: list[list[str]] = [[] for _ in range(n)]
    i = 0
    for s in sentences:
        cur = buckets[i]
        # 当前桶已满 且 还有剩余桶 → 换下一桶
        if cur and sum(len(x) for x in cur) + len(s) > target * 1.25 and i < n - 1:
            i += 1
        buckets[i].append(s)
    return [b for b in buckets if b] or [sentences]


def _split_heavy(buckets: list[list[str]], max_scenes: int = 16) -> list[list[str]]:
    """任何一桶超过字数预算 → 在句边界二分，直到全部达标或到达镜数上限。"""
    out = [list(b) for b in buckets]
    while len(out) < max_scenes:
        idx = max(range(len(out)), key=lambda i: sum(len(x) for x in out[i]))
        weight = sum(len(x) for x in out[idx])
        if weight <= _SCENE_CHAR_BUDGET * 1.15:
            break
        # 按句边界在中间切
        b = out[idx]
        total = sum(len(x) for x in b)
        acc = 0
        cut = len(b) // 2
        for i, s in enumerate(b):
            acc += len(s)
            if acc >= total / 2:
                cut = max(1, i + 1)
                break
        if cut <= 0 or cut >= len(b):
            break
        out = out[:idx] + [b[:cut], b[cut:]] + out[idx + 1 :]
    return out


def _rule_storyboard(script_text: str, scene_count: int, registry: TemplateRegistry) -> list[StoryboardScene]:
    sents = _sentences(script_text)
    buckets = _bucket(sents, scene_count)
    buckets = _split_heavy(buckets)  # 单镜超字数预算 → 分裂补镜

    out: list[StoryboardScene] = []
    for bi, bucket in enumerate(buckets):
        text = "".join(bucket)
        if bi == 0:
            tid = (registry.by_role("hook") or [None])[0]
            tid = tid.id if tid else "title-hook"
        elif bi == len(buckets) - 1:
            t = (registry.by_role("outro") or [None])[0]
            tid = t.id if t else "quote-cta"
        else:
            tid = _choose_body_template(text)
        extractor = _EXTRACTORS.get(tid, _fields_bullets)
        try:
            fields = extractor(text)
        except _NoFit:
            fields = None
            if tid in DIAGRAM_TIDS:
                try:
                    from .llm import synthesize_fields
                    fields = synthesize_fields(tid, text, registry=registry)
                except Exception:  # noqa: BLE001
                    fields = None
            if fields is None:
                tid = "bullet-list"
                fields = _fields_bullets(text)
        apply_variant(tid, fields, text)
        out.append(StoryboardScene(template_id=tid, voice_text=text, fields=fields))
    return out


# ── 对外入口 ─────────────────────────────────────────────────

def generate_storyboard(
    script_text: str,
    *,
    duration_target_s: int,
    registry: TemplateRegistry | None = None,
    engine: str = "auto",
) -> tuple[str, list[StoryboardScene]]:
    """返回 (实际使用的引擎, 场景列表)。字段已全部通过模板 schema 校验。"""
    reg = registry or TemplateRegistry()
    # 镜数由目标时长与文案长度共同决定：文案越长镜越多（长视频 14 封顶），保证单镜不超时
    n_by_time = max(3, min(14, round(duration_target_s / 12)))
    n_by_len = math.ceil(len(script_text) / _SCENE_CHAR_BUDGET)
    scene_count = max(3, min(14, max(n_by_time, n_by_len)))

    used = "rule"
    scenes: list[StoryboardScene] = []

    get_settings()
    if engine in ("auto", "llm"):
        llm_engine = LLMStoryboardEngine(reg)
        if llm_engine.available:
            try:
                raw = llm_engine.generate(script_text, scene_count=scene_count,
                                          aspect="16:9", voice="zf_xiaobei")
                scenes = [StoryboardScene(t["template_id"], t["voice_text"], t["fields"])
                          for t in raw]
                used = "llm"
            except Exception as e:
                log.warning("LLM 分镜失败，回落规则引擎: %s", e)

    if not scenes:
        scenes = _rule_storyboard(script_text, scene_count, reg)
        used = "rule"

    # 终检：任何一镜不合规 → 修字段（截断到限制）或直接抛错
    for sc in scenes:
        try:
            reg.validate(sc.template_id, sc.fields)
        except TemplateValidationError as e:
            raise e
    return used, scenes
