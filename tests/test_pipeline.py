"""端到端链路离线自测：菜单 fixture → 组单 → 卡片 PNG。

整个测试不联网、不消耗调用额度，可以随时重跑。

    python -m tests.test_pipeline
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from recommender import build_combos, load_menu  # noqa: E402
from card_render import render_card  # noqa: E402
import treat  # noqa: E402

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


def png_size(p: Path):
    d = p.read_bytes()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")


def main() -> None:
    raw = MENU.read_text(encoding="utf-8")
    items = load_menu(__import__("json").loads(raw))

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        combos = build_combos(items, scene="overtime", budget=Decimal("40"))

        print("\n1) 组单产出")
        check("3 套候选", len(combos) == 3, f"实际 {len(combos)}")
        check("主食各不相同", len({c.main.name for c in combos}) == 3)

        print("\n2) 渲染预览卡")
        c = combos[0]
        prev = render_card(
            recipient="张伟",
            message="改完 bug 记得吃，别熬太晚",
            items=[(i.name, "") for i in c.items],
            timestamp="2026.10.22  22:30",
            preview=True,
            out=str(out / "preview.png"),
        )
        check("文件生成", prev.exists())
        check("尺寸 1080x1440", png_size(prev) == (1080, 1440), str(png_size(prev)))

        print("\n3) 渲染正式卡")
        fin = render_card(
            recipient="张伟",
            message="改完 bug 记得吃，别熬太晚",
            items=[(i.name, "") for i in c.items],
            timestamp="2026.10.22  22:30",
            eta="订单尾号 3721 · 预计 23:10 前送达",
            preview=False,
            out=str(out / "final.png"),
        )
        check("文件生成", fin.exists())
        check("尺寸 1080x1440", png_size(fin) == (1080, 1440), str(png_size(fin)))

    print("\n4) 下单闸门")
    from treat import TreatFlow, TreatSafetyError

    flow = TreatFlow.__new__(TreatFlow)      # 不触发 __init__，避免需要 Token
    flow.c = None
    flow.s = treat.TreatSession()
    flow.s.picked = build_combos(items, scene="overtime", budget=Decimal("40"))[0]

    try:
        flow.create_order(confirmed=False)
        check("未确认时应拒绝下单", False, "居然放行了")
    except TreatSafetyError:
        check("未确认时拒绝下单", True)

    print("\n" + "=" * 46)
    print(f"  通过 {passed} 项，失败 {failed} 项")
    print("=" * 46)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
