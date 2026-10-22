"""McTreat 主流程编排。

阶段：
  IDLE → COLLECT → MENU → RECOMMEND → WAIT_PICK
      → PREVIEW_CARD → WAIT_CONFIRM → ORDERED
      → CONFIRM_ORDER → FINAL_CARD → DONE

★ 唯一的写操作 create-order 位于 WAIT_CONFIRM 闸门之后，
  未通过闸门绝不调用。付款永远由用户自己完成。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import McpAuthError, McpClient, McpError  # noqa: E402
from recommender import build_combos, load_menu  # noqa: E402
from card_render import render_card  # noqa: E402
import contacts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

BE_TYPE_DELIVERY = 2
ORDER_TYPE_DELIVERY = 2


class TreatSafetyError(Exception):
    """在用户显式确认前尝试下单。"""


def time_slot_from(data: dict) -> str:
    """从 now-time-info 的 data 判断时段。"""
    dt = data.get("datetime") or ""
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


@dataclass
class TreatSession:
    recipient: str = ""
    address_id: str = ""
    store_code: str = ""
    be_code: str = ""
    scene: str = "overtime"
    budget: Decimal | None = None
    message: str = ""
    time_slot: str = "dinner"
    menu_raw: dict | None = None
    combos: list = field(default_factory=list)
    picked: object | None = None
    order_id: str = ""
    stage: str = "IDLE"


class TreatFlow:
    """把 MCP 调用串成一次完整的请客。

    所有金额相关的判断都交给 calculate-price，本地只做单位换算与格式化，
    绝不在本地推算价格——算出来的数比实付便宜就会彻底失去信任。
    """

    def __init__(self, client: McpClient | None = None):
        self.c = client or McpClient()
        self.s = TreatSession()

    # ---- COLLECT ----

    def collect(self, recipient: str, scene: str, budget: Decimal | None, message: str) -> dict:
        now = self.c.call_tool_data("now-time-info")
        self.s.recipient = recipient
        self.s.scene = scene
        self.s.budget = budget
        self.s.message = message
        self.s.time_slot = time_slot_from(now)
        self.s.stage = "MENU"
        return now

    def addresses(self):
        return self.c.call_tool_data("delivery-query-addresses")

    def resolve_recipient(self, name: str) -> str | None:
        """人名 → 麦当劳账户里的地址 ID。

        已建过就直接复用通讯录里记着的 mcdAddressId，不重复创建；
        没建过才调 delivery-create-address，并把拿到的 ID 回填。
        """
        ct = contacts.get(name)
        if not ct:
            return None
        if ct.get("mcdAddressId"):
            return ct["mcdAddressId"]

        args = {k: v for k, v in contacts.mcp_args(ct).items() if v is not None}
        res = self.c.call_tool_data("delivery-create-address", args)
        aid = _first_key(res, ("addressId", "id", "address_id"))
        if aid:
            contacts.set_address_id(name, str(aid))
            return str(aid)
        return None

    def create_address(
        self,
        address: str,
        address_detail: str,
        city: str,
        contact_name: str,
        phone: str,
        gender: str | None = None,
    ):
        args = {
            "address": address,
            "addressDetail": address_detail,
            "city": city,
            "contactName": contact_name,
            "phone": phone,
        }
        if gender:
            args["gender"] = gender
        return self.c.call_tool_data("delivery-create-address", args)

    # ---- MENU ----

    def stores(self, address_id: str) -> list[dict]:
        data = self.c.call_tool_data(
            "delivery-query-stores",
            {"addressId": address_id, "beType": BE_TYPE_DELIVERY},
        )
        return _as_list(data)

    def pick_store(self, stores: list[dict]) -> dict | None:
        """优先营业中的门店；没有则退回第一家。"""
        for s in stores:
            if s.get("businessStatus") is True:
                return s
        return stores[0] if stores else None

    def menu(self, store_code: str, be_code: str | None = None) -> dict:
        args = {
            "storeCode": store_code,
            "beType": BE_TYPE_DELIVERY,
            "orderType": ORDER_TYPE_DELIVERY,
        }
        if be_code:
            args["beCode"] = be_code
        return self.c.call_tool_data("query-meals", args)

    def store_coupons(self, store_code: str, be_code: str | None = None):
        args = {
            "storeCode": store_code,
            "beType": BE_TYPE_DELIVERY,
            "orderType": ORDER_TYPE_DELIVERY,
        }
        if be_code:
            args["beCode"] = be_code
        return self.c.call_tool_data("query-store-coupons", args)

    def my_coupons(self, page: str = "1", page_size: str = "200"):
        return self.c.call_tool_data(
            "query-my-coupons", {"page": page, "pageSize": page_size}
        )

    # ---- RECOMMEND ----

    def recommend(self, menu_raw: dict) -> list:
        self.s.menu_raw = menu_raw
        self.s.combos = build_combos(
            load_menu(menu_raw),
            scene=self.s.scene,
            budget=self.s.budget,
        )
        self.s.stage = "WAIT_PICK"
        return self.s.combos

    def pick(self, idx: int):
        if not (0 <= idx < len(self.s.combos)):
            raise IndexError(f"候选只有 {len(self.s.combos)} 套")
        self.s.picked = self.s.combos[idx]
        self.s.stage = "PREVIEW_CARD"
        return self.s.picked

    def preview(self, out: str = "output/card_preview.png") -> Path:
        if not self.s.picked:
            raise TreatSafetyError("还没选好要送哪一套")
        c = self.s.picked
        return render_card(
            recipient=self.s.recipient,
            message=self.s.message,
            items=[(i.name, "") for i in c.items],
            timestamp=_now_str(),
            preview=True,
            out=out,
        )

    # ---- ORDER（★ 闸门）----

    def create_order(self, confirmed: bool = False):
        """建单。★ 必须 confirmed=True 才执行。"""
        if not confirmed:
            raise TreatSafetyError(
                "下单需要用户明确确认。请在用户说出「确认」「下单」等明确指令后再调用。"
            )
        c = self.s.picked
        if not c:
            raise TreatSafetyError("还没有选中的组合，无法下单")
        if not self.s.address_id:
            raise TreatSafetyError("缺少收货地址")

        args = {
            "storeCode": self.s.store_code,
            "beType": BE_TYPE_DELIVERY,
            "orderType": ORDER_TYPE_DELIVERY,
            "addressId": self.s.address_id,
            "items": [{"productCode": i.code, "quantity": 1} for i in c.items],
            "needTableware": True,
        }
        if self.s.be_code:
            args["beCode"] = self.s.be_code
        if self.s.message:
            args["remark"] = self.s.message[:50]

        res = self.c.call_tool_data("create-order", args)
        self.s.order_id = _first_key(res, ("orderId", "id", "orderNo")) or ""
        self.s.stage = "ORDERED"
        return res

    def query_order(self):
        if not self.s.order_id:
            raise TreatSafetyError("还没有订单号")
        return self.c.call_tool_data("query-order", {"orderId": self.s.order_id})

    def final_card(self, out: str = "output/card_final.png") -> Path:
        c = self.s.picked
        return render_card(
            recipient=self.s.recipient,
            message=self.s.message,
            items=[(i.name, "") for i in c.items],
            timestamp=_now_str(),
            eta=f"订单尾号 {self.s.order_id[-4:]}" if self.s.order_id else "",
            preview=False,
            out=out,
        )


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("list", "items", "records", "data", "stores"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _first_key(obj, keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if obj.get(k):
            return obj[k]
    return None


def _now_str() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y.%m.%d  %H:%M")


def _run(flow, args) -> None:
    """跑完整链路：时间 → 地址 → 门店 → 菜单 → 组单 → 预览卡。

    默认停在「待付款」，不下单。
    """
    # 1) 时间
    now = flow.collect(args.recipient, args.scene, args.budget, args.message)
    print(f"[1/6] 时间    ✓  {now.get('formatted')}  时段={flow.s.time_slot}")

    # 2) 地址
    addrs = _as_list(flow.addresses())
    if not addrs:
        print("[2/6] 地址    ×  账户无配送地址")
        print("\n先创建地址：")
        print("  python scripts/probe.py call delivery-create-address "
              '\'{"address":"..","addressDetail":"..","city":"..",'
              '"contactName":"..","phone":".."}\'')
        return
    addr = addrs[0]
    addr_id = addr.get("addressId") or addr.get("id")
    print(f"[2/6] 地址    ✓  id={str(addr_id)[:12]}")

    # 3) 门店
    stores = flow.stores(addr_id)
    open_stores = [s for s in stores if s.get("businessStatus") is True]
    print(f"[3/6] 门店    {'✓' if open_stores else '×'}  "
          f"可配送 {len(stores)} 家，营业中 {len(open_stores)} 家")
    if not open_stores:
        print("\n当前时段无营业门店，换个时间再跑。")
        return

    store = open_stores[0]
    sc, bc = store.get("storeCode"), store.get("beCode")

    # 4) 菜单
    menu_raw = flow.menu(sc, bc)
    n_menu = len(menu_raw.get("meals", {})) if isinstance(menu_raw, dict) else 0
    n_cat = len(menu_raw.get("categories", [])) if isinstance(menu_raw, dict) else 0
    print(f"[4/6] 菜单    {'✓' if n_menu else '×'}  {n_menu} 个菜品 / {n_cat} 个分类")
    if not n_menu:
        return

    # 5) 组单
    items = load_menu(menu_raw)
    combos = build_combos(items, scene=args.scene, budget=args.budget)
    if not combos:
        print("[5/6] 组单    ×  预算内无可行组合")
        return
    print(f"[5/6] 组单    ✓  {len(combos)} 套候选")

    # 6) 卡片
    out = render_card(
        recipient=args.recipient,
        message=args.message or "记得吃饭",
        items=[(i.name, "") for i in combos[0].items],
        timestamp=_now_str(),
        preview=True,
        out=str(ROOT / "output" / "verify_card.png"),
    )
    print(f"[6/6] 卡片    ✓  {out}")
    print("\n★ 全流程未触发 create-order。真下单请加 --confirm。")


def main() -> None:
    ap = argparse.ArgumentParser(description="McTreat 请客卡流程")
    ap.add_argument("cmd", choices=["time", "addr", "stores", "menu", "coupons", "run", "order"])
    ap.add_argument("--city")
    ap.add_argument("--address-id")
    ap.add_argument("--store-code")
    ap.add_argument("--be-code")
    ap.add_argument("--recipient", default="朋友")
    ap.add_argument("--scene", default="overtime",
                    choices=["overtime", "longdistance", "apology", "care"])
    ap.add_argument("--budget")
    ap.add_argument("--message", default="")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    budget = Decimal(args.budget) if args.budget else None
    flow = TreatFlow()

    try:
        if args.cmd == "run":
            _run(flow, args)
        elif args.cmd == "order":
            _order(flow, args)
        elif args.cmd == "time":
            print(json.dumps(flow.collect("测试", "overtime", None, ""), ensure_ascii=False)[:500])
        elif args.cmd == "addr":
            print(str(flow.addresses())[:800])
        elif args.cmd == "stores":
            print(json.dumps(flow.stores(args.address_id), ensure_ascii=False)[:800])
        elif args.cmd == "menu":
            print(str(flow.menu(args.store_code, args.be_code))[:800])
        elif args.cmd == "coupons":
            print(str(flow.my_coupons())[:800])
    except McpError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


def _order(flow, args) -> None:
    """真实建单。★ 必须带 --confirm。"""
    if not args.confirm:
        print("[中止] 真实下单必须显式加 --confirm。")
        print("  python scripts/treat.py order --recipient 张伟 --store-code X --be-code Y --address-id Z --confirm")
        sys.exit(2)
    res = flow.create_order(confirmed=True)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
