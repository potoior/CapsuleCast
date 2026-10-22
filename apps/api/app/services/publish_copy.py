"""平台发布文案：渲染完成时 LLM 顺带产出各平台标题/简介/话题标签。

一次调用，产出抖音 / B站 / 视频号 三套；LLM 失败时静默跳过（不影响渲染发布）。
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "publish_copy",
        "schema": {
            "type": "object",
            "properties": {
                "douyin": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 4, "maxLength": 30},
                        "caption": {"type": "string", "minLength": 4, "maxLength": 120},
                        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
                    },
                    "required": ["title", "caption", "tags"],
                    "additionalProperties": False,
                },
                "bilibili": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 4, "maxLength": 40},
                        "desc": {"type": "string", "minLength": 4, "maxLength": 200},
                        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 8},
                    },
                    "required": ["title", "desc", "tags"],
                    "additionalProperties": False,
                },
                "weixin": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 4, "maxLength": 30},
                        "caption": {"type": "string", "minLength": 4, "maxLength": 100},
                        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
                    },
                    "required": ["title", "caption", "tags"],
                    "additionalProperties": False,
                },
            },
            "required": ["douyin", "bilibili", "weixin"],
            "additionalProperties": False,
        },
        "strict": False,
    },
}


def generate_publish_copy(
    *,
    title: str,
    summary: str,
    hook: str,
    beats: list[dict],
) -> dict | None:
    """返回 {douyin:{title,caption,tags}, bilibili:{...}, weixin:{...}}；失败返回 None。"""
    from .llm import chat_with_fallback

    beats_txt = "\n".join(
        f"- {b.get('title', '')}：{(b.get('narration') or '')[:50]}" for b in beats[:4]
    ) or "（无）"
    system = (
        "你是短视频运营。根据视频大纲为三个平台写发布文案。要求："
        "抖音标题口语化带悬念（≤16字最佳），简介前两行必须钩人，标签含 1 个大流量词 + 2-4 个垂类词；"
        "B站标题可稍长但要有信息量，desc 客观介绍内容并引导三连；"
        "视频号面向微信生态，稳重一些。只输出 JSON。"
    )
    user = (
        f"视频标题：{title}\n"
        f"一句话简介：{summary or hook}\n"
        f"开场钩子：{hook}\n"
        f"内容层次：\n{beats_txt}"
    )
    try:
        content, model = chat_with_fallback(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            response_format=_SCHEMA,
            temperature=0.6,
            max_tokens=8000,
        )
        data = json.loads(content)
        # 轻校验：三平台齐全
        for k in ("douyin", "bilibili", "weixin"):
            if not isinstance(data.get(k), dict):
                raise ValueError(f"缺 {k}")
        log.info("发布文案已生成（模型 %s）", model)
        return data
    except Exception as e:
        log.warning("发布文案生成失败（不影响发布）: %s", e)
        return None
