"""LLM 分镜引擎（可选）。

对接 vLLM（OpenAI 兼容接口，如 Qwen3.6-35B-A3B）。
· 只允许输出结构化 JSON：response_format = json_schema（vLLM guided decoding）
· 服务端再用模板 schema 逐场景二次校验；不合规 → 带错误重试一次 → 仍失败则 422
· 未配置 / 调用失败 → 自动回落规则引擎（storyboard.rule_engine）

不在 MVP 验收路径上：LLM_BASE_URL 为空时本模块不会被调用。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from ..config import get_settings
from .template_engine import TemplateRegistry, TemplateValidationError

log = logging.getLogger(__name__)

_MAX_RETRIES = 2

# 内网/本机不走代理（httpx 显式传 proxy 时不看 NO_PROXY 环境变量）
_PRIVATE = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.",
            "172.19.", "172.2", "172.30.", "172.31.", "localhost")


def proxy_for(base_url: str) -> str | None:
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").lower()
        if not host or host in _PRIVATE or any(
            host.startswith(p) for p in ("10.", "192.168.", "127.", "172.")
        ):
            return None
    except Exception:
        pass
    return get_settings().http_proxy or None


# ── 模型链：主模型 → 备用模型（LLM_FALLBACK_MODELS，逗号分隔） ──

def model_chain() -> list[str]:
    s = get_settings()
    models = [s.llm_model] if s.llm_model else []
    models += [m.strip() for m in (s.llm_fallback_models or "").split(",") if m.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# ── 瞬时抖动 vs 致命错误：同模型先重试，再降级 ────────────────
_RETRY_ATTEMPTS = 3          # 每模型最多尝试次数（含首发）
_RETRY_BACKOFF = (1.5, 3.5)  # 重试退避秒数

# 等满超时的错误：说明该模型病得不轻，重试=白等，直接降级
_FATAL_TIMEOUT = (httpx.ReadTimeout,)


def _is_transient(e: Exception) -> bool:
    """瞬时错误（网络抖动/网关过载）→ 值得同模型重试。"""
    if isinstance(e, _FATAL_TIMEOUT):
        return False
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.WriteError,
                      httpx.ReadError, httpx.RemoteProtocolError, httpx.PoolTimeout)):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500 or e.response.status_code == 429
    if isinstance(e, json.JSONDecodeError):
        return True  # 响应体截断
    if isinstance(e, RuntimeError):
        t = str(e).lower()
        return any(k in t for k in ("overload", "timeout", "rate limit", "繁忙", "过载"))
    return False


def chat_with_fallback(
    messages: list[dict],
    *,
    response_format: dict | None = None,
    temperature: float = 0.5,
    max_tokens: int = 6000,
    on_fallback=None,
) -> tuple[str, str]:
    """按模型链依次尝试，全部失败抛 RuntimeError。返回 (content, model_used)。

    on_fallback(model)：从第二个模型开始命中时回调（用于日志）。
    """
    s = get_settings()
    base = (s.llm_base_url or "").rstrip("/")
    if not base:
        raise ValueError("LLM 未配置")
    headers = {"Authorization": f"Bearer {s.llm_api_key or 'EMPTY'}"}
    chain = model_chain()
    last_err: Exception | None = None
    for i, model in enumerate(chain):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                with httpx.Client(timeout=s.llm_timeout_s, proxy=proxy_for(base)) as client:
                    r = client.post(f"{base}/chat/completions", headers=headers, json=payload)
                    r.raise_for_status()
                    body = r.json()
                    if "error" in body:
                        raise RuntimeError(str(body["error"].get("message", "LLM 网关错误"))[:400])
                    msg = body["choices"][0]["message"]
                    content = msg.get("content") or ""
                    if not content or not str(content).strip():
                        choice = body["choices"][0]
                        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                        usage = body.get("usage") or {}
                        log.warning(
                            "LLM 空内容诊断: model=%s finish=%s reasoning=%d字 "
                            "usage(completion=%s, prompt=%s) content_type=%s",
                            model, choice.get("finish_reason"), len(str(reasoning)),
                            usage.get("completion_tokens"), usage.get("prompt_tokens"),
                            type(msg.get("content")).__name__)
                        raise RuntimeError("LLM 返回空内容")
                    if i and on_fallback:
                        on_fallback(model)
                    return str(content), model
            except Exception as e:
                last_err = e
                transient = _is_transient(e)
                if transient and attempt < _RETRY_ATTEMPTS - 1:
                    wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                    log.warning("LLM 模型 %s 瞬时抖动（尝试 %d/%d），%.1fs 后原模型重试: %s",
                                model, attempt + 1, _RETRY_ATTEMPTS, wait, str(e)[:160])
                    time.sleep(wait)
                    continue
                log.warning("LLM 模型 %s 调用失败（链 %d/%d，已尝试 %d/%d%s）: %s",
                            model, i + 1, len(chain), attempt + 1, _RETRY_ATTEMPTS,
                            "" if transient else "，非瞬时错误不重试",
                            str(e)[:160])
                break  # 本模型放弃 → 降级到链上下一个
    raise RuntimeError(f"LLM 模型链全部失败（{len(chain)} 个）：{last_err}")


def _storyboard_json_schema(registry: TemplateRegistry, aspects: list[str]) -> dict:
    """合并所有模板 schema → 一个分镜级 JSON Schema（oneOf 按 template_id 分支）。"""
    branches = []
    for tpl in registry.list():
        if tpl.user_only:
            continue  # 需外部素材（如图片），只能用户手动选
        props = dict(tpl.schema.get("properties", {}))
        branch_props = {
            "template_id": {"const": tpl.id},
            "voice_text": {
                "type": "string", "minLength": 8, "maxLength": 60,
                "description": "该场景的中文旁白（8-60 字，将用于 TTS）",
            },
            "fields": {"type": "object", "properties": props,
                       "required": tpl.schema.get("required", []),
                       "additionalProperties": False},
        }
        branches.append({
            "type": "object",
            "properties": branch_props,
            "required": ["template_id", "voice_text", "fields"],
            "additionalProperties": False,
        })
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "storyboard",
            "schema": {
                "type": "object",
                "properties": {
                    "scenes": {
                        "type": "array", "minItems": 3, "maxItems": 14,
                        "items": {"oneOf": branches},
                    },
                },
                "required": ["scenes"],
                "additionalProperties": False,
            },
            "strict": False,
        },
    }


# ── LLM 字段合成：为图解/图表模板从旁白生成 fields ────────────
# 规则抽取器是兜底；hint 命中图解时优先走这里（一次调用 ~7s），
# 不合规自动重试一次，仍失败返回 None（调用方回落规则/bullet）。

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _stated_count(narration: str) -> int | None:
    """旁白明确列举数量（「三个原则/四步/两条」）→ 期望项数。"""
    m = re.search(r"([一两二三四五六七八九十\d])\s*(?:个|条|步|样|项|招|方面|原则|动作|点)",
                  narration or "")
    if not m:
        return None
    tok = m.group(1)
    return _CN_NUM.get(tok) or (int(tok) if tok.isdigit() else None)


DIAGRAM_TIDS = frozenset({
    "chart-bars", "chart-line", "chart-donut", "chart-table", "chart-kpi",
    "diagram-flow", "diagram-layers", "diagram-cycle", "diagram-quadrant",
    "diagram-hub", "diagram-pyramid", "diagram-funnel", "diagram-venn",
    "diagram-tree", "diagram-radar", "diagram-gauge", "diagram-stairs",
    "diagram-spectrum", "diagram-balance",
    "myth-buster", "qa-card", "checklist", "free-blocks", "chapter-card",
    "formula-card", "diagram-iceberg", "rank-list", "diagram-fishbone",
    "diagram-journey", "diagram-blocks", "dialogue-quotes", "bio-card",
    "news-card", "chart-scatter", "diagram-gantt", "price-compare",
    "before-after", "pros-cons", "facts-trio",
    "diagram-domino", "diagram-onion", "diagram-tug",
    "diagram-swimlane", "diagram-loop-duo",
    "chart-waterfall", "diagram-daywheel", "stat-clash", "stat-equivalent",
    "diagram-callouts", "habit-grid", "headline-barrage", "diagram-bottleneck",
    "chart-histogram", "chart-poll", "tier-list", "chart-splitbar",
    "experiment-log", "bio-timeline",
    "stat-dotmatrix", "stat-thermometer", "compare-trio", "progress-bundle",
    "sort-bins", "chain-distort", "quiz-reveal", "chat-bubbles",
    "word-slot", "receipt-strip", "ticket-stub", "nutrition-label",
    "terminal-window", "split-flap", "game-quest", "comic-panel",
})


def synthesize_fields(
    template_id: str,
    narration: str,
    *,
    title: str = "",
    points: list[str] | None = None,
    registry: TemplateRegistry | None = None,
    max_tokens: int = 4000,
) -> dict | None:
    """LLM 从旁白合成模板 fields；成功返回通过 schema 校验的 dict，失败 None。"""
    reg = registry or TemplateRegistry()
    tpl = reg.get(template_id)
    if tpl is None:
        return None
    props = tpl.schema.get("properties", {})
    req = set(tpl.schema.get("required", []))
    spec_lines = []
    for key in tpl.field_order:
        p = props.get(key)
        if not p:
            continue
        tag = "必填" if key in req else "可留空/省略"
        if p.get("type") == "array":
            it = p.get("items", {})
            spec_lines.append(
                f"- {key}（{tag}）：数组 {p.get('minItems', 1)}-{p.get('maxItems', 9)} 项，"
                f"每项 {it.get('minLength', 1)}-{it.get('maxLength', 30)} 字：{it.get('description', '')}")
        else:
            spec_lines.append(
                f"- {key}（{tag}）：{p.get('minLength', 0)}-{p.get('maxLength', 99)} 字：{p.get('description', '')}")
    example = json.dumps(tpl.example, ensure_ascii=False) if tpl.example else "{}"
    system = (
        f"你是短视频分镜美术指导，擅长把旁白浓缩成图解/图表文案。"
        f"为模板「{tpl.id}·{tpl.name}」生成 fields JSON。模板说明：{tpl.description}\n"
        f"字段规范（严格遵守字数与项数限制；文字短促有力、口语化；节点必须是独立概念词，不写句子残片）：\n"
        + "\n".join(spec_lines) + "\n"
        f"字段结构示例（仅参考结构，内容按旁白重写）：{example}\n"
        "只输出一个 JSON 对象，键为字段名，禁止解释或代码块标记。"
    )
    user = (
        f"这一镜的标题：{title or '（无）'}\n"
        f"要点：{'；'.join(points[:5]) if points else '（无）'}\n"
        f"旁白：{narration}"
    )
    sys_text = system
    for attempt in range(2):
        try:
            content, _m = chat_with_fallback(
                [{"role": "system", "content": sys_text},
                 {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                temperature=0.4, max_tokens=max_tokens,
            )
            data: dict | None = None
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                st, en = content.find("{"), content.rfind("}")
                if st >= 0 and en > st:
                    data = json.loads(content[st:en + 1])
            if not isinstance(data, dict):
                raise ValueError("输出不是 JSON 对象")
            data = {k: v for k, v in data.items()
                    if k in props and k not in ("variant", "layout") and v not in (None, "")}
            reg.validate(template_id, data)
            # 数量忠实：旁白明说「N 个/条/步/样」时，主数组必须是 N 项
            want = _stated_count(narration)
            if want is not None:
                arrays = [(k, v) for k, v in data.items() if isinstance(v, list)]
                if len(arrays) == 1 and len(arrays[0][1]) != want:
                    raise TemplateValidationError(template_id, [
                        f"{arrays[0][0]}：旁白说 {want} 项，输出了 {len(arrays[0][1])} 项"])
            from .storyboard import apply_variant
            apply_variant(template_id, data, narration)
            return data
        except TemplateValidationError as e:
            log.warning("LLM 字段合成第 %d 次不合规 %s: %s", attempt + 1, template_id, str(e)[:220])
            sys_text = system + (
                "\n你上次的输出未通过校验，问题如下，务必修正后重新输出合规 JSON：\n" + str(e)[:300])
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 字段合成失败 %s: %s", template_id, e)
            return None
    return None


class LLMStoryboardEngine:
    def __init__(self, registry: TemplateRegistry):
        self.registry = registry
        s = get_settings()
        self.base_url = s.llm_base_url.rstrip("/") if s.llm_base_url else ""
        self.api_key = s.llm_api_key or "EMPTY"
        self.model = s.llm_model
        self.timeout = s.llm_timeout_s

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    def generate(
        self,
        script_text: str,
        *,
        scene_count: int,
        aspect: str,
        voice: str,
    ) -> list[dict[str, Any]]:
        """返回 [{template_id, voice_text, fields}, ...]；失败抛 ValueError。"""
        if not self.available:
            raise ValueError("LLM 未配置")

        rf = _storyboard_json_schema(self.registry, [aspect])
        system = (
            "你是短视频分镜师。把用户文案切成 {n} 个场景，从固定模板库中选择模板并填充字段。"
            "规则：字段必须符合 JSON Schema 的长度限制（中文字符数）；"
            "模板顺序建议：第一镜用 hook 角色，最后一镜用 outro 角色；"
            "voice_text 是该镜的旁白原文片段，覆盖全部文案内容且场景间不重复；"
            "只输出 JSON。".format(n=scene_count)
        )
        user = (
            f"文案：\n{script_text}\n\n"
            f"要求：切分为 {scene_count} 个场景。第一镜 template_id 用 "
            f"{(self.registry.by_role('hook') or [None])[0].id if self.registry.by_role('hook') else 'title-hook'}，"
            f"最后一镜用 {(self.registry.by_role('outro') or [None])[0].id if self.registry.by_role('outro') else 'quote-cta'}。"
            f"中间场景按内容选最贴切的模板。"
        )

        last_error = ""
        for attempt in range(1, _MAX_RETRIES + 1):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            if last_error:
                messages.append({"role": "user",
                                 "content": f"上次输出校验失败：{last_error}。请修正后重新输出完整 JSON。"})
            data = self._chat(messages, rf)
            if data is None:
                raise ValueError("LLM 返回为空")
            scenes = data.get("scenes")
            if not isinstance(scenes, list) or not scenes:
                raise ValueError("LLM 未返回 scenes")
            issues = self._validate(scenes)
            if not issues:
                return scenes
            last_error = "; ".join(issues[:8])
            log.warning("LLM 分镜第 %d 次校验失败: %s", attempt, last_error)

        raise ValueError(f"LLM 分镜两次校验失败: {last_error}")

    # ── 内部 ────────────────────────────────────────────────

    def _chat(self, messages: list[dict], response_format: dict) -> dict | None:
        content, _ = chat_with_fallback(
            messages, response_format=response_format,
            temperature=0.4, max_tokens=16000,
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 兜底：截取首个 JSON 对象
            start, end = content.find("{"), content.rfind("}")
            if start >= 0 and end > start:
                return json.loads(content[start : end + 1])
            raise

    def _validate(self, scenes: list[dict]) -> list[str]:
        issues: list[str] = []
        for i, sc in enumerate(scenes):
            tid = sc.get("template_id", "?")
            fields = sc.get("fields") or {}
            try:
                self.registry.validate(tid, fields)
            except TemplateValidationError as e:
                issues.extend(f"场景{i + 1}: {m}" for m in e.issues)
            vt = sc.get("voice_text", "")
            if not (8 <= len(vt) <= 60):
                issues.append(f"场景{i + 1}: voice_text 长度 {len(vt)} 不在 8-60")
        return issues
