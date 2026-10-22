"""抽取器/启发式/变体播种回归——每一条都对应本会话真实炸过的坑。"""
import pytest
from app.services import storyboard as sb
from app.services.storyboard import _EXTRACTORS, _choose_body_template, _NoFit, apply_variant


class TestExtractors:
    def test_myth(self):
        f = _EXTRACTORS["myth-buster"]("很多人以为隔夜水有毒致癌，其实盖好的隔夜水细菌根本不超标。")
        assert "致癌" in f["myth"] and "细菌" in f["truth"]

    def test_myth_nofit(self):
        with pytest.raises(_NoFit):
            _EXTRACTORS["myth-buster"]("今天天气不错，我们出去玩吧。")

    def test_qa(self):
        f = _EXTRACTORS["qa-card"]("为什么天空是蓝色的？因为空气分子散射了蓝光。")
        assert f["question"].endswith("？") or f["question"].endswith("?")
        assert "散射" in f["answer"]

    def test_kpi(self):
        f = _EXTRACTORS["chart-kpi"]("这个月完播2.4万次，涨粉860个，收益312元。")
        assert len(f["cards"]) >= 2

    def test_funnel_decreasing(self):
        assert _choose_body_template("1000人看到广告，200人点进来，40人加购，8人下单。") == "diagram-funnel"

    def test_radar_score_labels_use_dimension(self):
        """「通勤70分」标签必须取维度名而不是重复的「分」（曾因去重只剩 1 对）。"""
        f = _EXTRACTORS["diagram-radar"]("通勤打70分，房价45分，医疗85分，教育90分。")
        labels = [a.split("|")[0] for a in f["axes"]]
        assert len(labels) == len(set(labels)), f"标签重复: {labels}"
        assert "通勤" in labels[0]

    def test_gauge(self):
        f = _EXTRACTORS["diagram-gauge"]("这次体验综合打 82 分，非常不错。")
        assert f["value_pct"] == 82

    def test_venn(self):
        f = _EXTRACTORS["diagram-venn"]("理想职业既要你喜欢，又要你擅长，两者的交集才是答案。")
        assert f["left_label"] == "你喜欢" and f["right_label"] == "你擅长"

    def test_checklist_items_not_fragments(self):
        """残片过滤：连接词开头/助词结尾的子句不配做节点。"""
        f = _EXTRACTORS["checklist"]("查抵押看文书网，查违建看规划，查户籍去派出所，最后签字交定金。")
        for it in f["items"]:
            name = it.split("|")[0]
            assert name[0] not in "而是的了让使又也还但就才都", f"残片混入: {name}"

    def test_spectrum_and_balance_delegate_to_llm(self):
        with pytest.raises(_NoFit):
            _EXTRACTORS["diagram-spectrum"]("任何文本")
        with pytest.raises(_NoFit):
            _EXTRACTORS["diagram-balance"]("任何文本")


class TestHeuristics:
    def test_myth_heuristic(self):
        assert _choose_body_template("大家以为吃素一定健康，其实纯素容易缺B12。") == "myth-buster"

    def test_qa_heuristic_immediate_answer(self):
        """「？因为」紧跟也要命中（曾是 2 字下限 bug）。"""
        assert _choose_body_template("为什么天空是蓝色的？因为瑞利散射。") == "qa-card"

    def test_venn_tree_heuristics(self):
        assert _choose_body_template("喜欢与擅长的交集就是理想工作。") == "diagram-venn"
        assert _choose_body_template("AI分为符号主义，有连接主义，还有行为主义，共三大学派。") == "diagram-tree"


class TestApplyVariant:
    def test_seeded_and_stable(self):
        f1, f2 = {}, {}
        apply_variant("bullet-list", f1, "同一段旁白")
        apply_variant("bullet-list", f2, "同一段旁白")
        assert f1["variant"] == f2["variant"]
        assert 0 <= f1["variant"] <= 2

    def test_manual_wins(self):
        f = {"variant": 2}
        apply_variant("bullet-list", f, "任何旁白")
        assert f["variant"] == 2

    def test_layout_key_for_free_blocks(self):
        f = {}
        apply_variant("free-blocks", f, "内容")
        assert "layout" in f and 0 <= f["layout"] <= 3

    def test_untouched_for_diagrams(self):
        f = {}
        apply_variant("diagram-hub", f, "x")
        assert "variant" not in f, "图解模板不该有 variant 字段"


def test_variant_max_table_matches_schemas():
    """_VARIANT_MAX 的取值范围必须与各模板 schema 的 maximum 一致。"""
    from app.services.template_engine import TemplateRegistry
    reg = TemplateRegistry()
    for tid, vmax in sb._VARIANT_MAX.items():
        key = sb._VARIANT_KEY.get(tid, "variant")
        prop = reg.get(tid).schema["properties"].get(key)
        assert prop and prop.get("maximum") == vmax, f"{tid}.{key} 范围漂移"
