"""模板路由：模板库清单（前端表单/选择器用）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import TemplateOut
from ..services.template_engine import TemplateRegistry, get_registry

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("", response_model=list[TemplateOut])
def list_templates():
    reg: TemplateRegistry = get_registry()
    out = []
    for t in reg.list():
        out.append(TemplateOut(
            id=t.id, name=t.name, description=t.description, role=t.role,
            aspects=t.aspects, motion=t.motion,
            min_duration_s=t.min_duration_s, max_duration_s=t.max_duration_s,
            fields=t.field_schemas(),  # type: ignore[arg-type]
            example=t.example,
            user=bool(t.user_only or _is_user(t.id)),
        ))
    return out


def _is_user(tid: str) -> bool:
    p = _tpl_dir(tid) / "meta.json"
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("user"))
    except Exception:
        return False


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: str):
    reg = get_registry()
    t = reg.list()
    for x in t:
        if x.id == template_id:
            return TemplateOut(
                id=x.id, name=x.name, description=x.description, role=x.role,
                aspects=x.aspects, motion=x.motion,
                min_duration_s=x.min_duration_s, max_duration_s=x.max_duration_s,
                fields=x.field_schemas(),  # type: ignore[arg-type]
                example=x.example,
            )
    raise HTTPException(404, "模板不存在")


# ── 用户自定义模板包导入/卸载 ────────────────────────────────
import json
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import UploadFile
from jsonschema import Draft202012Validator

from ..config import get_settings

_BANNED_HTML = re.compile(
    r"https?://|eval\(|new Function|fetch\(|XMLHttpRequest|WebSocket"
    r"|<script[^>]+src=|@import|import\(", re.I)


def _tpl_dir(tid: str) -> Path:
    return get_settings().templates_dir / tid


@router.post("/import")
def import_template_pack(file: UploadFile):
    """导入模板包 zip（meta.json + schema.json + template.html）。
    安全门：禁外链/禁动态代码执行；金测渲染示例字段不过即回滚。"""
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(422, "请上传 .zip 模板包")
    data = file.file.read()
    if len(data) > 512 * 1024:
        raise HTTPException(422, "模板包超过 512KB")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
                for name in z.namelist():
                    # 路径安全：拒绝绝对路径/穿越
                    if name.startswith("/") or ".." in name:
                        raise HTTPException(422, f"非法路径 {name}")
                z.extractall(tdp)
        except zipfile.BadZipFile:
            raise HTTPException(422, "不是合法 zip") from None

        root = tdp
        entries = list(tdp.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            root = entries[0]  # 允许 zip 内带一层目录
        need = ["meta.json", "schema.json", "template.html"]
        for n in need:
            if not (root / n).exists():
                raise HTTPException(422, f"模板包缺 {n}")

        try:
            meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
            schema = json.loads((root / "schema.json").read_text(encoding="utf-8"))
            html = (root / "template.html").read_text(encoding="utf-8")
        except Exception as e:
            raise HTTPException(422, f"文件解析失败: {e}") from None

        tid = str(meta.get("id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", tid):
            raise HTTPException(422, "id 需为小写字母/数字/连字符，3-32 位")
        if meta.get("role") not in ("hook", "body", "outro"):
            raise HTTPException(422, "role 只能是 hook/body/outro")
        aspects = meta.get("aspects") or ["16:9"]
        if not set(aspects) <= {"16:9", "9:16", "1:1"}:
            raise HTTPException(422, "aspects 只支持 16:9/9:16/1:1")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:
            raise HTTPException(422, f"schema 不合法: {str(e)[:160]}") from None
        props = set(schema.get("properties", {}))
        for k in meta.get("field_order") or []:
            if k not in props:
                raise HTTPException(422, f"field_order 字段 {k} 未在 schema 定义")
        bad = _BANNED_HTML.search(html)
        if bad:
            raise HTTPException(422, f"template.html 含禁止内容（外链/动态执行）: {bad.group(0)[:40]}")
        if "base_css" not in html or "__timelines" not in html:
            raise HTTPException(422, "模板须包含 {{ base_css }} 与 window.__timelines 注册（参考内置模板）")

        if _tpl_dir(tid).exists() and not (root / "meta.json").exists():
            raise HTTPException(409, f"模板 {tid} 已存在")

        # 落盘 + 金测渲染（失败回滚）
        dest = _tpl_dir(tid)
        import shutil
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for n in need:
            shutil.copy2(root / n, dest / n)
        meta["user"] = True
        (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        try:
            fresh = TemplateRegistry()
            t = fresh.get(tid)
            if t is None:
                raise ValueError("注册表未加载")
            if meta.get("example"):
                fresh.validate(tid, dict(meta["example"]))
            html_out = fresh.render_frame(
                tid, dict(meta.get("example") or {}), scene_id="99-selftest",
                duration_s=8.0, width=1920, height=1080,
                theme_tokens=None, style_name="warm")
            if "99-selftest" not in html_out:
                raise ValueError("渲染产物异常")
        except Exception as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(422, f"金测渲染未通过: {str(e)[:200]}") from None

    import app.services.template_engine as te
    te._registry = None  # 单例复位，下一请求加载新模板
    return {"ok": True, "id": tid, "name": meta.get("name")}


@router.delete("/{template_id}")
def delete_template(template_id: str):
    """卸载用户模板（内置模板不可删；被场景引用的不可删）。"""
    d = _tpl_dir(template_id)
    if not d.exists():
        raise HTTPException(404, "模板不存在")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("user"):
        raise HTTPException(403, "内置模板不可删除")
    from sqlalchemy import select

    from ..db import SessionLocal
    from ..models import Scene
    db = SessionLocal()
    try:
        used = db.scalars(select(Scene.id).where(
            Scene.template_id == template_id).limit(1)).first()
    finally:
        db.close()
    if used:
        raise HTTPException(409, "有场景正在使用该模板，先改掉再删")
    import shutil
    shutil.rmtree(d)
    import app.services.template_engine as te
    te._registry = None
    return {"ok": True}
