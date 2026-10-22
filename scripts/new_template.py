#!/usr/bin/env python3
"""脚手架：一键生成模板三件套骨架。

用法：
    python scripts/new_template.py my-new-template "我的新模板" "一句话描述"

生成 templates/my-new-template/{meta.json, schema.json, template.html}，
带最小可用结构（含 hf-kicker/headline/note + fromTo 动画 + AM 出场），
然后照 CONTRIBUTING.md 的五步把它接进系统。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    tid, name, desc = sys.argv[1], sys.argv[2], sys.argv[3]
    d = ROOT / "templates" / tid
    if d.exists():
        sys.exit(f"已存在: {d}")
    d.mkdir(parents=True)
    d.joinpath("meta.json").write_text(json.dumps({
        "id": tid, "name": name, "role": "body",
        "description": desc,
        "aspects": ["16:9", "9:16", "1:1"], "motion": "fade-rise",
        "min_duration_s": 5, "max_duration_s": 16,
        "field_order": ["kicker", "headline", "note"],
        "example": {"kicker": "示例标签", "headline": "示例标题", "note": "示例注解"},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    d.joinpath("schema.json").write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "kicker": {"type": "string", "minLength": 2, "maxLength": 16, "description": "左上角标签"},
            "headline": {"type": "string", "minLength": 2, "maxLength": 16, "description": "标题"},
            "note": {"type": "string", "minLength": 0, "maxLength": 22, "description": "注解（可留空）"},
        },
        "required": ["kicker", "headline"],
        "additionalProperties": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    d.joinpath("template.html").write_text(
        TEMPLATE.replace("__TID__", tid), encoding="utf-8")
    print(f"✓ 已生成 {d}")
    print("下一步：照 CONTRIBUTING.md「添加一个模板」完成登记。")


TEMPLATE = '''<!doctype html>
<html lang="zh-CN">
  <head><meta charset="UTF-8" /><title>{{ scene_id }}</title></head>
  <body>
    <template>
      <style>
        {{ base_css }}
        #f-{{ scene_id }} .headline { font-family: var(--font-display); font-size: 6.2cqmin; margin-top: 3cqh; }
        #f-{{ scene_id }} .note { margin-top: 1.4cqh; font-family: var(--font-mono); font-size: 2cqmin; color: var(--ink-faint); letter-spacing: 0.06em; }
      </style>
      <div id="f-{{ scene_id }}" data-composition-id="{{ scene_id }}"
        data-width="{{ width }}" data-height="{{ height }}" data-duration="{{ duration }}">
        <div class="clip hf-ground" id="s-{{ scene_id }}-ground" data-start="0" data-duration="{{ duration }}"></div>
        <div class="hf-safe">
          <div class="hf-kicker" id="s-{{ scene_id }}-kicker"><span class="hf-spike"></span> {{ fields.kicker }}</div>
          <div class="headline" id="s-{{ scene_id }}-headline">{{ fields.headline }}</div>
          {% if fields.get('note') %}<div class="note" id="s-{{ scene_id }}-note">{{ fields.get('note') }}</div>{% endif %}
        </div>
      </div>
      <script>
        (function () {
          var SID = "s-{{ scene_id }}";
          var D = {{ duration }};
          var AD = window.__AD || { flip: 1, pace: 1 };
          var tl = gsap.timeline({ paused: true });

          tl.fromTo("#" + SID + "-kicker", { opacity: 0, y: -14 },
            { opacity: 1, y: 0, duration: 0.5, ease: "power3.out" }, 0.1);
          tl.fromTo("#" + SID + "-headline", { opacity: 0, y: 26 },
            { opacity: 1, y: 0, duration: 0.7, ease: "power3.out" }, 0.28);
          if (document.getElementById(SID + "-note")) {
            AM.wipe(tl, "#" + SID + "-note", Math.min(1.6, D * 0.5), 0.6, "bottom");
          }

          {% if not is_last %}AM.exitSoft(tl, "#" + SID + "-kicker, #" + SID + "-headline", D);{% endif %}
          tl.to({}, { duration: D }, 0);
          window.__timelines = window.__timelines || {};
          window.__timelines["{{ scene_id }}"] = tl;
        })();
      </script>
    </template>
  </body>
</html>
'''

if __name__ == "__main__":
    main()
