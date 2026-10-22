"""真机落地验证：用真实 MCP 接口跑完整链路。

跑到「待付款」为止 —— 不调用 create-order，不花一分钱。

    python scripts/verify.py                # 有地址就走外送
    python scripts/verify.py --city 上海     # 没地址就降级走"到店取餐"验证

验证 7 步：
    Token → 握手 → 时段 → 地址 → 门店 → 菜单 → 组单 + 卡片 PNG
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import McpAuthError, McpClient, McpError  # noqa: E402
from recommender import build_combos, load_menu  # noqa: E402
from card_render import render_card  # noqa: E402

BE_TYPE_DELIVERY = 2
BE_TYPE_PICKUP = 1
ORDER_TYPE_DELIVERY = 2
ORDER_TYPE_PICKUP = 1


def slot_from(dt: str) -> str:
    try:
        hour = int(dt[11:13])
    except (ValueError, IndexError):
        return "dinner"
    if 5 <= hour < 10:
        return "breakfast"
    if 10 <= hour < 14:
        return "lunch"
    if 14 <= hour < 21:
        return "dinner"
    return "latenight"


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def main() -> int:
    ap = argparse.ArgumentParser(description="McTreat 真机落地验证")
    ap.add_argument("--city", help="无配送地址时，用该城市跑「到店取餐」降级链路")
    args = ap.parse_args()

    print("=" * 60)
    print("  McTreat 真机落地验证")
    print("=" * 60)

    # 1) Token
    try:
        c = McpClient()
    except McpAuthError as e:
        print("\n[1/7] Token   ✗")
        print(e)
        return 1
    print("\n[1/7] Token   ✓  已读取（不展示内容）")

    # 2) 握手
    try:
        c.initialize()
        state = "无状态会话" if not c.session_id else f"会话 {c.session_id[:8]}..."
        print(f"[2/7] 连接    ✓  {c.protocol_version}，{state}")
    except McpError as e:
        print(f"[2/7] 连接    ✗  {e}")
        return 1

    # 3) 时间
    try:
        now = c.call_tool_data("now-time-info")
        slot = slot_from(now.get("datetime", ""))
        print(f"[3/7] 时间    ✓  {now.get('formatted')}  时段={slot}")
    except McpError as e:
        print(f"[3/7] 时间    ✗  {e}")
        return 1

    # 4) 地址（有则走外送，无则降级到店取餐）
    addrs = _as_list(c.call_tool_data("delivery-query-addresses"))
    address_id = addrs[0].get("addressId") or addrs[0].get("id") if addrs else None
    print(f"[4/7] 地址    {'✓' if address_id else '—'}  共 {len(addrs)} 条"
          + ("" if address_id else "（无地址，转降级链路）"))

    if address_id:
        stores = _as_list(c.call_tool_data(
            "delivery-query-stores", {"addressId": address_id, "beType": BE_TYPE_DELIVERY}))
        be_type, order_type = BE_TYPE_DELIVERY, ORDER_TYPE_DELIVERY
        mode = "外送"
    else:
        if not args.city:
            print("\n未指定 --city，无法继续验证门店/菜单链路。")
            print("用法：python scripts/verify.py --city 上海")
            return 0
        stores = _as_list(c.call_tool_data(
            "query-nearby-stores",
            {"beType": BE_TYPE_PICKUP, "searchType": 2,
             "city": args.city, "keyword": args.city}))
        be_type, order_type = BE_TYPE_PICKUP, ORDER_TYPE_PICKUP
        mode = "到店取餐（降级验证）"

    open_stores = [s for s in stores if s.get("businessStatus") is True]
    print(f"[5/7] 门店    {'✓' if open_stores else '×'}  可配送 {len(stores)} 家，营业中 {len(open_stores)} 家")

    if not open_stores:
        print("\n当前时段无营业门店。换个时间再跑，或到店取餐分支也依赖营业状态。")
        return 0

    store = open_stores[0]
    store_code = store.get("storeCode")
    be_code = store.get("beCode")

    try:
        args = {"storeCode": store_code, "beType": be_type, "orderType": order_type}
        if be_code:
            args["beCode"] = be_code
        menu_raw = c.call_tool_data("query-meals", args)
        n_menu = len(menu_raw.get("meals", {})) if isinstance(menu_raw, dict) else 0
        n_cat = len(menu_raw.get("categories", [])) if isinstance(menu_raw, dict) else 0
        print(f"[6/7] 菜单    {'✓' if n_menu else '×'}  {n_menu} 个菜品 / {n_cat} 个分类")
    except McpError as e:
        print(f"[6/7] 菜单    ✗  {e}")
        return 1

    if not n_menu:
        return 1

    items = load_menu(menu_raw)
    combos = build_combos(items, scene="overtime", budget=Decimal("40"))
    if not combos:
        print("[7/7] 组单    ×  预算内没有可行组合")
        return 1

    out = render_card(
        recipient="联调验证",
        message="这是一次真实的端到端验证",
        items=[(i.name, "") for i in combos[0].items],
        timestamp=datetime.now().strftime("%Y.%m.%d  %H:%M"),
        preview=True,
        out=str(ROOT / "output" / "verify_card.png"),
    )
    print(f"[7/7] 卡片    ✓  已渲染 verify_card.png（{out.stat().st_size} bytes）")

    print("\n--- 本次真实组单结果（加班场景 / 预算 ¥40）---")
    for i, c in enumerate(combos, 1):
        names = " + ".join(x.name for x in c.items)
        print(f"  {i}. {names}")
        print(f"     合计 ¥{c.total}   体面度 {c.decency('overtime')}")

    print("\n" + "=" * 60)
    print("  链路验证通过")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
