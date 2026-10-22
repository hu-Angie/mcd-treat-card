"""组单引擎离线自测 —— 用真实菜单样本跑，不消耗任何调用额度。

    python -m tests.test_recommender
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from recommender import (  # noqa: E402
    ROLE_DRINK,
    ROLE_MAIN,
    ROLE_SIDE,
    build_combos,
    load_menu,
)

MENU = ROOT / "fixtures" / "menu_sample.json"

passed = failed = 0


def check(label: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  {detail}")


def main() -> None:
    data = json.loads(MENU.read_text(encoding="utf-8"))
    items = load_menu(data)

    print("\n1) 菜单加载")
    check("菜品非空", len(items) > 0, f"实际 {len(items)}")
    check("价格都是 Decimal", all(isinstance(i.price, Decimal) for i in items))
    check("都有 code 和 name", all(i.code and i.name for i in items))

    print("\n2) 角色分类")
    mains = [i for i in items if i.role == ROLE_MAIN]
    sides = [i for i in items if i.role == ROLE_SIDE]
    drinks = [i for i in items if i.role == ROLE_DRINK]
    check("有主食", len(mains) > 0, f"{len(mains)}")
    check("有小食", len(sides) > 0, f"{len(sides)}")
    check("有饮料", len(drinks) > 0, f"{len(drinks)}")

    print("\n3) 排除不能送的东西")
    edible_names = "".join(i.name for i in items if i.edible)
    check("没有蘸酱混入", "风味酱" not in edible_names, "蘸酱不算一份菜")
    check("没有周边玩具混入", "痒痒挠" not in edible_names, "玩具不能吃")

    print("\n4) 组单结果（加班场景，预算 ¥40）")
    combos = build_combos(items, scene="overtime", budget=Decimal("40"))
    check("返回 3 套", len(combos) == 3, f"实际 {len(combos)}")
    check("每套都有主食", all(c.main for c in combos))
    check("每套都不超预算", all(c.total <= Decimal("40") for c in combos))
    check("三套主食互不相同",
          len({c.main.code for c in combos}) == len(combos))
    for c in combos:
        print(f"    [{c.kind}] {c.title('overtime')}  ¥{c.total}")
        print("       " + " + ".join(i.name for i in c.items))
        print(f"       {c.reason('overtime')}")

    print("\n5) 预算约束")
    tight = build_combos(items, scene="care", budget=Decimal("20"))
    check("紧预算下也不超支", all(c.total <= Decimal("20") for c in tight),
          f"{[str(c.total) for c in tight]}")
    check("紧预算下仍返回候选", len(tight) > 0)

    print("\n6) 场景影响主食选择")
    picks = {}
    for scene in ("overtime", "longdistance", "apology", "care"):
        cs = build_combos(items, scene=scene, budget=Decimal("60"))
        picks[scene] = cs[0].main.name if cs else None
    for s, n in picks.items():
        print(f"    {s:14s} -> {n}")
    check("不同场景推荐不完全雷同",
          len(set(picks.values())) > 1, str(picks))


    print("\n" + "=" * 46)
    print(f"  通过 {passed} 项，失败 {failed} 项")
    print("=" * 46)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
