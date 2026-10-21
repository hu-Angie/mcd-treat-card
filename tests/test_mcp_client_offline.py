"""传输层离线自测 —— 不需要 Token，不需要网络。

mcp_client.py 是整个项目的基础，但官方接口要等 Token 才能连。
所以在拿到 Token 之前，先用一个本地假 MCP 服务把协议层验一遍：

  - initialize 握手与 Mcp-Session-Id 透传
  - tools/list 解析
  - tools/call 返回 JSON 时自动解析成 dict
  - tools/call 返回 TOON 文本时保留原样
  - SSE 流式响应解析
  - 401 立即抛 McpAuthError 且不重试
  - 429 指数退避后重试成功

运行：
    python -m tests.test_mcp_client_offline
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_client import (  # noqa: E402
    McpAuthError,
    McpClient,
    McpError,
    McpRateLimitError,
)

STATE = {
    "auth_fail": False,
    "auth_fail_403": False,
    "biz_error_200": False,
    "rate_limit_once": False,
    "rate_limited": False,
    "use_sse": False,
    "use_sse_bad": False,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if STATE["auth_fail"]:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return

        if STATE["auth_fail_403"]:
            # 实测：mcp.mcd.cn 前置网关返回 403 + 麦当劳业务格式，不是 JSON-RPC
            data = json.dumps({"code": "0001", "msg": "系统错误", "data": None}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if STATE["biz_error_200"]:
            # HTTP 200 但 body 是业务错误格式
            data = json.dumps({"code": "9999", "msg": "门店不存在", "data": None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if STATE["rate_limit_once"] and not STATE["rate_limited"]:
            STATE["rate_limited"] = True
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b"too many requests")
            return

        method = body.get("method")

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "fake-mcd", "version": "1.0.0"},
            }
            self._reply({"jsonrpc": "2.0", "id": body.get("id"), "result": result},
                        session_id="sess-fake-123")

        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()

        elif method == "tools/list":
            result = {"tools": [
                {"name": "now-time-info", "description": "time",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "query-meals", "description": "menu",
                 "inputSchema": {"type": "object",
                                 "properties": {"storeCode": {"type": "string"}},
                                 "required": ["storeCode"]}},
            ]}
            self._reply({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

        elif method == "tools/call":
            params = body.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})

            if name == "probe-session":
                text = json.dumps({"session": self.headers.get("Mcp-Session-Id")})
            elif name == "toon_tool":
                text = "{productName,nutritionDescription}:\n麦香鱼,null\n汉堡,a,b"
            elif name == "err_tool":
                self._reply({"jsonrpc": "2.0", "id": body.get("id"),
                             "result": {"isError": True,
                                        "content": [{"type": "text", "text": "boom"}]}})
                return
            else:
                text = json.dumps({"echo": name, "args": args, "ok": True})

            self._reply({"jsonrpc": "2.0", "id": body.get("id"),
                         "result": {"content": [{"type": "text", "text": text}],
                                    "isError": False}})
        else:
            self._reply({"jsonrpc": "2.0", "id": body.get("id"),
                         "error": {"code": -32601, "message": "method not found"}})

    def _reply(self, payload: dict, session_id: str | None = None):
        raw = json.dumps(payload)
        # use_sse      -> 标准 SSE 格式（data: 前缀 + 空行）
        # use_sse_bad  -> 声明 SSE 但吐裸 JSON，模拟不规范的真实实现
        if STATE["use_sse"]:
            data = f"data: {raw}\n\n".encode()
        else:
            data = raw.encode()

        self.send_response(200)
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        if STATE["use_sse"] or STATE["use_sse_bad"]:
            self.send_header("Content-Type", "text/event-stream")
        else:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _mk(url: str) -> McpClient:
    """本地 mock 必须绕过系统代理。

    有代理时 httpx 会把 127.0.0.1 的请求也送进代理，拿到 502 而不是 mock 的响应。
    """
    return McpClient(token="fake", url=url, trust_env=False)


def _serve() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _reset():
    STATE.update(auth_fail=False, auth_fail_403=False, biz_error_200=False,
                 rate_limit_once=False, rate_limited=False,
                 use_sse=False, use_sse_bad=False)


def main() -> None:
    srv = _serve()
    host, port = srv.server_address
    url = f"http://{host}:{port}"
    passed, failed = 0, 0

    def check(label: str, cond: bool, detail: str = ""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {label}")
        else:
            failed += 1
            print(f"  [FAIL] {label}  {detail}")

    print("\n1) initialize 握手 + 会话 ID 透传")
    _reset()
    c = _mk(url)
    c.initialize()
    check("协议版本协商成功", c.protocol_version == "2025-06-18", c.protocol_version)
    check("拿到 Mcp-Session-Id", c.session_id == "sess-fake-123", str(c.session_id))
    check("会话 ID 会透传到后续请求",
          c.call_tool("probe-session", {})["session"] == "sess-fake-123")

    print("\n2) tools/list")
    _reset()
    c = _mk(url)
    tools = c.list_tools()
    check("拿到 2 个工具", len(tools) == 2, str(len(tools)))
    check("工具名正确", {t["name"] for t in tools} == {"now-time-info", "query-meals"})
    qm = [t for t in tools if t["name"] == "query-meals"][0]
    check("inputSchema 含必需参数", qm["inputSchema"].get("required") == ["storeCode"])

    print("\n3) tools/call 返回 JSON -> 自动解析为 dict")
    _reset()
    c = _mk(url)
    r = c.call_tool("query-meals", {"storeCode": "S001"})
    check("解析为 dict", isinstance(r, dict), type(r).__name__)
    check("字段值正确", r.get("args", {}).get("storeCode") == "S001", str(r))

    print("\n4) tools/call 返回 TOON -> 保留原始文本")
    _reset()
    c = _mk(url)
    r = c.call_tool("toon_tool", {})
    check("保留为字符串", isinstance(r, str), type(r).__name__)
    check("内容未被破坏", r.startswith("{productName,nutritionDescription}"), r[:40])

    print("\n5) SSE 流式响应解析")
    _reset()
    STATE["use_sse"] = True
    c = _mk(url)
    r = c.call_tool("query-meals", {"storeCode": "S002"})
    check("SSE 也能解析为 dict", isinstance(r, dict) and r["args"]["storeCode"] == "S002", str(r))

    print("\n6) 声明 SSE 却返回裸 JSON 时也能兼容")
    _reset()
    STATE["use_sse_bad"] = True
    c = _mk(url)
    r = c.call_tool("query-meals", {"storeCode": "S003"})
    check("不规范 SSE 也能解析",
          isinstance(r, dict) and r["args"]["storeCode"] == "S003", str(r))

    print("\n7) 401 立即中止且不重试")
    _reset()
    STATE["auth_fail"] = True
    c = _mk(url)
    t0 = time.time()
    try:
        c.initialize()
        check("应抛 McpAuthError", False, "没有抛异常")
    except McpAuthError as e:
        check("抛出 McpAuthError", True)
        check("未做重试（<2秒完成）", time.time() - t0 < 2.0, f"{time.time()-t0:.1f}s")

    print("\n8) 429 退避后重试成功")
    _reset()
    STATE["rate_limit_once"] = True
    c = _mk(url)
    tools = c.list_tools()
    check("429 后重试成功", len(tools) == 2, str(len(tools)))

    print("\n9) 缺少 Token 时给出可操作提示")
    _reset()
    import os
    saved = os.environ.pop("MCD_MCP_TOKEN", None)
    try:
        # 必须关掉 .env，否则会读到项目里真实的 Token 而测不到"缺失"场景
        McpClient(url=url, use_env_file=False)
        check("应抛 McpAuthError", False, "没有抛异常")
    except McpAuthError as e:
        check("提示中包含申请地址", "open.mcd.cn/mcp" in str(e))
        check("提示中包含设置命令", "MCD_MCP_TOKEN" in str(e))
    finally:
        if saved:
            os.environ["MCD_MCP_TOKEN"] = saved

    print("\n10) 网关 403 + 业务错误格式能识别（实测场景）")
    _reset()
    STATE["auth_fail_403"] = True
    c = _mk(url)
    try:
        c.initialize()
        check("应抛 McpAuthError", False, "没有抛异常")
    except McpAuthError as e:
        check("抛出 McpAuthError", True)
        check("消息含业务错误码", "[0001] 系统错误" in str(e), str(e))
        check("消息含排查指引", "open.mcd.cn/mcp" in str(e))

    print("\n11) HTTP 200 但 body 是业务错误时能识别")
    _reset()
    STATE["biz_error_200"] = True
    c = _mk(url)
    try:
        c.call_tool("query-meals", {})
        check("应抛 McpError", False, "没有抛异常")
    except McpAuthError:
        check("不应抛 McpAuthError", False, "错误类型不对")
    except McpError as e:
        check("识别为业务错误", "门店不存在" in str(e), str(e))

    print("\n" + "=" * 46)
    print(f"  通过 {passed} 项，失败 {failed} 项")
    print("=" * 46)

    srv.shutdown()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
