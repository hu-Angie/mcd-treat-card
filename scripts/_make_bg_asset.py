# 一次性脚本：无动态文字 SVG（标题保留原位）-> 1080x1440(@2x) PNG -> WebP 背景资源
# 定稿基准 = output/sample_final.png：标题「麦麦请客卡」烧在底图顶部原位，
# 不再分离/下移，因此无需任何残影修补。
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "_tmp_svg" / "card_textless.svg"
PNG = ROOT / "_tmp_svg" / "card_textless_2x.png"
ASSET_DIR = ROOT / "templates" / "assets"
ASSET_DIR.mkdir(exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1800, "height": 2400}, device_scale_factor=2)
    pg.set_content(
        '<!DOCTYPE html><html><body style="margin:0">'
        + SRC.read_text(encoding="utf-8")
        + "</body></html>",
        wait_until="load",
    )
    pg.wait_for_timeout(1200)
    pg.query_selector("svg").screenshot(path=str(PNG))
    b.close()
print("png:", PNG, PNG.stat().st_size)

# 转 WebP
from PIL import Image

img = Image.open(PNG).convert("RGB")
print("png size:", img.size)

out = ASSET_DIR / "card_bg.webp"
img.save(out, "WEBP", quality=88, method=6)
print("webp:", out, out.stat().st_size)
# 备选：JPEG 以防 webp 不可用
out_j = ASSET_DIR / "card_bg.jpg"
img.save(out_j, "JPEG", quality=90, optimize=True)
print("jpg:", out_j, out_j.stat().st_size)
