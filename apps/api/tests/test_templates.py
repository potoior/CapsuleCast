"""模板注册表金测：34 套齐、schema 自洽、示例字段必须通过自身校验。"""
from pathlib import Path

from app.services.template_engine import TemplateRegistry, TemplateValidationError

TPL_ROOT = Path(__file__).resolve().parents[2] / "templates" / ".." / "templates"


def _tpl_root() -> Path:
    # apps/api/tests → 仓库根/templates
    return Path(__file__).resolve().parents[3] / "templates"


def test_registry_loads_all():
    reg = TemplateRegistry()
    ids = {t.id for t in reg.list()}
    assert len(ids) >= 68, f"模板数不足: {len(ids)}"
    for must in ["title-hook", "bullet-list", "quote-cta", "big-stat",
                 "diagram-funnel", "diagram-venn", "diagram-tree", "diagram-radar",
                 "diagram-gauge", "diagram-stairs", "diagram-spectrum",
                 "diagram-balance", "myth-buster", "chart-kpi", "chart-line",
                 "chart-donut", "chart-table", "qa-card", "checklist",
                 "free-blocks", "chapter-card",
                 "formula-card", "diagram-iceberg", "rank-list",
                 "diagram-fishbone", "diagram-journey", "diagram-blocks",
                 "dialogue-quotes", "bio-card", "news-card", "chart-scatter",
                 "diagram-gantt", "price-compare"]:
        assert must in ids, f"缺模板 {must}"


def test_every_template_example_passes_own_schema():
    """金测：meta.example 必须能通过 schema 校验——防字段规范与示例漂移。"""
    reg = TemplateRegistry()
    bad = []
    for t in reg.list():
        if not t.example:
            continue
        try:
            reg.validate(t.id, dict(t.example))
        except TemplateValidationError as e:
            bad.append(f"{t.id}: {str(e)[:90]}")
    assert not bad, "示例字段未过自身 schema:\n" + "\n".join(bad)


def test_field_order_subset_of_schema():
    reg = TemplateRegistry()
    for t in reg.list():
        props = set(t.schema.get("properties", {}))
        missing = [k for k in t.field_order if k not in props]
        assert not missing, f"{t.id}: field_order 有 schema 未定义字段 {missing}"


def test_required_fields_in_field_order():
    reg = TemplateRegistry()
    for t in reg.list():
        req = set(t.schema.get("required", []))
        assert req <= set(t.field_order), f"{t.id}: required 不在 field_order 里"


def test_variant_fields_expose_select():
    reg = TemplateRegistry()
    for tid in ("bullet-list", "title-hook", "big-stat", "quote-cta"):
        fs = reg.get(tid).field_schemas()
        sel = [f for f in fs if f["type"] == "select"]
        assert sel, f"{tid} 应有 select 型 variant 字段"
        assert sel[0]["options"], f"{tid} variant 无选项"


def test_diagram_tids_covers_hintable_templates():
    """llm.DIAGRAM_TIDS 必须覆盖所有'图解类'模板——曾因漏配导致新模板全回落 bullet。"""
    from app.services.llm import DIAGRAM_TIDS

    expect = {"chart-kpi", "diagram-spectrum", "qa-card", "checklist",
              "diagram-balance", "myth-buster", "free-blocks", "chapter-card",
              "diagram-radar", "diagram-gauge", "diagram-stairs",
              "formula-card", "diagram-iceberg", "rank-list",
              "diagram-fishbone", "diagram-journey", "diagram-blocks",
              "dialogue-quotes", "bio-card", "news-card", "chart-scatter",
              "diagram-gantt", "price-compare"}
    missing = expect - set(DIAGRAM_TIDS)
    assert not missing, f"DIAGRAM_TIDS 漏了: {missing}"


def test_template_hints_all_registered():
    """outline 的 TEMPLATE_HINTS 里每个 id 都必须真实存在（防拼写漂移）。"""
    from app.services.outline import TEMPLATE_HINTS
    reg = TemplateRegistry()
    ids = {t.id for t in reg.list()}
    missing = [h for h in TEMPLATE_HINTS if h not in ids]
    assert not missing, f"hint 指向不存在的模板: {missing}"


def test_svg_el_helper_accepts_text_arg():
    """JS 生成 SVG 的模板里，凡是 el("text", attrs, 内容) 三参调用，
    el 助手必须支持第三参（否则文字静默丢失——journey/radar 真实炸过）。"""
    root = _tpl_root()
    import re as _re
    bad = []
    for html_p in root.glob("*/template.html"):
        html = html_p.read_text(encoding="utf-8")
        if not _re.search(r'el\("text",\s*\{[^}]*\},\s*\w', html):
            continue  # 没有三参调用，不适用
        m = _re.search(r"function el\(tag,\s*attrs(,\s*\w+)?\)", html)
        if not m or not m.group(1):
            bad.append(html_p.parent.name)
    assert not bad, "el() 助手缺 text 参数:\n" + "\n".join(bad)


def test_no_bare_to_opacity_on_svg_element_refs():
    """HF 逐帧 seek 捕获下，对 SVG 元素引用裸 tl.to(el, {opacity: ...}) 不可靠
    （起点惰性捕获）——journey 真实炸过（标签全隐）。必须用 fromTo 给显式起点。
    规则：tl.to( 后面不是字符串选择器且目标含 opacity 的，一律拦截。"""
    root = _tpl_root()
    import re as _re
    bad = []
    for html_p in root.glob("*/template.html"):
        for m in _re.finditer(r"tl\.to\((?!\s*\"|\s*\{)([^,]{1,40}),\s*\{[^}]*opacity", html_p.read_text(encoding="utf-8")):
            bad.append(f"{html_p.parent.name}: tl.to({m.group(1).strip()}…")
    assert not bad, "SVG 元素引用上的裸 to()+opacity:\n" + "\n".join(bad)


def test_no_dict_method_shadowing_in_templates():
    """fields.items/keys/values/get 是 dict 方法——属性访问会拿到内建方法而不是字段。
    此坑已两次真实炸线（spectrum/checklist/rank-list），永远锁死。"""
    root = _tpl_root()
    import re as _re
    bad = []
    for html_p in root.glob("*/template.html"):
        html = html_p.read_text(encoding="utf-8")
        for m in _re.finditer(r"fields\.(items|keys|values|get)(?![\w(])", html):
            bad.append(f"{html_p.parent.name}: fields.{m.group(1)}")
    assert not bad, "模板里访问了 dict 方法名:\n" + "\n".join(bad)


def test_no_template_marks_user_only_unless_expected():
    reg = TemplateRegistry()
    uo = [t.id for t in reg.list() if t.user_only]
    assert uo == ["image-focus"], f"user_only 集合漂移: {uo}"
