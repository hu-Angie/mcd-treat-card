"""麦当劳 MCP Streamable HTTP 客户端。

只实现 MCP 协议层，不含任何麦当劳业务字段名。
因此本文件可以在拿到真实接口结构之前就写完并验证。

协议要点：
  POST /  JSON-RPC 2.0
  Authorization: Bearer <token>
  Accept: application/json, text/event-stream
  响应可能是 application/json 或 text/event-stream（SSE）

已知硬约束（来自官方文档）：
  401 -> Token 无效/过期/未携带，立即中止，不重试
  429 -> 触发 600 次/分钟限流，指数退避重试
"""

from __future__ import annotations

import json
import os
import re
import time
import random
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "https://mcp.mcd.cn"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
FALLBACK_PROTOCOL_VERSIONS = ["2025-03-26", "2024-11-05"]


class McpError(Exception):
    """MCP 协议层错误。"""


class McpAuthError(McpError):
    """401：Token 无效/过期/未携带。不可重试。"""


class McpRateLimitError(McpError):
    """429：触发限流。"""


def _load_env_file() -> None:
    """从项目根的 .env 载入配置。

    优先级：已存在的环境变量 > .env 文件。
    .env 被 .gitignore 排除，且**不应被任何工具读取后展示**——
    它存在的意义就是让 Token 不出现在命令行、日志和对话里。
    """
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        env_file = base / ".env"
        if not env_file.is_file():
            continue
        try:
            # utf-8-sig 会自动吃掉 PowerShell 5.1 写文件时加的 BOM，
            # 否则首个键名会变成 "\ufeffMCD_MCP_TOKEN" 而读不到
            lines = env_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


def _read_token() -> str:
    """只从环境变量读，不碰 .env。"""
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if not token:
        raise McpAuthError(
            "未找到环境变量 MCD_MCP_TOKEN。\n"
            "请先在项目根目录创建 .env 文件（该文件已被 .gitignore 排除），内容为：\n"
            "  MCD_MCP_TOKEN=你的Token\n"
            "Token 申请：https://open.mcd.cn/mcp\n"
            "另也可设为环境变量：PowerShell 用 $env:MCD_MCP_TOKEN=..."
        )
    return token


class McpClient:
    def __init__(
        self,
        token: str | None = None,
        url: str = DEFAULT_URL,
        protocol_version: str | None = None,
        timeout: float = 30.0,
        trust_env: bool = True,
        use_env_file: bool = True,
    ) -> None:
        """trust_env: 是否读系统代理环境变量。

        有代理（如 Clash 127.0.0.1:7890）时，httpx 默认会把请求送进代理。
        访问 https://mcp.mcd.cn 通常没问题，但代理配置不当会导致连接失败，
        届时把 trust_env 关掉即可直连。

        use_env_file: 是否读取项目根的 .env。测试或只想用环境变量时关掉。
        """
        if use_env_file:
            _load_env_file()
        self.token = token or _read_token()
        self.url = url
        self.protocol_version = protocol_version
        self.session_id: str | None = None
        self._id = 0
        self._client = httpx.Client(timeout=timeout, trust_env=trust_env)

    # ---------- 协议层 ----------

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict, require_session: bool = True) -> dict:
        """发一个 JSON-RPC 请求，返回响应中的 result 部分。

        对 429 做指数退避重试；对 401 直接抛 McpAuthError。
        """
        headers = self._headers()
        if require_session and self.session_id is None and payload.get("method") != "initialize":
            self.initialize()

        max_retries = 4
        delay = 1.0
        last_err: Exception | None = None

        for attempt in range(max_retries):
            try:
                resp = self._client.post(self.url, json=payload, headers=headers)
            except httpx.RequestError as e:
                last_err = e
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code in (401, 403):
                # 401 = MCP 层判定 Token 无效；403 = 前置网关拦截（实测存在）
                # 网关返回的是麦当劳业务格式 {"code":"0001","msg":"系统错误"}，
                # 不是 JSON-RPC，必须单独识别，否则会报出看不懂的错
                detail = _biz_error(resp.text) or resp.text[:200]
                raise McpAuthError(
                    f"{resp.status_code}：{detail}\n"
                    "请到 https://open.mcd.cn/mcp 确认 Token 已激活且复制完整。"
                )
            if resp.status_code == 429:
                if attempt == max_retries - 1:
                    raise McpRateLimitError("429：触发 600 次/分钟限流，重试后仍失败。")
                time.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
                continue
            if resp.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise McpError(f"HTTP {resp.status_code}: {resp.text[:500]}")

            # 会话 ID：服务端可能在 initialize 时下发
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid

            data = self._parse_body(resp)

            # 网关/业务层可能直接返回 {"code":...,"msg":...} 而非 JSON-RPC
            if "result" not in data and "error" not in data:
                biz = _biz_error(json.dumps(data))
                if biz:
                    raise McpError(f"麦当劳返回业务错误：{biz}")

            return data

        raise McpError(
            f"请求失败（已重试 {max_retries} 次）：{last_err}\n"
            "排查：1) 确认能访问 https://mcp.mcd.cn；"
            "2) 若系统有代理（如 Clash），可改用 McpClient(..., trust_env=False) 直连。"
        )

    def _parse_body(self, resp: httpx.Response) -> dict:
        """MCP 响应可能是普通 JSON，也可能是 SSE 流。两种都要能解。"""
        ctype = resp.headers.get("Content-Type", "")

        if "text/event-stream" in ctype:
            try:
                return _parse_sse(resp.text)
            except McpError:
                # 部分实现声明了 SSE 却直接返回裸 JSON，回退一次再解析
                try:
                    return json.loads(resp.text)
                except json.JSONDecodeError:
                    raise
        try:
            return resp.json()
        except json.JSONDecodeError:
            return _parse_sse(resp.text)

    def initialize(self) -> dict:
        """建立会话。协议版本先试最新，被拒则逐级回退。"""
        versions = [self.protocol_version] if self.protocol_version else []
        versions += [DEFAULT_PROTOCOL_VERSION] + FALLBACK_PROTOCOL_VERSIONS

        # 去重保持顺序
        seen: set[str] = set()
        ordered = [v for v in versions if v and not (v in seen or seen.add(v))]

        last_err: Exception | None = None
        for ver in ordered:
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": ver,
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-treat-card", "version": "1.0.0"},
                },
            }
            try:
                result = self._post(payload, require_session=False)
            except McpAuthError:
                # 401 与协议版本无关，回退没有意义，直接上抛
                raise
            except McpError as e:
                last_err = e
                continue

            if "error" in result:
                last_err = McpError(f"initialize 被拒绝（{ver}）：{result['error']}")
                continue

            self.protocol_version = ver
            # MCP 要求初始化完成后发一次 initialized 通知
            try:
                self._post(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    require_session=False,
                )
            except McpError:
                pass  # 通知失败不影响后续调用
            return result

        raise McpError(f"initialize 全部协议版本均失败：{last_err}")

    def list_tools(self) -> list[dict]:
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            }
        )
        if "error" in result:
            raise McpError(f"tools/list 失败：{result['error']}")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调用工具，返回解析后的结果。

        返回规则：
          - 若 content 是单条 text 且能被 json 解析，返回解析后的对象
          - 否则返回原始 text（例如 TOON 文本）
        """
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if "error" in result:
            raise McpError(f"tools/call {name} 失败：{result['error']}")

        res = result.get("result", {})
        if res.get("isError"):
            raise McpError(f"{name} 返回业务错误：{res}")

        content = res.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if not texts:
            return res

        raw = texts[0] if len(texts) == 1 else "\n".join(texts)
        stripped = raw.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return raw
        return raw


    # 麦当劳 MCP 不直接返回 JSON，而是返回一份 Markdown 文档：
    #   # API Response Information
    #   ## Response Structure   <- 字段说明
    #   ## Original Response   <- 真正的 JSON 在这里
    _DOC_MARKERS = ("## Original Response", "## Response Body", "## Original Response Body")

    def _extract_mcd_envelope(self, text: str) -> dict | None:
        return _find_json_envelope(text)

    def _extract_field_docs(self, text: str) -> str:
        """取出文档里的字段说明部分（官方自带，等于随响应下发的 schema）。"""
        if not text:
            return ""
        start = text.find("## Response Structure")
        end = text.find("## Original Response")
        if start < 0 or end <= start:
            return ""
        return text[start:end].strip()

    def call_tool_raw_text(self, name: str, arguments: dict | None = None) -> str:
        res = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if "error" in res:
            raise McpError(f"tools/call {name} 失败：{res['error']}")
        content = res.get("result", {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts)

    def call_tool_data(self, name: str, arguments: dict | None = None) -> Any:
        """调用并直接返回业务数据（信封里的 data 部分）。

        麦当劳响应外层是 {"success","code","message","traceId","data"}，
        业务代码通常只要 data。
        """
        raw = self.call_tool_raw_text(name, arguments)
        env = self._extract_mcd_envelope(raw)
        if env is None:
            return self._parse_plain(raw)
        if env.get("success") is False or (env.get("code") not in (None, 200)):
            raise McpError(
                f"{name} 返回业务错误：[{env.get('code')}] {env.get('message')}"
                f"（traceId: {env.get('traceId')}）"
            )
        return env.get("data")

    def _parse_plain(self, raw: str) -> Any:
        stripped = raw.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return raw
        return raw

def _scan_json_object(text: str) -> dict | None:
    """在任意文本里扫出第一个完整 JSON 对象。

    麦当劳响应里 JSON 的位置不固定：有的在 '## Original Response' 之后，
    有的直接跟在展示指令后面，前后都可能裹着自然语言。
    所以不能用 split，必须做括号平衡扫描。
    """
    start = text.find('{"')
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find('{"', start + 1)
    return None


def _find_json_envelope(text: str) -> dict | None:
    """取出麦当劳响应信封 {"success":..,"code":..,"data":..}。"""
    if not text:
        return None
    # 先按标记定位，命中就用标记后的内容
    for marker in ("## Original Response", "## Response Body"):
        i = text.find(marker)
        if i >= 0:
            env = _scan_json_object(text[i + len(marker):])
            if env:
                return env
    # 标记缺失时退回全文扫描
    return _scan_json_object(text)


def _biz_error(text: str) -> str | None:
    """识别麦当劳业务错误格式：{"code":"0001","msg":"系统错误","data":null}。"""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(obj, dict) and "code" in obj and "msg" in obj:
        return f'[{obj.get("code")}] {obj.get("msg")}'
    return None


def _parse_sse(text: str) -> dict:
    """从 SSE 文本中取出最后一个完整的 JSON-RPC 对象。"""
    last: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            last = obj
    if not last:
        raise McpError(f"SSE 响应中未找到 JSON-RPC 对象：{text[:500]}")
    return last
