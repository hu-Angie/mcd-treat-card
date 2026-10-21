# MCP 集成说明

## 1. 使用的 MCP Server

| 项 | 值 |
|---|---|
| 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | `Authorization: Bearer <token>` |
| 提供方 | 麦当劳中国（GitHub 组织 M-China，仓库 `mcd-mcp-server`，共 33 个工具） |
| Token 申请 | https://open.mcd.cn/mcp |

配置示例（提交进仓库的 `mcp-config.example.json` 只允许环境变量占位符）：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

---

## 2. 实际使用的 Tool

33 个工具中，本项目使用 13 个。**其中只有 1 个是写操作。**

| Tool | 读写 | 调用阶段 | 用途 |
|---|---|---|---|
| `now-time-info` | 读 | COLLECT | 判断当前时段，决定推荐品类 |
| `delivery-query-addresses` | 读 | COLLECT | 复用用户已有配送地址 |
| `delivery-create-address` | **写** | COLLECT | 无地址时新增收件地址 |
| `delivery-query-stores` | 读 | MENU | 查**对方地址**附近可配送门店 |
| `query-nearby-stores` | 读 | MENU（兜底） | 外送不可用时查询店，切到店取餐 |
| `query-meals` | 读 | MENU | 拉门店当前在售菜单 |
| `query-meal-detail` | 读 | MENU（按需） | 展开套餐组成与可替换项 |
| `query-store-coupons` | 读 | MENU | 门店维度可用券 |
| `query-my-coupons` | 读 | MENU | 账户维度所有券 |
| `calculate-price` | 读 | RECOMMEND | 对 Top-K 候选验证真实总价 |
| `create-order` | **写** | 确认闸门后 | 建单，返回订单详情与支付链接 |
| `query-order` | 读 | FINAL_CARD | 校验订单真实存在、播报配送状态 |
| `cancel-order` | **写** | 仅用户主动要求 | 撤单 |

### 明确不使用的工具及理由

| 不用的工具 | 理由 |
|---|---|
| `list-nutrition-foods` | 请客场景不需要营养数据；且它返回 TOON 文本而非 JSON，引入无谓解析成本 |
| `auto-bind-coupons` / `available-coupons` | 本项目**主动不帮助凑券**，弃券保持组合干净是产品原则 |
| `mall-*` / `draw-lottery` / `query-my-prizes` | 积分商城与抽奖不在请客链路内 |
| `party-*` / `query-meal-assistance` | 团餐与企业助餐场景不同 |

---

## 3. 调用流程

```
START
  │
  ├─ now-time-info ─────────────────► 时段：早餐 / 午餐 / 晚餐 / 夜宵
  │
  ├─ delivery-query-addresses ──────► 有地址？
  │        └─ 无 → delivery-create-address   ← 写操作 ①
  │
  ├─ delivery-query-stores ─────────► 对方地址附近可配送门店 → 取最近的一家
  │        └─ 无可用门店 → query-nearby-stores → 降级为到店取餐
  │
  ├─ query-meals ───────────────────► 门店在售菜单
  ├─ query-meal-detail（按需）──────► 套餐组成 / 可替换项
  ├─ query-store-coupons ───────────► 门店可用券
  ├─ query-my-coupons ──────────────► 账户名下所有券
  │
  ├─ [本地] 组单引擎
  │     时段过滤 → 主食必选 → 场景加权 → 枚举组合
  │     → 券匹配（**需加购才能用的券一律弃用**）
  │     → 预算与起送价硬约束 → 取 Top3
  │
  ├─ calculate-price × 3 ───────────► 逐套验证真实总价（单位：分）
  │
  ├─ [展示 3 套候选] ← 用户挑选 + 写祝福语
  │
  ├─ [渲染预览卡]
  │
  ├─ ★ 确认闸门：用户明确说"确认"才算通过 ★
  │        └─ 未通过则绝不继续
  │
  ├─ create-order ──────────────────► orderId + 支付链接   ← 写操作 ②
  │        └─ 用户自己扫码付款（Agent 不代付）
  │
  ├─ query-order ───────────────────► 校验订单真实存在
  │
  └─ [渲染正式卡] → 交付 1080×1440 PNG
```

**单次完整流程约 11-15 次调用**，距 600 次/分钟限流极远，但仍实现指数退避重试。

---

## 4. 业务价值

麦当劳 MCP 把三件事挂在了一起：**读你的会员数据 → 替你写订单 → 把支付链接递回来**。
33 个工具中，同时具备「跨人」与「写操作」属性的组合只有 `delivery-create-address` + `create-order` 这一条。

McTreat 的价值正是这条链路第一次被用来做**情感表达**，而不是省钱：

1. **对麦当劳**：1024 程序员节期间，把一个技术接口讲成了一个有人情味的故事；
   请客场景天然带来新客——收到卡的人下次可能自己点。
2. **对用户**：把"想关心一个人但不知道怎么开口"变成一句话能完成的事。
3. **对开发者**：示范了消费品牌 MCP 接口在"工具性"之外的另一种用法。

---

## 5. 接口事实获取方式

官方 README 未提供入参/返回 schema，因此本项目通过 MCP 协议的 `tools/list` 取回真实 `inputSchema`，
再用 `tools/call` 逐个取真实返回，存为 `fixtures/` 下的样本：

```bash
python scripts/probe.py schema      # 取回全部工具的 inputSchema
python scripts/probe.py readonly    # 依次跑只读链路并逐步存盘
```

**含真实地址/订单的样本文件不进公开仓库**（见 `.gitignore`）。

---

## 6. 安全边界

| 项 | 处理方式 |
|---|---|
| Token | 只从环境变量读取，不进仓库、日志、截图、对话输出 |
| 401 | 提示用户检查 Token 后中止，不重试 |
| 429 | 指数退避重试，3 次后放弃 |
| 写操作 | 仅 `create-order`（及 `delivery-create-address`），且必须用户显式确认 |
| 付款 | 只返回支付链接，**付款永远由人完成** |
| 个人信息 | 地址只存用户自己的麦当劳账户，本项目不做任何持久化 |
