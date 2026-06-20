"""概念垃圾黑名单 _is_junk_concept 单测（剔非题材、保真题材）。

零依赖，可直接运行：python -m tests.test_concept_filter
"""

from __future__ import annotations

from app.factors.theme_wide import _is_junk_concept


def test_junk_concepts_removed() -> None:
    for junk in ["ST板块", "上证180成份股", "上证50样本股", "沪深300样本股",
                 "证金持股", "融资融券", "沪股通", "MSCI中国", "昨日涨停", "破净股"]:
        assert _is_junk_concept(junk), f"应剔除：{junk}"


def test_real_themes_kept() -> None:
    for theme in ["共封装光学(CPO)", "存储芯片", "PCB概念", "固态电池",
                  "智能穿戴", "人形机器人", "AI PC", "柔性屏(折叠屏)", "白酒"]:
        assert not _is_junk_concept(theme), f"误杀真题材：{theme}"


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
