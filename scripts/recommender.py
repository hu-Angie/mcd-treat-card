"""组单引擎：菜单 + 场景 + 时段 + 预算 → 3 套候选。

本项目的产品哲学在这里落地：

    请客不是省钱，是为了那句"我记得你"。

所以目标函数不是「总价最小」，而是「体面度最大，预算内总价次之」。
若用某张券需要加购，只有当加购物本身是合理组成部分时才保留该券，
否则弃券——为了满减塞两个甜筒进去，送的人省了 8 块，收的人看到的是"敷衍"。

券优化依赖真实券结构，联调后再接进来；组单主体可先用菜单离线跑通。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from pathlib import Path

# 麦当劳的分类名里含换行（如 "鸡肉汉堡\n/卷"），所以用片段匹配而非全等
MAIN_HINTS = ("鸡肉汉堡", "巨无霸", "安格斯", "厚牛堡", "精选单人餐", "大堡口福", "500")
SIDE_HINTS = ("小食拼盘", "蘸酱炸鸡", "炸鸡", "小食甜品")
DRINK_HINTS = ("麦咖啡", "饮品")

# 已含配餐的组合：名字里带这些，说明主食+小食+饮料已经齐了，不能再加配
SET_MEAL_HINTS = ("套餐", "三件套", "四件套", "随心选", "双全盒", "分享餐", "八件套")
# 蘸酱：单独点它不算一份小食
SAUCE_SUFFIX = ("酱",)
# 周边玩具：不能吃的，绝不能送出去
MERCH_HINTS = ("痒痒挠", "毛绒", "周边", "玩具", "捶", "盲盒")
DESSERT_HINTS = ("圆筒", "新地", "麦旋风", "派", "苹果片", "冰淇淋")

ROLE_MAIN, ROLE_SIDE, ROLE_DRINK, ROLE_OTHER = "main", "side", "drink", "other"

SCENE_HINTS = {
    "overtime": "改完这个 bug 还能再战",
    "longdistance": "送到手还是热的",
    "apology": "甜一点好说话",
    "care": "吃饱才有力气",
}

# 场景 → 主食气质偏好。
# 没有它的话，价格升序会让三套候选永远都是最便宜的那款堡，毫无个性。
SCENE_MAIN_HINTS = {
    "overtime": ("巨无霸", "双层", "不素之霸", "安格斯", "厚牛堡"),
    "longdistance": ("板烧", "麦辣", "巨无霸", "麦香鱼"),
    "apology": ("板烧", "麦香鱼", "吉士"),
    "care": ("双层", "安格斯", "大堡口福", "厚牛堡"),
}

DEFAULT_BUDGET_YUAN = Decimal("60")


@dataclass(frozen=True)
class MenuItem:
    code: str
    name: str
    price: Decimal          # 单位：元
    categories: tuple[str, ...]

    @property
    def role(self) -> str:
        joined = "".join(self.categories)
        if any(h in joined for h in DRINK_HINTS):
            return ROLE_DRINK
        if any(h in joined for h in MAIN_HINTS):
            return ROLE_MAIN
        if any(h in joined for h in SIDE_HINTS):
            return ROLE_SIDE
        return ROLE_OTHER

    @property
    def is_set_meal(self) -> bool:
        return any(h in self.name for h in SET_MEAL_HINTS)

    @property
    def is_dessert(self) -> bool:
        return any(h in self.name for h in DESSERT_HINTS)

    @property
    def is_sauce(self) -> bool:
        return self.name.endswith(SAUCE_SUFFIX)

    @property
    def is_merch(self) -> bool:
        return any(h in self.name for h in MERCH_HINTS)

    @property
    def edible(self) -> bool:
        """能不能送出去。蘸酱不算菜，周边玩具不能吃。"""
        return not self.is_sauce and not self.is_merch

    @property
    def is_coffee(self) -> bool:
        joined = "".join(self.categories) + self.name
        return "咖啡" in joined or "美式" in joined or "拿铁" in joined


@dataclass(frozen=True)
class Combo:
    main: MenuItem
    side: MenuItem | None
    drink: MenuItem | None

    @property
    def items(self) -> list[MenuItem]:
        return [i for i in (self.main, self.side, self.drink) if i]

    @property
    def total(self) -> Decimal:
        return sum((i.price for i in self.items), Decimal("0"))

    @property
    def kind(self) -> str:
        if self.main.is_set_meal:
            return "set"
        if self.side and self.drink:
            return "full"
        if self.drink:
            return "simple"
        return "minimal"

    def title(self, scene: str) -> str:
        if self.kind == "set":
            return "配齐组"
        if self.drink and self.drink.is_coffee:
            return "提神组"
        if scene == "apology":
            return "甜心组"
        if self.side:
            return "饱腹组"
        return "简约组"

    def reason(self, scene: str) -> str:
        parts = [SCENE_HINTS.get(scene, "吃得舒服")]
        if self.drink and self.drink.is_coffee:
            parts.append("咖啡顶住")
        elif self.drink:
            parts.append("配了喝的")
        if self.side:
            parts.append("搭一份小食够吃" if not self.side.is_dessert else "以甜收尾")
        return "，".join(parts)

    def decency(self, scene: str) -> int:
        """体面度：齐全 > 简单；主食契合场景则加分。"""
        score = 2                                   # 有主食是底线
        if self.drink:
            score += 1
        if self.side:
            score += 1
        if any(k in self.main.name for k in SCENE_MAIN_HINTS.get(scene, ())):
            score += 2
        if scene == "overtime" and self.drink and self.drink.is_coffee:
            score += 1
        if scene == "apology" and self.side and self.side.is_dessert:
            score += 1
        return score


def load_menu(data: dict) -> list[MenuItem]:
    """把 query-meals 返回的 data 转成菜品列表。"""
    cats_by_code: dict[str, list[str]] = {}
    for cat in data.get("categories", []):
        cname = cat.get("name") or ""
        for m in cat.get("meals") or []:
            code = m.get("code")
            if code:
                cats_by_code.setdefault(code, []).append(cname)

    items: list[MenuItem] = []
    for code, info in (data.get("meals") or {}).items():
        try:
            price = Decimal(str(info.get("currentPrice")))
        except Exception:
            continue
        items.append(
            MenuItem(
                code=code,
                name=info.get("name") or code,
                price=price,
                categories=tuple(cats_by_code.get(code, [])),
            )
        )
    return items


def load_menu_file(path: str | Path) -> list[MenuItem]:
    return load_menu(json.loads(Path(path).read_text(encoding="utf-8")))


def build_combos(
    items: list[MenuItem],
    scene: str = "overtime",
    budget: Decimal | None = None,
    top_k: int = 3,
) -> list[Combo]:
    """枚举候选组合，按体面度取 Top-K。

    三条硬规则：
      1. 主食必选 —— 送的是"一顿饭"而不是一把零食
      2. 套餐不再加配 —— 三件套/四件套本身已含小食饮料，再配就是浪费
      3. 蘸酱与周边玩具一律排除
    """
    cap = budget if budget is not None else DEFAULT_BUDGET_YUAN

    food = [i for i in items if i.edible]
    mains = [i for i in food if i.role == ROLE_MAIN]
    sets = [i for i in mains if i.is_set_meal]
    solo = [i for i in mains if not i.is_set_meal]
    sides = [i for i in food if i.role == ROLE_SIDE and not i.is_dessert]
    sweets = [i for i in food if i.role == ROLE_SIDE and i.is_dessert]
    drinks = [i for i in food if i.role == ROLE_DRINK]

    # 道歉场景把甜品排到小食前面
    side_pool = (sweets + sides) if scene == "apology" else (sides + sweets)

    def score(c: Combo) -> int:
        s = c.decency(scene)
        if any(k in c.main.name for k in SCENE_MAIN_HINTS.get(scene, ())):
            s += 2
        return s

    # 全量枚举主食 × 小食 × 饮料，再按预算过滤。
    # 不能"先定主食再配餐"——那会固定选中最贵的主食，导致配餐必然超预算。
    pool: list[Combo] = []
    for s in sets:                                  # 套餐已配齐，不再加配
        if s.price <= cap:
            pool.append(Combo(main=s, side=None, drink=None))
    for m, sd, dk in product(solo, [None] + side_pool, [None] + drinks):
        c = Combo(main=m, side=sd, drink=dk)
        if c.total <= cap:
            pool.append(c)

    pool.sort(key=lambda c: (-score(c), c.total))

    # 三套主食必须不同，否则三套只是同一款汉堡加减配菜
    picked: list[Combo] = []
    used_main: set[str] = set()
    for c in pool:
        if c.main.name in used_main:
            continue
        used_main.add(c.main.name)
        picked.append(c)
        if len(picked) >= top_k:
            break
    return picked[:top_k]
