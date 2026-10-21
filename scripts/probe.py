"""接口侦察工具 —— 拿到官方 README 里缺失的真实结构。

官方文档只有 33 个工具名和一句描述，没有入参/返回 schema。
但 MCP 协议本身会通过 tools/list 把每个工具的 inputSchema 交出来。

所以第一步永远是：

    python scripts/probe.py schema

拿到 inputSchema 之后，才知道每个工具该传什么参数。
拿到真实返回之后，才知道每个字段叫什么名字。

用法：
    python scripts/probe.py schema                  # dump 全部工具的 inputSchema
    python scripts/probe.py list                    # 只打印工具名清单
    python scripts/probe.py call now-time-info      # 调用无参工具
    python scripts/probe.py call query-meals '{"storeCode":"..."}'
    python scripts/probe.py readonly                # 依次跑只读链路，逐步存盘
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import McpClient, McpError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# 脱敏样本，可公开进仓库
FIXTURES = ROOT / "fixtures"
# 真实响应（含地址/券/积分），放在项目**外面**，
# 这样 `git add -A` 和网页全选上传都不可能碰到它
PRIVATE = ROOT.parent / "_本地隐私文件"


def _ensure_fixtures() -> None:
    FIXTURES.mkdir(exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)


def _save(name: str, data, private: bool = False) -> None:
    """private=True 表示内容含真实个人信息，写到项目外。"""
    base = PRIVATE if private else FIXTURES
    path = base / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    where = "_本地隐私文件" if private else "fixtures"
    print(f"  [saved] {where}/{name}.json  ({path.stat().st_size} bytes)")


def cmd_schema(client: McpClient) -> None:
    tools = client.list_tools()
    _ensure_fixtures()

    print(f"\n共 {len(tools)} 个工具\n")
    print("=" * 78)

    compact = []
    for t in tools:
        name = t.get("name", "?")
        desc = (t.get("description") or "").split("\n")[0][:70]
        schema = t.get("inputSchema", {})
        props = schema.get("properties", {}) or {}
        required = schema.get("required", []) or []

        print(f"\n■ {name}")
        print(f"  {desc}")
        if props:
            print(f"  入参 ({len(props)}):")
            for k, v in props.items():
                mark = "*" if k in required else " "
                types = v.get("type", "?")
                if types == "array":
                    types = f"array<{_item_type(v)}>"
                note = v.get("description", "")[:40]
                print(f"    {mark} {k}: {types}" + (f"   # {note}" if note else ""))
        else:
            print("  入参: 无")

        compact.append({"name": name, "inputSchema": schema, "description": t.get("description")})

    print("\n" + "=" * 78)
    _save("tools_schema", compact)
    print("\n完整 schema 已存盘。下一步：读 fixtures/tools_schema.json 确认参数名后，")
    print("用 `python scripts/probe.py call <工具名> '<JSON参数>'` 逐个取真实返回。")


def _item_type(v: dict) -> str:
    items = v.get("items", {})
    if isinstance(items, dict):
        return items.get("type", "?")
    return "?"


def cmd_list(client: McpClient) -> None:
    tools = client.list_tools()
    for i, t in enumerate(tools, 1):
        props = (t.get("inputSchema", {}) or {}).get("properties", {}) or {}
        flag = "" if props else "  (无参)"
        print(f"{i:2d}. {t.get('name')}{flag}")


def cmd_call(client: McpClient, name: str, args_json: str | None) -> None:
    args = json.loads(args_json) if args_json else {}
    print(f"\n>>> {name} {json.dumps(args, ensure_ascii=False)}")

    result = client.call_tool(name, args)

    _ensure_fixtures()
    _save(f"raw_{name}", {"tool": name, "arguments": args, "result": result}, private=True)

    print("\n--- 返回内容 ---")
    if isinstance(result, str):
        print(result[:4000])
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
    print("--- end ---")
    print("\n提示：若上面是 JSON，字段名已可直接用于 parsers.py；")
    print("      若是 TOON 文本（首行是 {字段1,字段2,...} 头），需走 toon 解析器。")


def cmd_readonly(client: McpClient) -> None:
    """按依赖顺序跑只读链路。需要参数的地方从上一步结果里取，取不到就跳过。"""
    _ensure_fixtures()

    def step(label: str, tool: str, args: dict | None = None):
        print(f"\n[{label}] {tool}")
        try:
            r = client.call_tool(tool, args)
        except McpError as e:
            print(f"  ! 失败: {e}")
            return None
        _save(f"raw_{tool}", {"tool": tool, "arguments": args, "result": r}, private=True)
        # 只打印结构不打印值——地址/订单等个人隐私不进终端输出与日志
        print(f"  OK: {_shape(r)}")
        return r

    step("时间", "now-time-info")
    addrs = step("地址", "delivery-query-addresses")
    coupons = step("我的券", "query-my-coupons")
    step("可领券", "available-coupons")
    step("积分", "query-my-account")
    step("营养", "list-nutrition-foods")

    # 有地址才继续往下走
    addr = _first_addr(addrs)
    if not addr:
        print("\n未取到地址，链路到此为止。")
        print("请先执行：python scripts/probe.py call delivery-create-address '<地址JSON>'")
        print("（地址参数名以上面 tools_schema.json 中该工具的入参为准）")
        return

    stores = step("可配送门店", "delivery-query-stores", addr)
    store = _first_store(stores)
    if not store:
        print("\n未取到门店，链路到此为止。")
        return

    step("菜单", "query-meals", store)
    step("门店券", "query-store-coupons", store)

    print("\n只读链路跑完。检查 fixtures/ 下的文件，确认字段名后再写解析层。")


def _shape(obj, depth: int = 0) -> str:
    """输出结构骨架（键名路径），不输出值。用于避免隐私数据进终端与日志。"""
    pad = "  " * depth
    if isinstance(obj, dict):
        keys = list(obj.keys())
        if depth >= 2:
            return "{" + ", ".join(keys[:8]) + ("..." if len(keys) > 8 else "") + "}"
        out = []
        for k in keys[:12]:
            v = obj[k]
            if isinstance(v, (dict, list)):
                out.append(f"{pad}  {k}: {_shape(v, depth + 1)}")
            else:
                out.append(f"{pad}  {k}: {type(v).__name__}")
        if len(keys) > 12:
            out.append(f"{pad}  ... 共 {len(keys)} 个键")
        return "{\n" + "\n".join(out) + f"\n{pad}}}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        if depth >= 2:
            return f"[{len(obj)} 项]"
        return f"[{len(obj)} 项] 首项: {_shape(obj[0], depth + 1)}"
    return type(obj).__name__


def _first_addr(addrs) -> dict | None:
    """从地址返回里挖出第一个可用作查询参数的地址对象。"""
    return _pick_first(addrs, ["addressId", "id", "address"])


def _first_store(stores) -> dict | None:
    return _pick_first(stores, ["storeCode", "code", "storeId", "id"])


def _pick_first(data, keys: list[str]) -> dict | None:
    if not isinstance(data, dict):
        return None
    for container_key in ("data", "result", "list", "items", "records"):
        c = data.get(container_key)
        if isinstance(c, list) and c and isinstance(c[0], dict):
            d = c[0]
            return {k: d[k] for k in keys if k in d} or None
    # 直接就是对象
    if any(k in data for k in keys):
        return {k: data[k] for k in keys if k in data} or None
    return None


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    client = McpClient()

    try:
        client.initialize()
        print(f"[connected] 协议版本 {client.protocol_version}"
              + (f"  会话 {client.session_id[:8]}..." if client.session_id else "  无状态会话"))

        if cmd == "schema":
            cmd_schema(client)
        elif cmd == "list":
            cmd_list(client)
        elif cmd == "call":
            name = sys.argv[2]
            args_json = sys.argv[3] if len(sys.argv) > 3 else None
            cmd_call(client, name, args_json)
        elif cmd == "readonly":
            cmd_readonly(client)
        else:
            print(__doc__)
    except McpError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
