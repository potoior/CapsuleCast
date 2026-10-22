"""测试基座：路径注入 + 隔离的临时工作目录（不碰真实 work_dir/缓存）。"""
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


@pytest.fixture()
def iso_settings(tmp_path, monkeypatch):
    """把 work_dir 指到临时目录，避免测试污染 SFX/BGM/分段缓存。"""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "work_dir", str(tmp_path / "work"))
    (tmp_path / "work").mkdir(exist_ok=True)
    return s


@pytest.fixture()
def reg():
    from app.services.template_engine import TemplateRegistry

    return TemplateRegistry()
