"""本地 Web UI：填表 → 出候选 → 确认 → 建单 → 付款链接 + 预览卡 → 轮询 → 正式卡。

只监听 127.0.0.1，不对外暴露。
付款永远由人在麦当劳自己的收银台完成，本服务不代付、不碰支付凭据。

    python scripts/webui.py            # 默认 http://127.0.0.1:8787
    python scripts/webui.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import McpAuthError, McpClient, McpError  # noqa: E402
from recommender import build_combos, load_menu  # noqa: E402
from card_render import render_card  # noqa: E402
import contacts  # noqa: E402

BE_TYPE_DELIVERY = 2
ORDER_TYPE_DELIVERY = 2

_client: McpClient | None = None


def get_client() -> McpClient:
    """复用同一个客户端，避免每次请求都重新握手。"""
    global _client
    if _client is None:
        _client = McpClient()
        _client.initialize()
    return _client


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


def as_list(x) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("list", "items", "records", "data", "stores"):
            v = x.get(k)
            if isinstance(v, list):
                return v
    return []


def first_key(obj, keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if obj.get(k):
            return obj[k]
    return None


class Session:
    """一次请客的全部中间状态。

    注意：这里只存 ID，不存任何收货信息明文。
    """

    def __init__(self) -> None:
        self.recipient = ""
        self.scene = "care"
        self.budget: Decimal | None = None
        self.message = ""
        self.address_id: str | None = None
        self.store_code: str | None = None
        self.be_code: str | None = None
        self.combos: list = []
        self.order_id: str = ""


def run_plan(c: McpClient, s: Session) -> dict:
    """跑完整链路到出候选。只读 + 一个地址创建，不建单。"""
    now = c.call_tool_data("now-time-info")
    s.recipient_name = getattr(s, "recipient_name", s.recipient)
    slot = slot_from(now.get("datetime", ""))
    s.slot = slot

    ct = contacts.get(s.recipient)
    if not ct:
        return {"error": f"通讯录里没有「{s.recipient}」，先跑 contacts.py add"}

    aid = ct.get("mcdAddressId")
    if not aid:
        res = c.call_tool_data(
            "delivery-create-address",
            {k: v for k, v in contacts.mcp_args(ct).items() if v is not None},
        )
        aid = first_key(res, ("addressId", "id", "address_id"))
        if not aid:
            return {"error": f"地址创建失败：{str(res)[:200]}"}
        contacts.set_address_id(s.recipient, str(aid))
    s.address_id = str(aid)

    stores = as_list(
        c.call_tool_data(
            "delivery-query-stores",
            {"addressId": aid, "beType": BE_TYPE_DELIVERY},
        )
    )
    if not stores:
        return {"error": "该地址无可配送门店"}

    open_stores = [x for x in stores if x.get("businessStatus") is True]
    if not open_stores:
        return {"error": "当前时段无营业门店，请稍后再试"}

    store = open_stores[0]
    s.store_code = store.get("storeCode")
    s.be_code = store.get("beCode")
    s.store_name = store.get("storeName")

    margs = {
        "storeCode": s.store_code,
        "beType": BE_TYPE_DELIVERY,
        "orderType": ORDER_TYPE_DELIVERY,
    }
    if s.be_code:
        margs["beCode"] = s.be_code
    menu_raw = c.call_tool_data("query-meals", margs)

    items = load_menu(menu_raw)
    s.combos = build_combos(items, scene=s.scene, budget=s.budget)

    if not s.combos:
        return {"error": "预算内没有可行组合"}

    return {
        "slot": slot,
        "store": {"code": s.store_code, "name": s.store_name},
        "combos": [
            {
                "index": i,
                "title": c.title(s.scene),
                "items": [{"code": x.code, "name": x.name} for x in c.items],
                "total": str(c.total),
                "reason": c.reason(s.scene),
            }
            for i, c in enumerate(s.combos)
        ],
    }


def preview_card(c: McpClient, s: Session) -> Path:
    combo = s.combos[0]
    return render_card(
        recipient=s.recipient,
        message=s.message or "记得吃饭",
        items=[(x.name, "") for x in combo.items],
        timestamp=datetime.now().strftime("%Y.%m.%d  %H:%M"),
        preview=True,
        out=str(ROOT / "output" / "card_preview.png"),
    )


def final_card(c: McpClient, s: Session) -> Path:
    combo = s.picked or s.combos[0]
    return render_card(
        recipient=s.recipient,
        message=s.message or "记得吃饭",
        items=[(x.name, "") for x in combo.items],
        timestamp=datetime.now().strftime("%Y.%m.%d  %H:%M"),
        eta=("订单尾号 " + s.order_id[-4:]) if s.order_id else "",
        preview=False,
        out=str(ROOT / "output" / "card_final.png"),
    )


def plan(recipient: str, budget: Decimal, message: str, scene: str) -> dict:
    c = McpClient()
    c.initialize()
    s = Session()
    s.recipient = recipient
    s.budget = budget
    s.message = message
    s.scene = scene
    return run_plan(c, s)


def order(recipient: str, budget: Decimal, message: str, scene: str, pick: int) -> dict:
    c = McpClient()
    c.initialize()
    s = Session()
    s.recipient = recipient
    s.budget = budget
    s.message = message
    s.scene = scene
    p = run_plan(c, s)
    if "error" in p:
        return p
    if not (0 <= pick < len(s.combos)):
        return {"error": f"候选只有 {len(s.combos)} 套"}
    s.picked = s.combos[pick]
    res = create_order(c, s)
    return {"order": res, "pick": pick}


def create_order(c: McpClient, s: Session) -> dict:
    combo = s.picked
    args = {
        "storeCode": s.store_code,
        "beType": BE_TYPE_DELIVERY,
        "orderType": ORDER_TYPE_DELIVERY,
        "addressId": s.address_id,
        "items": [{"productCode": x.code, "quantity": 1} for x in combo.items],
        "needTableware": True,
    }
    if s.be_code:
        args["beCode"] = s.be_code
    if s.message:
        args["remark"] = s.message[:50]
    return c.call_tool_data("create-order", args)
