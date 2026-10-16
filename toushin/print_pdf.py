"""投資信託ランキングを、印刷用の PDF にして Google ドライブへ置く係（2026-10-01）。

別のPCで印刷したいので、Google ドライブ経由で渡す。

  python print_pdf.py               いま1回作る
  python print_pdf.py --if-changed  前の PDF のあとにデータが変わっていたときだけ（毎朝9時の見回り notify.py から）

中身：この PC の 8050 の画面を「表」で開いて、そのまま PDF にする（A4 横）。並びは「平均」（3ヶ月・6ヶ月・1年・3年の
順位をならした順）の上位100本。列は画面の表と同じ（期間ごとの順位・1年・3年・5年・荒さ・効率・信託報酬・評価・NISA）。
画面を開くときは、自動の「取る」の印とつぎのページを自動で開くを切ってから開く（本番のデータを1つも変えない）。
置き場：<置き場>\\投資信託ランキング\\投資信託ランキング_YYYY-MM-DD.pdf（新しい6個を残す）。
Google ドライブで同期されるので、刷るPCで開いて刷る。置き場が見えないときは、何もしないで理由だけ返す。
TOUSHIN_PRINT_DIR・TOUSHIN_PRINT_BASE で、確かめ役が一時フォルダ・別のポートに向ける。
開くのは、この PC の 8050 の画面だけ（ほかの住所への通信は止める）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import store

DEST = Path(os.environ.get("TOUSHIN_PRINT_DIR") or r"<置き場>\投資信託ランキング")
BASE = os.environ.get("TOUSHIN_PRINT_BASE") or "http://127.0.0.1:8050"
KEEP = 6
# 画面の設定（localStorage）：平均・表・並びは元のまま・自動の印は付けない・つぎを自動で開かない
VIEW = {"tab": "avg", "avg": ["3", "6", "12", "36"], "view": "table", "sort": None, "autoAvg": False, "autoNext": False}
FOOTER = ('<div style="width:100%;font-size:8px;color:#666;text-align:center;font-family:sans-serif">'
          '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')


def _changed_since(last: Path | None) -> bool:
    if last is None:
        return True
    newest = max((p.stat().st_mtime for p in (store.DB, store.STARS, store.HOLDINGS) if p.exists()), default=0)
    return newest > last.stat().st_mtime


def make(today: date | None = None, if_changed: bool = False, pw=None) -> dict:
    """PDF を1つ作る。pw＝確かめ役が開いている playwright（無ければ、ここで開く）。"""
    today = today or date.today()
    if not DEST.parent.exists():
        return {"made": False, "why": f"置き場が見えません（{DEST.parent}）"}
    DEST.mkdir(exist_ok=True)
    olds = sorted(DEST.glob("投資信託ランキング_*.pdf"))
    if if_changed and not _changed_since(olds[-1] if olds else None):
        return {"made": False, "why": "前の PDF から、データは変わっていません"}
    out = DEST / f"投資信託ランキング_{today.isoformat()}.pdf"
    part = out.with_suffix(".part")
    if pw is None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            _render(p, part)
    else:
        _render(pw, part)
    part.replace(out)
    removed = []
    for old in sorted(DEST.glob("投資信託ランキング_*.pdf"))[:-KEEP]:
        old.unlink()
        removed.append(old.name)
    return {"made": True, "file": str(out), "removed": removed}


def _render(pw, path: Path) -> None:
    browser = pw.chromium.launch()
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        # この PC の画面のほかへは、1本も通信しない（みんかぶの住所は、画面の中のリンクにあるだけ）
        ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(BASE) else r.abort())
        ctx.add_init_script("try { localStorage.setItem('toushin', JSON.stringify(%s)); } catch (e) {}" % json.dumps(VIEW))
        page = ctx.new_page()
        page.goto(BASE + "/", wait_until="load")
        page.wait_for_selector("#grid tbody tr", timeout=30000)
        page.emulate_media(media="print")
        page.pdf(path=str(path), format="A4", landscape=True, print_background=False,
                 margin={"top": "10mm", "bottom": "12mm", "left": "8mm", "right": "8mm"},
                 display_header_footer=True, header_template="<div></div>", footer_template=FOOTER)
    finally:
        browser.close()


if __name__ == "__main__":
    print(date.today().isoformat(), "印刷用の PDF", json.dumps(make(if_changed="--if-changed" in sys.argv), ensure_ascii=False))
