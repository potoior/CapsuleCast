"""模板引擎：加载固定模板、严格校验字段、渲染场景 HTML。

模板 = HTML(Jinja2) + JSON Schema + meta（动效预设/角色/画幅）。
字段缺失、超长、类型不符 → 直接拒绝（TemplateValidationError）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from jinja2 import Environment, StrictUndefined, select_autoescape
from jsonschema import Draft202012Validator
from markupsafe import Markup

from ..config import get_settings


class TemplateValidationError(ValueError):
    """字段不合规：携带面向用户的中文错误列表。"""

    def __init__(self, template_id: str, issues: list[str]):
        self.template_id = template_id
        self.issues = issues
        super().__init__(f"模板 {template_id} 字段校验失败: " + "; ".join(issues))


@dataclass
class Template:
    id: str
    name: str
    description: str
    role: str
    aspects: list[str]
    motion: str
    min_duration_s: float
    max_duration_s: float
    field_order: list[str]
    schema: dict[str, Any]
    html: str
    example: dict[str, Any] = field(default_factory=dict)
    user_only: bool = False  # 仅用户手动选择（需外部素材，规则/LLM 不自动选）

    def field_schemas(self) -> list[dict[str, Any]]:
        """转成前端表单可直接消费的字段描述。"""
        out = []
        for key in self.field_order:
            spec = self.schema.get("properties", {}).get(key)
            if spec is None:
                continue
            is_list = spec.get("type") == "array"
            item_spec = spec.get("items", {}) if is_list else {}
            if spec.get("labels") and spec.get("minimum") is not None:
                out.append(
                    {
                        "key": key,
                        "label": spec.get("description") or key,
                        "type": "select",
                        "options": [{"value": i, "label": lbl}
                                    for i, lbl in enumerate(spec["labels"])],
                        "required": key in self.schema.get("required", []),
                    }
                )
                continue
            out.append(
                {
                    "key": key,
                    "label": spec.get("description") or key,
                    "type": "list" if is_list else ("textarea" if spec.get("maxLength", 0) > 24 else "text"),
                    "max_length": spec.get("maxLength"),
                    "required": key in self.schema.get("required", []),
                    "item_max_length": item_spec.get("maxLength"),
                    "min_items": spec.get("minItems"),
                    "max_items": spec.get("maxItems"),
                }
            )
        return out


_JINJA_ENV = Environment(
    autoescape=select_autoescape(default=True),
    undefined=StrictUndefined,  # 模板里引用了未提供的变量 → 直接报错，不静默出空
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


class TemplateRegistry:
    def __init__(self, templates_dir: Path | None = None):
        self.dir = templates_dir or get_settings().templates_dir
        self._cache: dict[str, Template] = {}
        self._base_css = ""
        self._load_all()

    # ── 加载 ────────────────────────────────────────────────

    def _load_all(self) -> None:
        self._cache.clear()
        self._base_css = (self.dir / "_shared" / "base.css").read_text(encoding="utf-8")
        for child in sorted(self.dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            meta_p = child / "meta.json"
            schema_p = child / "schema.json"
            html_p = child / "template.html"
            if not (meta_p.exists() and schema_p.exists() and html_p.exists()):
                continue
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            schema = json.loads(schema_p.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self._cache[meta["id"]] = Template(
                id=meta["id"],
                name=meta["name"],
                description=meta.get("description", ""),
                role=meta.get("role", "body"),
                user_only=bool(meta.get("user_only", False)),
                aspects=meta.get("aspects", ["16:9"]),
                motion=meta.get("motion", "fade"),
                min_duration_s=meta.get("min_duration_s", 4),
                max_duration_s=meta.get("max_duration_s", 20),
                field_order=meta.get("field_order", list(schema.get("properties", {}).keys())),
                schema=schema,
                html=html_p.read_text(encoding="utf-8"),
                example=meta.get("example", {}),
            )

    def reload(self) -> None:
        self._load_all()

    # ── 查询 ────────────────────────────────────────────────

    def list(self) -> list[Template]:
        return list(self._cache.values())

    def get(self, template_id: str) -> Template:
        tpl = self._cache.get(template_id)
        if tpl is None:
            raise TemplateValidationError(template_id, [f"模板不存在: {template_id}"])
        return tpl

    def by_role(self, role: str) -> list[Template]:
        return [t for t in self._cache.values() if t.role == role]

    @property
    def base_css(self) -> str:
        return self._base_css

    # ── 校验 ────────────────────────────────────────────────

    def validate(self, template_id: str, fields: dict[str, Any]) -> Template:
        tpl = self.get(template_id)
        if not isinstance(fields, dict):
            raise TemplateValidationError(template_id, ["字段必须是对象"])
        validator = Draft202012Validator(tpl.schema)
        issues: list[str] = []
        for err in sorted(validator.iter_errors(fields), key=lambda e: list(e.absolute_path)):
            path = ".".join(str(p) for p in err.absolute_path) or "(根)"
            msg = err.message
            # 人性化常见错误
            if err.validator == "maxLength":
                got = len(err.instance) if hasattr(err.instance, "__len__") else 0
                msg = f"超长（{got} > {err.validator_value} 字）"
            elif err.validator == "minLength":
                msg = f"太短（< {err.validator_value} 字）"
            elif err.validator == "required":
                msg = f"缺少必填字段: {err.message}"
            elif err.validator == "minItems":
                msg = f"至少 {err.validator_value} 项"
            elif err.validator == "maxItems":
                msg = f"最多 {err.validator_value} 项"
            elif err.validator == "type":
                msg = f"类型错误: 需要 {err.validator_value}"
            issues.append(f"{path}: {msg}")
        if issues:
            raise TemplateValidationError(template_id, issues)
        return tpl

    # ── 渲染 ────────────────────────────────────────────────

    def render_frame(
        self,
        template_id: str,
        fields: dict[str, Any],
        *,
        scene_id: str,
        duration_s: float,
        width: int,
        height: int,
        theme_tokens: dict[str, str] | None = None,
        style_name: str = "",
        is_last: bool = False,
    ) -> str:
        tpl = self.validate(template_id, fields)
        self._style_name = style_name
        # 场景时长必须落在模板允许区间
        if not (tpl.min_duration_s <= duration_s <= tpl.max_duration_s):
            raise TemplateValidationError(
                template_id,
                [f"时长 {duration_s:.1f}s 超出模板允许范围 "
                 f"{tpl.min_duration_s:.0f}–{tpl.max_duration_s:.0f}s"],
            )
        # base.css：根选择器占位符替换 + 主题令牌覆盖（后置同特异性规则胜出）
        base_css = self._base_css.replace("ROOT_SEL", f"#f-{scene_id}")
        if theme_tokens:
            from .themes import override_css, style_extra_css
            base_css += override_css(f"#f-{scene_id}", theme_tokens)
            extra = style_extra_css((getattr(self, "_style_name", "") or ""))
            if extra:
                base_css += "\n" + extra.replace("ROOT_SEL", f"#f-{scene_id}")
        ctx = {
            "scene_id": scene_id,
            "duration": round(duration_s, 3),
            "is_last": is_last,
            "width": width,
            "height": height,
            "base_css": Markup(base_css),
            "fields": fields,
            **fields,  # 模板里直接 {{ headline }}
        }
        return _JINJA_ENV.from_string(tpl.html).render(**ctx)


# 单例
_registry: TemplateRegistry | None = None


def get_registry() -> TemplateRegistry:
    global _registry
    if _registry is None:
        _registry = TemplateRegistry()
    return _registry


ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


def aspect_size(aspect: str) -> tuple[int, int]:
    return ASPECT_SIZES[aspect]
