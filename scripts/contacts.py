"""联系人通讯录：人名 ↔ 收货地址。

存放位置：项目**外面**的 `_本地隐私文件/address_book.json`。

为什么不放在项目里：
  .gitignore 挡不住网页上传，也挡不住 `git add -A` 之前的一次误放。
  移出项目目录，才能让「全选上传」这个动作永远安全。

这个模块只做本地文件读写，不碰任何 MCP 调用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIR = ROOT.parent / "_本地隐私文件"
# 可用环境变量覆盖，方便测试时指向临时目录
DEFAULT_PATH = Path(
    os.environ.get("MCD_CONTACTS_PATH", str(PRIVATE_DIR / "address_book.json"))
)

# 与 MCP 的 delivery-create-address 入参保持一致，方便直接透传
ADDRESS_FIELDS = ("address", "addressDetail", "city", "contactName", "phone", "gender")


def _empty() -> dict:
    return {"version": 1, "contacts": {}}


def load(path: Path | None = None) -> dict:
    p = path or DEFAULT_PATH
    if not p.is_file():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict) or "contacts" not in data:
        return _empty()
    data.setdefault("contacts", {})
    return data


def save(data: dict, path: Path | None = None) -> Path:
    p = path or DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def get(name: str, path: Path | None = None):
    """按人名取联系人；没有返回 None。"""
    return load(path).get("contacts", {}).get(name)


def names(path: Path | None = None) -> list[str]:
    return list(load(path).get("contacts", {}).keys())


def add(
    name: str,
    address: str,
    address_detail: str,
    city: str,
    contact_name: str,
    phone: str,
    gender: str | None = None,
    note: str = "",
    path: Path | None = None,
) -> dict:
    """新增/覆盖一个联系人。phone 必须是 11 位数字。"""
    if not (isinstance(phone, str) and phone.isdigit() and len(phone) == 11):
        raise ValueError("phone 必须是 11 位纯数字")

    data = load(path)
    data.setdefault("contacts", {})[name] = {
        "address": address,
        "addressDetail": address_detail,
        "city": city,
        "contactName": contact_name,
        "phone": phone,
        "gender": gender,
        "note": note,
        "mcdAddressId": None,
    }
    save(data, path)
    return data["contacts"][name]


def set_address_id(name: str, address_id: str, path: Path | None = None) -> dict:
    """回填麦当劳账户里的地址 ID，避免重复创建。"""
    data = load(path)
    if name not in data.get("contacts", {}):
        raise KeyError(f"通讯录里没有「{name}」")
    data["contacts"][name]["mcdAddressId"] = address_id
    save(data, path)
    return data["contacts"][name]


def mcp_args(contact: dict) -> dict:
    """把联系人转成 delivery-create-address 的入参。"""
    return {k: contact.get(k) for k in ADDRESS_FIELDS if contact.get(k)}


# ---------------------------------------------------------------- CLI

def _cmd_list(_):
    ns = names()
    print(f"共 {len(ns)} 位联系人" if ns else "通讯录为空")
    for n in ns:
        c = get(n)
        print(f"  {n}    {c.get('city', '')}{c.get('address', '')}{c.get('addressDetail', '')}")


def _cmd_show(a):
    c = get(a.name)
    print(json.dumps(c, ensure_ascii=False, indent=2) if c else f"通讯录里没有「{a.name}」")


def _cmd_add(a):
    c = add(a.name, a.address, a.detail, a.city, a.contact, a.phone,
            a.gender, a.note)
    print(f"已保存：{a.name}")
    print(json.dumps(c, ensure_ascii=False, indent=2))


def _cmd_remove(a):
    data = load()
    if a.name in data.get("contacts", {}):
        del data["contacts"][a.name]
        save(data)
        print(f"已删除：{a.name}")
    else:
        print(f"通讯录里没有「{a.name}」")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="联系人通讯录（存于项目外，不入库）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有联系人")
    sub.add_parser("show", help="查看单人").add_argument("--name", required=True)
    sub.add_parser("remove", help="删除联系人").add_argument("--name", required=True)

    p = sub.add_parser("add", help="新增或覆盖联系人")
    p.add_argument("--name", required=True, help="通讯录里的名字（用来点单时称呼）")
    p.add_argument("--city", required=True, help="城市")
    p.add_argument("--address", required=True, help="主干地址")
    p.add_argument("--detail", required=True, help="门牌号")
    p.add_argument("--contact", required=True, help="收件人真实姓名")
    p.add_argument("--phone", required=True, help="11 位数字")
    p.add_argument("--gender")
    p.add_argument("--note", default="")

    args = ap.parse_args()
    {"list": _cmd_list, "show": _cmd_show, "add": _cmd_add,
     "remove": _cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    main()
