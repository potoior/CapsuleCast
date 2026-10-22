"""确定性词级对齐 + 词级卡拉OK字幕。

参考项目（token-cache-explained）的词级时间戳实测为"按字数比例分配"：
每个词的时长 ≈ 字数 × 常数，标点分配小停顿。本模块复刻该方法：

  1. 中文用贪心最长匹配切词（内置常用词表），拉丁/数字按 token；
  2. 字符权重：CJK=1.0，拉丁=0.55，数字=0.6，标点=0.35（不显示，只占时）；
  3. 时长按权重比例分配（可选用 ffmpeg silencedetect 修剪首尾静音）。

全程确定性、无模型依赖，误差 ~±150ms，与参考项目同级。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ── 常用中文词表（贪心最长匹配；覆盖参考项目风格的常用组合） ──────────
_COMMON_WORDS = """
一个 一直 一起 一些 一致 一切 一段 一般 十分之一 百分之
第二 第三 首先 然后 其次 最后 接着 此外 另外 例如 比如
可能 可以 应该 必须 需要 就是 都是 也是 不是 还是 但是 因此 所以
如果 虽然 然后 于是 并且 或者 而且 无论 不管 只要 除非
什么 怎么 为什么 这样 那样 哪些 其中 其他 任何 所有 全部
我们 你们 他们 它们 自己 大家 一起 之间 之前 之后 以后 以后
时间 价格 成本 收益 结果 原因 问题 方法 方式 原理 概念 定义
模型 数据 系统 服务 用户 客户 产品 平台 工具 程序 代码 结构
内容 文本 文档 信息 知识 经验 规则 标准 目标 过程 步骤 阶段
提高 降低 减少 增加 优化 改进 实现 完成 开始 结续 持续 保持
输入 输出 计算 读取 存储 缓存 调用 请求 响应 处理 生成 转换
平均值 最大值 最小值
上亿 成本价
直接 间接 通常 经常 偶尔 一定 大概 大约 几乎 完全 基本
更加 越来越 相当 比较 非常 特别 尤其 重要 关键 核心 主要
提示词 大模型 机器人 人工智能 深度学习 机器学习 神经网络
互联网 计算机 服务器 数据库 应用程序 操作系统 开发者
""".split()


def _build_lexicon() -> set[str]:
    return {w for w in _COMMON_WORDS if w}


_LEXICON = _build_lexicon()

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_TOKEN_RE = re.compile(
    r"([\u4e00-\u9fff\u3400-\u4dbf]+|[A-Za-z]+|\d+(?:[.,]\d+)*%?|\s+|.)"
)


@dataclass
class Word:
    text: str
    start: float
    end: float
    display: bool = True  # False = 标点/空白，占时但不显示


@dataclass
class SceneWords:
    words: list[Word] = field(default_factory=list)

    def visible(self) -> list[Word]:
        return [w for w in self.words if w.display]


def segment_zh(run: str) -> list[str]:
    """中文切词：jieba 精确模式（自然词）；未安装时回落内置贪心词表。"""
    try:
        import jieba

        jieba.setLogLevel(60)  # 关掉初始化日志
        return [t for t in jieba.cut(run, cut_all=False) if t.strip()]
    except ImportError:
        pass
    out: list[str] = []
    i = 0
    while i < len(run):
        matched = ""
        for length in range(min(6, len(run) - i), 1, -1):
            cand = run[i : i + length]
            if cand in _LEXICON:
                matched = cand
                break
        if matched:
            out.append(matched)
            i += len(matched)
        else:
            out.append(run[i])
            i += 1
    return out


def tokenize(text: str) -> list[tuple[str, bool]]:
    """返回 [(词, 是否显示)]，标点/空白 display=False。"""
    tokens: list[tuple[str, bool]] = []
    for m in _TOKEN_RE.finditer(text):
        chunk = m.group(0)
        if not chunk:
            continue
        if chunk.isspace():
            tokens.append((chunk, False))
        elif _CJK_RE.match(chunk[0]):
            for w in segment_zh(chunk):
                tokens.append((w, True))
        elif re.match(r"^[A-Za-z0-9%.,，。！!？?；;：:、·]+$", chunk) and not re.match(r"^[A-Za-z]", chunk) and not re.match(r"^\d", chunk):
            # 纯标点
            tokens.append((chunk, False))
        else:
            tokens.append((chunk, True))
    return tokens


def _weight(tok: str, display: bool) -> float:
    if not display:
        # 标点：句末标点多停一点，逗号顿号少停
        if tok in "。！？!?；;\n":
            return 0.55
        if tok in "，、：:，":
            return 0.4
        return 0.15  # 空白等
    w = 0.0
    for ch in tok:
        if _CJK_RE.match(ch):
            w += 1.0
        elif ch.isdigit():
            w += 0.6
        elif ch.isalpha():
            w += 0.55
        else:
            w += 0.3
    return max(w, 0.4)


def detect_speech_span(wav: Path, ffmpeg_bin: str = "ffmpeg") -> tuple[float, float] | None:
    """用 silencedetect 找实际语音起止（秒）。失败返回 None。"""
    try:
        r = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(wav), "-af",
             "silencedetect=noise=-38dB:d=0.18", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        err = r.stderr
        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
        dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
        total = sum(int(dur_m.group(i)) * f for i, f in ((1, 3600), (2, 60), (3, 1))) if dur_m else None
        if total is None:
            return None
        # 语音起点 = 第一个非静音（若开头静音）
        lead = ends[0] if starts and starts[0] < 0.05 and ends else 0.0
        tail_start = starts[-1] if starts and starts[-1] > total * 0.7 else total
        return (min(lead, total * 0.4), max(tail_start, total * 0.6))
    except Exception:
        return None


def measure_lead_in(wav_path: Path, *, frame_ms: float = 10.0) -> float:
    """实测语音起点（秒）：能量包络 + 自适应阈值。

    Kokoro 输出常有 0.1-0.3s 的前导静音，按字数比例的字幕会整体超前。
    用每镜 WAV 的真实起点平移该镜全部字幕，误差可压到 ±50ms。
    失败时回落默认 0.04s。
    """
    import wave as _wave

    import numpy as np

    try:
        with _wave.open(str(wav_path)) as w:
            sr, ch = w.getframerate(), w.getnchannels()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        x = data.astype(np.float64) / 32768.0
        if len(x) < int(sr * 0.2):
            return 0.04
        hop = max(1, int(sr * frame_ms / 1000))
        nfr = len(x) // hop
        env = np.sqrt((x[: nfr * hop].reshape(nfr, hop) ** 2).mean(axis=1))
        thr = max(1e-3, float(np.percentile(env, 95)) * 0.06)
        above = np.where(env > thr)[0]
        if len(above) == 0:
            return 0.04
        return round(min(float(above[0]) * hop / sr, 0.6), 3)
    except Exception:
        return 0.04


def align_scene_words(text: str, duration_s: float, *, lead_in_s: float = 0.04) -> SceneWords:
    """把已知文本按权重比例铺到 [0, duration_s] 上。"""
    tokens = tokenize(text)
    weights = [_weight(t, d) for t, d in tokens]
    total_w = sum(weights) or 1.0
    speech = duration_s - lead_in_s
    words: list[Word] = []
    t = lead_in_s
    for (tok, disp), w in zip(tokens, weights, strict=False):
        dur = speech * w / total_w
        words.append(Word(text=tok, start=round(t, 3), end=round(t + dur, 3), display=disp))
        t += dur
    # 末端对齐
    if words:
        last = words[-1]
        last.end = max(last.start + 0.01, round(duration_s, 3))
    return SceneWords(words=words)


# ── 字幕分组（卡拉OK卡片）：按断句走，绝不跨标点 ─────────────
# 规则（对齐参考项目的行为）：
#   1. 标点（。！？；，、：…）处硬断组 —— 一张字幕卡 = 一个语义短语；
#   2. 短语内超过 4 词 / 12 字时，在词边界拆分；
#   3. 时间跳变 > 0.3s 也断组（保险）。

def group_words(sw: SceneWords, *, max_words: int = 4, max_chars: int = 12) -> list[dict]:
    groups: list[dict] = []
    cur: list[Word] = []
    cur_chars = 0
    prev_end: float | None = None

    def flush():
        nonlocal cur, cur_chars
        if not cur:
            return
        groups.append(
            {
                "text": " ".join(w.text for w in cur),
                "start": round(cur[0].start, 3),
                "end": round(max(cur[-1].end, cur[0].start + 0.25), 3),
                "words": [
                    {"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)}
                    for w in cur
                ],
            }
        )
        cur, cur_chars = [], 0

    for w in sw.words:
        if not w.display:
            # 标点/空白：断句（空白权重极小的直接吞掉不断）
            if w.text.strip() == "":
                continue
            flush()
            prev_end = w.end
            continue
        # 时间跳变断组（保险）
        if prev_end is not None and cur and w.start - prev_end > 0.3:
            flush()
        # 短语内容量上限：词边界拆分
        if cur and (len(cur) >= max_words or cur_chars + len(w.text) > max_chars):
            flush()
        cur.append(w)
        cur_chars += len(w.text)
        prev_end = w.end
    flush()
    return groups
