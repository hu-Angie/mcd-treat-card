# 一次性脚本：无文字 SVG -> 1080x1440(@2x) PNG -> WebP 背景资源
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "_tmp_svg" / "card_notitle.svg"
PNG = ROOT / "_tmp_svg" / "card_notitle_2x.png"
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
from PIL import Image, ImageFilter

img = Image.open(PNG).convert("RGB")
print("png size:", img.size)

# —— 修补标题幽灵残留 ——
# 标题浅色阴影与背景水彩斑合并在同一大 path 里无法按路径抠除；
# 托板(HTML)盖住 card y112 以下，这里把托板上沿以上露出的
# 残影条带(card y50-115 -> 2x y160-368)用上方干净带(card y0-50)翻转拉伸后羽化贴回。
W, H = img.size  # 3456x4608
px_x0, px_x1 = 416, 3072          # card x130-960
dst_y0, dst_y1 = 160, 368         # card y50-115
src = img.crop((px_x0, 0, px_x1, 152)).transpose(Image.FLIP_TOP_BOTTOM)
src = src.resize((px_x1 - px_x0, dst_y1 - dst_y0), Image.LANCZOS)
mask = Image.new("L", src.size, 0)
from PIL import ImageDraw

d = ImageDraw.Draw(mask)
d.rectangle((16, 16, src.size[0] - 16, src.size[1] - 16), fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(10))
img.paste(src, (px_x0, dst_y0), mask)
print("ghost patched")

out = ASSET_DIR / "card_bg.webp"
img.save(out, "WEBP", quality=88, method=6)
print("webp:", out, out.stat().st_size)
# 备选：JPEG 以防 webp 不可用
out_j = ASSET_DIR / "card_bg.jpg"
img.save(out_j, "JPEG", quality=90, optimize=True)
print("jpg:", out_j, out_j.stat().st_size)
