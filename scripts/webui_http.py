"""McTreat 本地 Web UI 的 HTTP 层。

业务逻辑在 webui.py（跑流程、建单、渲染卡片），这里只负责起服务和路由。

    python scripts/webui_http.py              # http://127.0.0.1:8787
    python scripts/webui_http.py --port 9000

只监听 127.0.0.1，不对外暴露。
付款永远由人在麦当劳自己的收银台完成，本服务不代付、不碰支付凭据。
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from webui import (  # noqa: E402
    Session,
    create_order,
    final_card,
    first_key,
    get_client,
    preview_card,
    run_plan,
)
from mcp_client import McpAuthError, McpError  # noqa: E402

WEB = ROOT / "web"

# order_id -> {"session": Session, "client": McpClient}
PENDING: dict[str, dict] = {}

# 订单状态字段名官方未文档化，做尽力而为的匹配
PAID_WORDS = ("已支付", "已付款", "支付成功", "待出餐", "出餐中", "制作中", "配送中")


def _mk_session(body: dict) -> Session:
    s = Session()
    s.recipient = (body.get("recipient") or "").strip()
    s.scene = body.get("scene") or "care"
    s.message = (body.get("message") or "").strip()
    b = body.get("budget")
    s.budget = Decimal(str(b)) if b else None
    s.picked = None
    s.final_done = False
    return s


def _status_text(data) -> str:
    if isinstance(data, dict):
        for k in ("orderStatusName", "orderStatus", "status", "payStatus", "state"):
            v = data.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, (int, bool)):
                return str(v)
    return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _file(self, p: Path, ctype: str) -> None:
        if not p.exists():
            self._json({"error": "文件不存在"}, 404)
            return
        self._send(p.read_bytes(), ctype)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            self._file(WEB / "index.html", "text/html; charset=utf-8")
        elif p == "/api/status":
            self._status(parse_qs(u.query).get("orderId", [""])[0])
        elif p.startswith("/output/"):
            self._file(ROOT / p.lstrip("/"), "image/png")
        else:
            self._json({"error": "未找到"}, 404)

    def do_POST(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            self._json({"error": "JSON 解析失败"}, 400)
            return
        p = urlparse(self.path).path
        if p == "/api/plan":
            self._api_plan(body)
        elif p == "/api/order":
            self._api_order(body)
        else:
            self._json({"error": "未找到"}, 404)

    def _api_plan(self, body: dict) -> None:
        """只出候选，不建单。可以随便点。"""
        try:
            c = get_client()
            s = _mk_session(body)
            r = run_plan(c, s)
            if "error" in r:
                self._json(r, 400)
                return
            preview_card(c, s)
            self._json(r)
        except (McpAuthError, McpError) as e:
            self._json({"error": str(e)}, 500)

    def _api_order(self, body: dict) -> None:
        """真的建单。"""
        try:
            c = get_client()
            s = _mk_session(body)
            r = run_plan(c, s)
            if "error" in r:
                self._json(r, 400)
                return
            pick = int(body.get("pick") or 0)
            if not (0 <= pick < len(s.combos)):
                self._json({"error": f"候选只有 {len(s.combos)} 套"}, 400)
                return
            s.picked = s.combos[pick]
            preview_card(c, s)
            res = create_order(c, s)
            oid = first_key(res, ("orderId", "id", "orderNo")) or ""
            s.order_id = str(oid)
            PENDING[str(oid)] = {"session": s, "client": c}
            self._json({
                "orderId": str(oid),
                "payUrl": (res.get("payUrl") or res.get("payLink")
                           or res.get("url") or ""),
                "raw": res,
                "preview": "card_preview.png",
            })
        except (McpAuthError, McpError) as e:
            self._json({"error": str(e)}, 500)

    def _status(self, oid: str) -> None:
        rec = PENDING.get(oid)
        if not rec:
            self._json({"error": "本地没有这个订单的记录"}, 404)
            return
        c, s = rec["client"], rec["session"]
        try:
            d = c.call_tool_data("query-order", {"orderId": oid})
        except McpError as e:
            self._json({"error": str(e)}, 500)
            return
        st = _status_text(d)
        paid = any(w in st for w in PAID_WORDS)
        if paid and not s.final_done:
            final_card(c, s)
            s.final_done = True
        self._json({
            "orderId": oid,
            "status": st,
            "paid": paid,
            "final": "card_final.png" if s.final_done else None,
        })


def serve(port: int, host: str = "127.0.0.1") -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"麦麦请客卡 Web UI 已启动: http://{host}:{port}")
    print("按 Ctrl+C 停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="McTreat 本地 Web UI")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    serve(a.port, a.host)


if __name__ == "__main__":
    main()
