# 一次性脚本：从用户提供的 SVG 设计稿中抠掉动态文字路径，导出无文字底图。
# v2：文字是多层水彩路径（主色+浅影+描边），按颜色删不干净；
#     改为在四个文字矩形内无差别删除所有小路径（区域内部均为平坦底色）。
# 用法: python scripts/_extract_textless_bg.py
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "_tmp_svg" / "mcdonalds_card.svg"
OUT_SVG = ROOT / "_tmp_svg" / "card_textless.svg"

svg = SRC.read_text(encoding="utf-8", errors="ignore")

JS = r"""
() => {
  const svgEl = document.querySelector('svg');
  const sr = svgEl.getBoundingClientRect();
  // 四块动态文字区域（SVG 用户坐标 1728x2304）
  const rects = [
    {name:'ribbon', x0:565,  x1:1180, y0:1462, y1:1612},
    {name:'to',     x0:250,  x1:650,  y0:1700, y1:1775},
    {name:'message',x0:380,  x1:1380, y0:1798, y1:1885},
    {name:'items',  x0:300,  x1:1460, y0:2018, y1:2080},
  ];
  const removed={}, unions={};
  const kill=[];
  document.querySelectorAll('path').forEach(p=>{
    const r=p.getBoundingClientRect();
    const w=r.width, h=r.height;
    if (w>300 || h>220 || w<1 || h<1) return;   // 保留大色块（背景/丝带/纸条本体）
    const cx=r.left-sr.left+w/2, cy=r.top-sr.top+h/2;
    rects.forEach(t=>{
      if (cx>=t.x0 && cx<=t.x1 && cy>=t.y0 && cy<=t.y1){
        kill.push(p);
        removed[t.name]=(removed[t.name]||0)+1;
        const u=unions[t.name]=unions[t.name]||{x1:1e9,y1:1e9,x2:-1,y2:-1};
        u.x1=Math.min(u.x1,cx-w/2); u.y1=Math.min(u.y1,cy-h/2);
        u.x2=Math.max(u.x2,cx+w/2); u.y2=Math.max(u.y2,cy+h/2);
      }
    });
  });
  kill.forEach(p=>p.remove());
  return {removed, unions, svg: svgEl.outerHTML};
}
"""

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1800, "height": 2400})
    pg.set_content(
        '<!DOCTYPE html><html><body style="margin:0">' + svg + "</body></html>",
        wait_until="load",
    )
    pg.wait_for_timeout(800)
    rep = pg.evaluate(JS)
    print("removed:", rep["removed"])
    print("unions:", json.dumps(rep["unions"]))
    OUT_SVG.write_text(rep["svg"], encoding="utf-8")
    print("saved:", OUT_SVG, len(rep["svg"]))
    b.close()
