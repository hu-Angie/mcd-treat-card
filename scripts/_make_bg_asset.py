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
# 托板(HTML)已下移到 card y220，残影在托板以上整段露出，
# 这里把 card y45-168（拍立得白框顶边之前）用上方干净带(card y0-45)
# 翻转拉伸后羽化贴回。
W, H = img.size  # 3456x4608
px_x0, px_x1 = 416, 3072          # card x130-960
dst_y0, dst_y1 = 144, 538         # card y45-168
src = img.crop((px_x0, 0, px_x1, 144)).transpose(Image.FLIP_TOP_BOTTOM)
src = src.resize((px_x1 - px_x0, dst_y1 - dst_y0), Image.LANCZOS)
mask = Image.new("L", src.size, 0)
from PIL import ImageDraw

d = ImageDraw.Draw(mask)
d.rectangle((16, 16, src.size[0] - 16, src.size[1] - 16), fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(10))
img.paste(src, (px_x0, dst_y0), mask)
print("ghost patched")

# —— 白框顶部残迹提白：残影下缘延伸进拍立得白框(card y166-208)，向白框底色融合 ——
wx0, wx1 = 440, 3020               # card x137-944
wy0, wy1 = 531, 666                # card y166-208
region = img.crop((wx0, wy0, wx1, wy1))
white = Image.new("RGB", region.size, (251, 246, 239))
whitened = Image.blend(region, white, 0.62)
wmask = Image.new("L", region.size, 0)
d2 = ImageDraw.Draw(wmask)
d2.rectangle((20, 0, region.size[0] - 20, region.size[1]), fill=160)
wmask = wmask.filter(ImageFilter.GaussianBlur(12))
img.paste(whitened, (wx0, wy0), wmask)
print("polaroid top whitened")

out = ASSET_DIR / "card_bg.webp"
img.save(out, "WEBP", quality=88, method=6)
print("webp:", out, out.stat().st_size)
# 备选：JPEG 以防 webp 不可用
out_j = ASSET_DIR / "card_bg.jpg"
img.save(out_j, "JPEG", quality=90, optimize=True)
print("jpg:", out_j, out_j.stat().st_size)
