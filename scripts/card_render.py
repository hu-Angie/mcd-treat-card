"""卡片渲染：HTML 模板 -> 1080x1440 PNG。

模板用 string.Template（$var 语法），避开与 CSS 花括号的冲突，且不需要引入 jinja2。
用户输入一律 html.escape，防止祝福语里的特殊字符破坏页面。
"""

from __future__ import annotations

import html
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "card.html"
OUTPUT_DIR = ROOT / "output"


def _item_rows(items: list[tuple[str, str]]) -> str:
    rows = []
    for name, qty in items:
        rows.append(
            '<div class="item"><span>{}</span><span>{}</span></div>'.format(
                html.escape(name), html.escape(qty)
            )
        )
    return "\n      ".join(rows)


def render_card(
    recipient: str,
    message: str,
    items: list[tuple[str, str]],
    timestamp: str,
    out: str | Path = "output/card.png",
    subtitle: str = "有人给你点了麦当劳",
    eta: str | None = None,
    preview: bool = False,
    brand: str = "mcd-treat-card",
) -> Path:
    """渲染一张请客卡。

    preview=True  → 预览卡（带红色「预览」角标，不含订单信息）
    preview=False + eta → 正式卡（含订单尾号与预计送达）
    """
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"找不到卡片模板：{TEMPLATE}")

    tpl = Template(TEMPLATE.read_text(encoding="utf-8"))

    badge_html = '<div class="badge">预览</div>' if preview else ""
    eta_html = '<div class="tail">{}</div>'.format(html.escape(eta)) if eta else ""

    page_html = tpl.substitute(
        badge_html=badge_html,
        recipient=html.escape(recipient),
        subtitle=html.escape(subtitle),
        message=html.escape(message),
        items_html=_item_rows(items),
        timestamp=html.escape(timestamp),
        eta_html=eta_html,
        brand=html.escape(brand),
    )

    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "未安装 playwright。请先执行：\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from e

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1080, "height": 1440})
            page.set_content(page_html, wait_until="load")
            page.screenshot(path=str(out_path))
        finally:
            browser.close()

    return out_path


if __name__ == "__main__":
    # 无接口依赖的冒烟测试：用假数据渲染一张预览卡
    path = render_card(
        recipient="张 伟",
        message="改完 bug 记得吃，别熬太晚",
        items=[("板烧鸡腿堡", "×1"), ("麦辣鸡翅", "×2"), ("可乐（中）", "×1")],
        timestamp="2026.10.23  21:47",
        preview=True,
        out="output/sample_preview.png",
    )
    print(f"已生成：{path}")
