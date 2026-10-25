# 一次性脚本：把「麦麦请客卡」标题从原 SVG 中分离
# 产物：
#   _tmp_svg/card_notitle.svg   —— 去掉标题后的底图 SVG（用于重新导出 card_bg.webp）
#   _tmp_svg/card_title_2x.png  —— 标题独立透明 PNG（@2x，已裁边）
# 并打印标题在原稿中的精确位置（SVG 坐标与卡片坐标）
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
# 从「已抠掉动态文字」的版本出发，避免把原稿丝带/留言等文字又带回来
SRC = ROOT / "_tmp_svg" / "card_textless.svg"
OUT_NOTITLE = ROOT / "_tmp_svg" / "card_notitle.svg"
OUT_TITLE_PNG = ROOT / "_tmp_svg" / "card_title_2x_full.png"

# 标题判定区域（SVG 坐标，1728x2304）：标题字符分布带
# 区域内所有小路径（含深色笔画/白色高光/浅色水彩阴影）一律视为标题的一部分
TITLE_RECT = {"x0": 260, "y0": 80, "x1": 1500, "y1": 330}


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1800, "height": 2400})
    pg.set_content(
        '<!DOCTYPE html><html><body style="margin:0">'
        + SRC.read_text(encoding="utf-8")
        + "</body></html>",
        wait_until="load",
    )
    pg.wait_for_timeout(800)
    rep = pg.evaluate(
        """(cfg) => {
      const svgEl = document.querySelector('svg');
      const sr = svgEl.getBoundingClientRect();
      const R = cfg.rect;
      const titleIdx = [];
      let union = null;
      const fillCount = {};
      const all = Array.from(document.querySelectorAll('path'));
      all.forEach((p, i) => {
        const r = p.getBoundingClientRect();
        const w = r.width, h = r.height;
        if (w > 320 || h > 320 || w < 2 || h < 2) return;
        const cx = r.left - sr.left + w / 2, cy = r.top - sr.top + h / 2;
        if (cx < R.x0 || cx > R.x1 || cy < R.y0 || cy > R.y1) return;
        // 路径整体必须落在字符带内：排除跨带的背景大水彩斑
        const top = r.top - sr.top, bottom = r.top - sr.top + h;
        if (top < 95 || bottom > 325) return;
        // 区域内小路径无差别归入标题（不按颜色筛选，避免浅色高光/阴影漏网）
        titleIdx.push(i);
        fillCount[(p.getAttribute('fill') || 'none')] = (fillCount[(p.getAttribute('fill') || 'none')] || 0) + 1;
        if (!union) union = { x1: r.left - sr.left, y1: r.top - sr.top, x2: r.left - sr.left + w, y2: r.top - sr.top + h };
        else {
          union.x1 = Math.min(union.x1, r.left - sr.left);
          union.y1 = Math.min(union.y1, r.top - sr.top);
          union.x2 = Math.max(union.x2, r.left - sr.left + w);
          union.y2 = Math.max(union.y2, r.top - sr.top + h);
        }
      });
      // 生成两个版本
      const notitlePaths = all.filter((p, i) => !titleIdx.includes(i));
      const titleOnlyPaths = all.filter((p, i) => titleIdx.includes(i));
      const strip = (paths) => {
        const clone = svgEl.cloneNode(true);
        clone.querySelectorAll('path').forEach(el => el.remove());
        // 按原 DOM 顺序把保留路径放回（保持原有 <g> 结构已无效，直接平铺到根）
        paths.forEach(el => clone.appendChild(document.importNode(el, true)));
        return clone.outerHTML;
      };
      return {
        n: titleIdx.length,
        union,
        fills: Object.entries(fillCount).sort((a, b) => b[1] - a[1]).slice(0, 12),
        notitle: strip(notitlePaths),
        titleOnly: strip(titleOnlyPaths),
      };
    }""",
        {"rect": TITLE_RECT},
    )
    print("标题路径数:", rep["n"])
    print("标题 union(SVG 坐标):", json.dumps(rep["union"]))
    u = rep["union"]
    print("标题 union(卡片坐标 x0.625): x %.0f-%.0f  y %.0f-%.0f" % (
        u["x1"] * 0.625, u["x2"] * 0.625, u["y1"] * 0.625, u["y2"] * 0.625))
    print("主要填充色:", rep["fills"])
    OUT_NOTITLE.write_text(rep["notitle"], encoding="utf-8")
    Path("_tmp_svg/card_titleonly.svg").write_text(rep["titleOnly"], encoding="utf-8")
    print("saved:", OUT_NOTITLE.name, len(rep["notitle"]), "| titleonly", len(rep["titleOnly"]))

    # 渲染标题独立透明 PNG（@2x）
    pg2 = b.new_page(viewport={"width": 1800, "height": 2400}, device_scale_factor=2)
    pg2.set_content(
        '<!DOCTYPE html><html style="background:transparent"><body style="margin:0;background:transparent">'
        + rep["titleOnly"]
        + "</body></html>",
        wait_until="load",
    )
    pg2.wait_for_timeout(600)
    pg2.query_selector("svg").screenshot(path=str(OUT_TITLE_PNG), omit_background=True)
    b.close()

# 裁掉透明边
from PIL import Image

img = Image.open(OUT_TITLE_PNG)
bbox = img.getbbox()
pad = 10
l = max(0, bbox[0] - pad); t = max(0, bbox[1] - pad)
r = min(img.width, bbox[2] + pad); bo = min(img.height, bbox[3] + pad)
cropped = img.crop((l, t, r, bo))
final = ROOT / "_tmp_svg" / "card_title_2x.png"
cropped.save(final)
print("title png:", final.name, cropped.size)
