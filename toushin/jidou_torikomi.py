"""自動で、みんかぶの今月ぶんを 8050 へ取り込む（ブックマークを押さずに、全部を最後まで）（2026-10-01）。

ブックマークを押さなくても、月に1回の自動タスクで最後まで取れるようにするための道具。
→ 見えない Edge で、次の順にみんかぶのページを開き、ブックマークと同じ読み方（torikomi.js の toushinExtract）で写して /api/import へ送る。
  1) おまかせの「まだのページ」（8050 の画面の chainSteps＝3ヶ月→6ヶ月→1年→3年のランキング → 比較のページ）
  2) ♡のファンドのうち、今月の成績の月をまだ取っていないものの「リターン」のページ（値動きの荒さ・カテゴリー内の順位）
  3) NISA の印のランキング（成長投資枠・つみたて投資枠）を頭から最後まで。月に1回（今月まだのときだけ）
  4) 取り終わったら、家族に見せる表（tools/toushin_insatsu.py --8050＝3ヶ月の成績順・100位まで）を作り、
     置き場へ「【PDF】投資信託ランキング_2026年11月版.pdf」（＝10月末締め）の名前で置く。その月の版がもうあれば作らない。
     別のPCで刷る用に、同じ表の HTML を「投資信託ランキング」フォルダへ「【印刷用】投資信託ランキング_2026年11月版.html」で置く
  読み方・選び方・入れ方は 8050 のものをそのまま使う（ここに2つ目を書かない）。1ページごとに1秒あける。
  みんかぶがまだ新しい月になっていなければ、そこで止める（終わりの番号 3）。

  python jidou_torikomi.py                 300位まで取る（全部で15〜20分）
  python jidou_torikomi.py --depth 100     ランキングは100位まで
  python jidou_torikomi.py --tsuchi        終わったら Discord へ結果を1通（毎朝の見回り notify.py が使う）
  8050 の「自動で取る」ボタン（/api/jidou）も、これを回す。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
JS = (HERE / "torikomi.js").read_text(encoding="utf-8")
MKB = "https://itf.minkabu.jp"
INSATSU = HERE.parent / "tools" / "toushin_insatsu.py"
PDF_DIR = Path(os.environ.get("TOUSHIN_PDF_DIR") or r"<置き場>")
PDF_RE = re.compile(r"作りました：(.+?\.pdf)（(\d+)ページ）")
NISA_PAGES = (("yield_nisa_growth", "NISA成長投資枠", 200), ("yield_nisa", "つみたて投資枠", 60))   # 最後のページの上限（念のため）


def _post(url: str, payload: dict) -> tuple[bool, dict]:
    req = urllib.request.Request(url + "/api/import", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return True, json.load(r)
    except urllib.error.HTTPError as ex:
        try:
            return False, json.load(ex)
        except ValueError:
            return False, {"detail": str(ex)}


def _ui(page, url: str, depth: int) -> dict:
    """8050 の画面を開き直して、おまかせの「まだのページ」と、いまのデータの様子を聞く。"""
    page.goto(url + "/torikomi", wait_until="domcontentloaded")
    page.wait_for_function("() => S.data !== null", timeout=60000)
    return page.evaluate("""d => { S.depth = d; return {steps: chainSteps(), period: S.data.period, stars: [...S.stars],
        pages: S.data.page_dates || {}}; }""", depth)


def _read(page, mk_url: str) -> dict:
    """みんかぶのページを開いて、ブックマークと同じ読み方で写す。{d:中身} か {a:理由}"""
    page.goto(mk_url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("table tr", timeout=20000)
    except Exception:
        pass
    page.evaluate("window.__TOUSHIN_PAGE__ = true")
    page.add_script_tag(content=JS)
    return page.evaluate("() => { const r = toushinExtract(document, location, false); return r.u ? {a: 'このページは読めませんでした'} : r; }")


def _nisa_state() -> tuple[Path, dict]:
    """NISA のランキングを最後のページまで取り切った月（data/jidou_nisa.json・{which: "2026-10"}）。
    途中まで手で取った月は「済み」にしない。"""
    import store
    f = store.DATA / "jidou_nisa.json"
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
        return f, st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return f, {}


def pdf_name(need: str) -> str:
    """成績の締め（先月の末）の次の月の版。10月末締め → 2026年11月版。"""
    d = date.fromisoformat(need)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return "【PDF】投資信託ランキング_%d年%d月版.pdf" % (y, m)


def pdf_tsukuru(url: str, need: str, log=print) -> dict:
    """家族に見せる表を作って、置き場へ置く。{state: made／had／ng, path, why}"""
    dst = PDF_DIR / pdf_name(need)
    if dst.exists():
        return {"state": "had", "path": str(dst)}
    if not PDF_DIR.is_dir():
        return {"state": "ng", "why": "置き場（%s）が見えません" % PDF_DIR}
    log("家族に見せる表（PDF）を作っています…")
    pr = subprocess.run([sys.executable, str(INSATSU), "--8050"], capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=1800, env={**os.environ, "TOUSHIN_URL": url, "PYTHONIOENCODING": "utf-8"},
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    m = PDF_RE.search(pr.stdout or "")
    if pr.returncode or not m:
        tail = ((pr.stderr or "") + (pr.stdout or "")).strip().splitlines()[-1:] or ["?"]
        return {"state": "ng", "why": "表が作れませんでした（%s）" % tail[0][:200]}
    shutil.copyfile(m.group(1), dst)
    log("置き場へ置きました：%s（%sページ）" % (dst.name, m.group(2)))
    # 別のPCで刷る用の HTML（そのPCの toushin_insatsu.py --html <これ> --insatsu が Edge で刷る。PDF は刷れないため）
    src_html = Path(m.group(1)).with_suffix(".html")
    if src_html.exists():
        sub = PDF_DIR / "投資信託ランキング"
        sub.mkdir(exist_ok=True)
        shutil.copyfile(src_html, sub / dst.name.replace("【PDF】", "【印刷用】").replace(".pdf", ".html"))
    return {"state": "made", "path": str(dst), "pages": int(m.group(2))}


def toru(url: str, depth: int, log=print) -> dict:
    """全部を取る。返すのは {state: done／mada／stop, rank, compare, fund, nisa, why}"""
    from playwright.sync_api import sync_playwright
    t = date.today()
    need = date.fromordinal(date(t.year, t.month, 1).toordinal() - 1).isoformat()      # 先月の末
    got = {"state": "done", "rank": 0, "compare": 0, "fund": 0, "nisa": 0, "why": None, "need": need}

    def stop(state, why):
        got.update(state=state, why=why)
        log(why)
        return got

    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        try:
            ui = br.new_page()
            mk = br.new_page(locale="ja-JP")
            # 1) おまかせのページ
            last = None
            while True:
                info = _ui(ui, url, depth)
                st = info["steps"]
                if not st:
                    break
                s = st[0]
                if s["url"] == last:        # 同じページがまだ残る＝入らなかった。回り続けないように止める
                    return stop("stop", "「%s」が入りませんでした。ここで止めました。" % s["label"])
                last = s["url"]
                r = _read(mk, s["url"])
                if "d" not in r:
                    return stop("stop", "「%s」：%s。ここで止めました。" % (s["label"], r.get("a")))
                ok, out = _post(url, r["d"])
                if not ok:
                    return stop("stop", "「%s」：8050 が受け取りませんでした（%s）。ここで止めました。" % (s["label"], out.get("detail")))
                got["rank" if s.get("rank") else "compare"] += 1
                log("  %s … 入りました（あと %d）" % (s["label"], len(st) - 1))
                if out.get("kind") == "rank" and out.get("date", "") < need:
                    return stop("mada", "みんかぶが、まだ %s 末までの成績になっていないようです。" % need[:7])
                time.sleep(1)
            if (info.get("period") or "") < need:
                return stop("mada", "みんかぶが、まだ %s 末までの成績になっていないようです。" % need[:7])
            # 2) ♡のファンドの「リターン」のページ（今月の成績の月をまだ取っていないもの）
            todo = [f for f in info["stars"] if (info["pages"].get(f) or "") < info["period"]]
            for n, fid in enumerate(todo, 1):
                r = _read(mk, "%s/fund/%s/return" % (MKB, fid))
                ok, out = _post(url, r["d"]) if "d" in r else (False, {"detail": r.get("a")})
                if ok:
                    got["fund"] += 1
                else:
                    log("  ♡ %s：入りませんでした（%s）" % (fid, out.get("detail")))
                log("  ♡のファンドのページ %d / %d" % (n, len(todo)))
                time.sleep(1)
            # 3) NISA の印（月に1回、頭から最後まで）
            nf, ns = _nisa_state()
            month = date.today().strftime("%Y-%m")
            for which, label, cap in NISA_PAGES:
                if ns.get(which) == month:
                    continue
                prev = None
                for pg in range(1, cap + 1):
                    r = _read(mk, "%s/ranking/%s?page=%d" % (MKB, which, pg))
                    ids = [x[0] for x in r["d"].get("rows", [])] if "d" in r else None
                    if ids == prev:         # 最後を過ぎると同じページが返るとき
                        r = {}
                    prev = ids
                    if "d" not in r:        # 表が無い＝最後のページを過ぎた。取り切ったので、今月は済み
                        if pg > 1:
                            ns[which] = month
                            nf.write_text(json.dumps(ns, ensure_ascii=False), encoding="utf-8")
                        break
                    ok, out = _post(url, r["d"])
                    if not ok:
                        log("  %s の %dページ目：入りませんでした（%s）" % (label, pg, out.get("detail")))
                        break
                    got["nisa"] += 1
                    if pg % 10 == 0:
                        log("  %s … %dページ目まで" % (label, pg))
                    time.sleep(1)
        finally:
            br.close()
    log("今月ぶんは、全部取り込めました（ランキング %dページ・まとめて取る %d回・♡のファンド %d本・NISA %dページ）。"
        % (got["rank"], got["compare"], got["fund"], got["nisa"]))
    try:
        got["pdf"] = pdf_tsukuru(url, need, log)
    except Exception as ex:             # noqa: BLE001 — 表でつまずいても、取り込みは済んでいる
        got["pdf"] = {"state": "ng", "why": "表が作れませんでした（%s）" % ex}
    if got["pdf"]["state"] == "ng":
        log(got["pdf"]["why"])
    return got


def _pdf_line(pdf: dict) -> str:
    if pdf.get("state") == "made":
        return "置き場に「%s」を置きました📄\n" % Path(pdf["path"]).name
    if pdf.get("state") == "ng":
        return "（%s。tools/toushin_insatsu.py --8050 で作れます）\n" % pdf["why"]
    return ""


def tsuchi(got: dict) -> None:
    """Discord へ結果を1通（まだ新しい月でないときは送らない＝あしたの朝にまた見る）。"""
    import notify
    if got["state"] == "mada":
        return
    hi = date.fromisoformat(got["need"])
    if got["state"] == "done":
        text = (f"投資信託ランキングを、{hi.year}/{hi.month:02d} 末までの成績に自動で入れかえました📈\n"
                f"ランキング {got['rank']}ページ・まとめて取る {got['compare']}回・♡のファンド {got['fund']}本・NISA {got['nisa']}ページ\n"
                f"{_pdf_line(got.get('pdf') or {})}→ {notify.PAGE_URL}")
    else:
        text = (f"投資信託ランキングの自動の取り込みが、途中で止まりました。\n{got['why']}\n"
                f"投資信託の画面の「自動で取る」か「はじめる」で続きをお願いします → {notify.PAGE_URL}")
    w = notify.webhook_url()
    out = notify.send(w, text) if w else {"sent": False, "error": "Discord の宛先がありません"}
    print(json.dumps(out, ensure_ascii=False))
    if out.get("sent"):
        notify.mark_sent(date.today())


def main() -> int:
    ap = argparse.ArgumentParser(description="自動で、みんかぶの今月ぶんを 8050 へ取り込む")
    ap.add_argument("--url", default="http://127.0.0.1:8050")
    ap.add_argument("--depth", type=int, default=300, choices=[60, 100, 200, 300], help="ランキングを何位まで（既定300）")
    ap.add_argument("--tsuchi", action="store_true", help="終わったら Discord へ結果を1通")
    a = ap.parse_args()
    try:
        got = toru(a.url, a.depth, log=lambda s: print(s, flush=True))
    except Exception as ex:             # noqa: BLE001 — Edge やみんかぶでつまずいても、知らせて終わる
        t = date.today()
        got = {"state": "stop", "why": "つまずきました（%s）" % ex,
               "need": date.fromordinal(date(t.year, t.month, 1).toordinal() - 1).isoformat()}
        print(got["why"], flush=True)
    if a.tsuchi:
        tsuchi(got)
    return {"done": 0, "mada": 3}.get(got["state"], 1)


if __name__ == "__main__":
    sys.exit(main())
