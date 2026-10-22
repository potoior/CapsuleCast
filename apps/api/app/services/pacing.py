"""时长规划（pacing）：看完剧本选时长后，把成片拉到目标时长。

两个杠杆：
  · 语速 speed：Kokoro TTS 速度（0.85–1.22 安全区间，太极端听感差）
  · 停留 pads：每镜尾部停留秒数（受模板 max_duration_s 约束）

流程：分镜阶段用 speed=1.0 合成（自然时长 voice_s 落库）→ 用户选目标
→ 渲染时按 自然/目标 比 定 speed，TTS 实际产出后用剩余秒数算 pads。
达不到的目标（文案太短/太长）如实返回可达时长，绝不虚标。
"""

from __future__ import annotations

from dataclasses import dataclass, field

SPEED_MIN = 0.85
SPEED_MAX = 1.22
FADE_S = 0.5              # 转场 crossfade（不占片长，场景时长需为其预留）
PAD_MAX = 4.0             # 单镜停留上限（防止死画面过久）


@dataclass
class PacingPlan:
    mode: str              # standard | compact | relaxed
    target_s: float        # 用户目标
    speed: float = 1.0
    estimated_s: float = 0.0   # 预计可达时长
    note: str = ""
    pads: list[float] = field(default_factory=list)  # 每镜停留（占真实片长）


def choose_speed(mode: str, natural_s: float, target_s: float) -> float:
    """由模式/目标推语速。standard 固定 1.0。"""
    if mode == "standard" or target_s <= 0 or natural_s <= 0:
        return 1.0
    ratio = natural_s / target_s      # >1 说明要说快
    return max(SPEED_MIN, min(SPEED_MAX, ratio))


def estimate_after_speed(natural_s: float, speed: float) -> float:
    """语速线性近似：speed 1.12 ≈ 时长 / 1.12。"""
    return natural_s / speed if speed > 0 else natural_s


def plan_pads(
    *,
    voice_durations: list[float],   # 实际 TTS 时长（当前 speed 下）
    template_maxes: list[float],    # 每镜模板允许的最大场景时长
    target_s: float,
    mode: str,
) -> PacingPlan:
    """TTS 之后调用：把目标与实际的差值分配到停留（占真实片长）。"""
    n = len(voice_durations)
    if n == 0:
        return PacingPlan(mode=mode, target_s=target_s, estimated_s=0)

    voiced = sum(voice_durations)
    pads = [0.0] * n                     # 默认无停留（= 参考项目排布）
    total = voiced

    if mode == "standard" or target_s <= 0:
        return PacingPlan(mode=mode, target_s=target_s, estimated_s=total,
                          pads=pads, note="自然时长")

    deficit = target_s - total
    if abs(deficit) < 1.0:
        return PacingPlan(mode=mode, target_s=target_s, estimated_s=total,
                          pads=pads, note="已达标")

    if deficit > 0:
        # 拉长：停留优先给中段镜（记忆点），末镜吃剩余（静置收尾最自然）
        # 余量 = 模板最大时长 - 语音 - 转场预留
        room = [max(0.0, template_maxes[i] - voice_durations[i] - FADE_S)
                for i in range(n)]
        give = deficit
        per = give / max(1, n)
        for i in range(n - 1):
            add = max(0.0, min(per, PAD_MAX, room[i]))
            pads[i] += add
            give -= add
        pads[-1] += max(0.0, min(give, PAD_MAX * 2, room[-1]))
        total = voiced + sum(pads)
        note = "已延长至目标" if total >= target_s - 1 else "文案偏短，已尽量延长"
    else:
        # 压缩：语速已在 choose_speed 提前加快；停留本就是 0，无可再压
        note = "已压缩至目标" if total <= target_s + 1 else "文案偏长，已尽量压缩"

    return PacingPlan(mode=mode, target_s=target_s, estimated_s=total,
                      pads=pads, note=note)


def duration_options(natural_s: float) -> list[dict]:
    """分镜确认页的时长档位（基于自然时长，5 秒取整）。"""
    def snap(x: float) -> int:
        return max(20, int(round(x / 5) * 5))

    std = snap(natural_s)
    return [
        {"mode": "compact", "label": "紧凑", "target_s": snap(natural_s * 0.85),
         "hint": "语速略快 · 停留收紧"},
        {"mode": "standard", "label": "标准", "target_s": std,
         "hint": "自然语速 · 原生节奏"},
        {"mode": "relaxed", "label": "舒缓", "target_s": snap(natural_s * 1.2),
         "hint": "语速放缓 · 停留加长"},
        {"mode": "relaxed", "label": "深度", "target_s": snap(min(natural_s * 1.5, 295)),
         "hint": "大幅停留 · 适合长内容"},
    ]
