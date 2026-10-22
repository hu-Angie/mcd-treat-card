"""通讯录离线自测。

    python -m tests.test_contacts
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import contacts  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  {detail}")


def main() -> None:
    print("\n1) 默认存放位置")
    p = contacts.DEFAULT_PATH
    check("不在仓库目录内", ROOT not in p.parents, str(p))
    check("位于 _本地隐私文件/", p.parent.name == "_本地隐私文件", str(p.parent))

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "ab.json"

        print("\n2) 新增与按人名查询")
        contacts.add("张伟", "上海市浦东新区", "科苑路88号", "上海市", "张伟", "13800000001", path=f)
        c = contacts.get("张伟", path=f)
        check("取到联系人", c is not None)
        check("城市匹配", bool(c and c["city"] == "上海市"))
        check("电话匹配", bool(c and c["phone"] == "13800000001"))

        print("\n3) 多人共存，互不干扰")
        contacts.add("小李", "北京市朝阳区", "建国路1号", "北京市", "李雷", "13900000002", path=f)
        check("两人都存在", contacts.names(path=f) == ["张伟", "小李"])
        check("张伟仍是上海", contacts.get("张伟", path=f)["city"] == "上海市")
        check("小李是北京", contacts.get("小李", path=f)["city"] == "北京市")

        print("\n4) 电话格式校验")
        try:
            contacts.add("错误", "城市", "门牌", "市", "名", "139", path=f)
            check("拒绝非 11 位号码", False, "没有拒绝")
        except ValueError:
            check("拒绝非 11 位号码", True)

        print("\n5) 查不到的人")
        check("返回 None", contacts.get("查无此人", path=f) is None)

    print("\n" + "=" * 46)
    print(f"  通过 {passed} 项，失败 {failed} 项")
    print("=" * 46)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
