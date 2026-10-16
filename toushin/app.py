"""投資信託ランキング（8050）。みんかぶで「取り込む」を押した分を受け取って、貯めて、画面に出す。

  python app.py        http://<この PC>:8050
  TOUSHIN_PORT / TOUSHIN_DATA で、確かめ役が別のポート・別のフォルダに向ける。

データが入ってくるのは /api/import と、Google ドライブのファイル（import_file.scan）です。
「自動で取る」（/api/jidou）は、jidou_torikomi.py が見えない Edge でみんかぶを開き、/api/import へ送ります。
同じPCの、ほかのアプリには触りません。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

import import_file
import minkabu
import store

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("TOUSHIN_PORT") or 8050)
PAGE_SIZE = 20
ID_RE = re.compile(r"[0-9A-Za-z]{8}")
ALLOWED_TERMS = (3, 6, 12, 36)
BOOKMARKLET_VERSION = int(re.search(r"V=(\d+)", (HERE / "torikomi.js").read_text(encoding="utf-8")).group(1))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _page() -> FileResponse:
    return FileResponse(HERE / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


@app.get("/")
def index():
    return _page()


@app.get("/torikomi")
def torikomi():
    return _page()


@app.get("/api/latest")
def api_latest():
    out = store.latest()
    out["drop"] = import_file.status()      # Google ドライブから自動で入れている様子（取り込みの面に1行）
    return out


@app.get("/api/fund/{fid}")
def api_fund(fid: str):
    if not ID_RE.fullmatch(fid):
        raise HTTPException(400, "ID の形が違います")
    return {"id": fid, "history": store.history(fid)}


@app.get("/api/bookmarklet")
def api_bookmarklet(request: Request):
    """「取り込む」をブックマークに入れる形（javascript:…）で返す。戻り先は、いま開いているこの画面の住所。"""
    origin = str(request.base_url).rstrip("/")
    if not re.fullmatch(r"https?://[0-9A-Za-z.\-\[\]:]+", origin):
        raise HTTPException(400, "この住所からは作れません")
    src = (HERE / "torikomi.js").read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    code = "".join(line.strip() for line in src.splitlines()).replace("__ORIGIN__", origin)
    return {"code": "javascript:" + quote(code, safe="()!*'-._~"), "origin": origin, "v": BOOKMARKLET_VERSION}


@app.get("/extract.js")
def extract_js():
    """同じ読み方を、この画面の「貼り付けて取り込む」にも渡す（読み方を2か所に書かないため）。"""
    return Response((HERE / "torikomi.js").read_text(encoding="utf-8"),
                    media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "no-store"})


async def _json_body(request: Request) -> dict:
    """この画面が送った JSON だけ受ける（よそのページからの素の送信は Content-Type が違うので、ここで断る）。"""
    if "application/json" not in (request.headers.get("content-type") or ""):
        raise HTTPException(415, "JSON で送ってください")
    try:
        p = await request.json()
    except Exception:
        raise HTTPException(400, "読めない形でした")
    if not isinstance(p, dict):
        raise HTTPException(400, "読めない形でした")
    return p


def _int(v, lo: int, hi: int) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def _f(v, lo: float, hi: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if lo <= v <= hi else None


def _nav_day(text, time_text=None) -> str:
    """ランキングのページに出ている基準価額が、いつの値段か。

    ページの「更新：2026/10/01 07:09」の日を元にする。みんかぶは夜（21時ごろ）にその日の基準価額へ変わるので、
    18時より前の更新なら、値段は前の営業日のもの（10/1 の朝に見たページの値段は 9/30 のものだった）。
    祝日は見分けない（土日だけ飛ばす）。形がおかしい・今日より先・4日より前なら、今日を元にする。
    """
    today = date.today()
    day = today
    if isinstance(text, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", text):
        try:
            d = date.fromisoformat(text)
            if today - timedelta(days=4) <= d <= today + timedelta(days=1):
                day = d
        except ValueError:
            pass
    m = re.fullmatch(r"(\d\d?):\d\d", time_text) if isinstance(time_text, str) else None
    if m and int(m.group(1)) < 18:
        day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
    return day.isoformat()


def _period(span, nav_day: str) -> tuple[str, str | None]:
    """成績の期間（"2026/03~2026/08"）から、成績の月（末日）を決める。読めなければ、基準価額の日の前の月の末日。"""
    day = date.fromisoformat(nav_day)
    if isinstance(span, str):
        m = re.fullmatch(r"(\d{4})/(\d\d)~(\d{4})/(\d\d)", span)
        if m and 1 <= int(m.group(4)) <= 12 and 1 <= int(m.group(2)) <= 12:
            end = store.month_end(int(m.group(3)), int(m.group(4)))
            if day - timedelta(days=130) <= end <= day + timedelta(days=1):
                return end.isoformat(), span
    return store.prev_month_end(day).isoformat(), None


DATE_RE = re.compile(r"(19|20)\d\d-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")
WORD_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uff10-\uff19A-Za-z0-9・ー（）()]{1,20}")


def _day(v) -> str | None:
    if not (isinstance(v, str) and DATE_RE.fullmatch(v)):
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def _word(v, n: int) -> str | None:
    return v[:n] if isinstance(v, str) and WORD_RE.fullmatch(v[:n]) else None


def _compare_info(r: dict) -> dict:
    """比較のページの、なりたちの行（ブックマークの版6から）。形が違う値は入れない（前の値のまま）。
    bf＝買うときの手数料（上限・税込 %）・dl／dy＝分配金（前回・1年累計 円）・st＝決算回数・sd＝設定日・
    ud＝信託期間の終わり（"none"＝無期限）・rr＝みんかぶの RR分類"""
    ud = r.get("ud")
    return {"buy_fee": _f(r.get("bf"), 0, 10), "dist_last": _f(r.get("dl"), 0, 1e6), "dist_year": _f(r.get("dy"), 0, 1e7),
            "settle": _word(r.get("st"), 10), "setup": _day(r.get("sd")),
            "until": "none" if ud == "none" else _day(ud), "rr": _word(r.get("rr"), 20)}


@app.post("/api/import")
async def api_import(request: Request):
    p = await _json_body(request)

    if p.get("kind") == "rank":
        term = _int(p.get("term"), 1, 1000)
        if term not in ALLOWED_TERMS:
            raise HTTPException(400, "3ヶ月・6ヶ月・1年・3年のランキングで押してください")
        raw = p.get("rows")
        rows = []
        for r in (raw if isinstance(raw, list) else [])[:60]:
            if not (isinstance(r, list) and len(r) >= 9 and _int(r[0], 1, 5000)
                    and isinstance(r[1], str) and ID_RE.fullmatch(r[1])):
                continue
            rows.append({"rank": r[0], "id": r[1], "name": str(r[2] or "")[:120], "company": str(r[3] or "")[:40],
                         "nav": _f(r[4], 0, 1e8), "nav_diff": _f(r[5], -1e7, 1e7),
                         "ret12": _f(r[6], -100, 100000), "fee": _f(r[7], 0, 20), "rating": _int(r[8], 1, 5)})
        if not rows:
            raise HTTPException(400, "ランキングの行が読めませんでした")
        nav_day = _nav_day(p.get("date"), p.get("time"))
        period, span = _period(p.get("span"), nav_day)
        out = store.import_ranking(period, nav_day, term, rows, span)
        out["old"] = (_int(p.get("v"), 0, 1000) or 0) < BOOKMARKLET_VERSION     # 入ったけれど、ブックマークの中身が古い
        if out["stale"]:        # 開いたままの古いタブだった：同じページを開き直してもらう
            page = (out["first"] - 1) // PAGE_SIZE + 1
            out["next"] = {"url": minkabu.ranking_url(term, page), "first": out["first"], "last": out["last"], "reload": True}
        elif out["last"] and out["last"] % PAGE_SIZE == 0:
            out["next"] = {"url": minkabu.ranking_url(term, out["last"] // PAGE_SIZE + 1),
                           "first": out["last"] + 1, "last": out["last"] + PAGE_SIZE}
        return out

    if p.get("kind") == "fund":
        fid = p.get("id")
        if not (isinstance(fid, str) and ID_RE.fullmatch(fid)):
            raise HTTPException(400, "ファンドの ID が読めませんでした")
        md = p.get("md") if isinstance(p.get("md"), str) and re.fullmatch(r"\d\d/\d\d", p["md"]) else None
        returns, ranks = {}, {}
        for key, v in (p["r"].items() if isinstance(p.get("r"), dict) else ()):
            if str(key).isdigit() and int(key) in store.RETURN_TERMS and _f(v, -100, 100000) is not None:
                returns[int(key)] = float(v)
        for key, v in (p["k"].items() if isinstance(p.get("k"), dict) else ()):
            if str(key).isdigit() and int(key) in store.RETURN_TERMS and _int(v, 1, 20000):
                ranks[int(key)] = v
        if not returns:
            raise HTTPException(400, "リターンの表が読めませんでした")
        # 値動きの荒さ（s・その順位 sk）と、荒さの割の増え方（p・その順位 pk）
        risk: dict = {}
        for key, lo, hi, as_int in (("s", 0, 1000, False), ("sk", 1, 20000, True), ("p", -100, 100, False), ("pk", 1, 20000, True)):
            got = p.get(key)
            risk[key] = {}
            for term, v in (got.items() if isinstance(got, dict) else ()):
                if not (str(term).isdigit() and int(term) in store.RISK_TERMS):
                    continue
                val = _int(v, lo, hi) if as_int else _f(v, lo, hi)
                if val is not None:
                    risk[key][int(term)] = val
        raw_nisa = p.get("nisa")
        nisa = ([str(x)[:30] for x in raw_nisa if isinstance(x, str)][:4]
                if isinstance(raw_nisa, list) else None)
        nav_day = store.nav_day_from_md(md)
        out = store.import_fund(store.fund_period(nav_day), nav_day, fid, str(p.get("name") or "")[:120],
                                _f(p.get("nav"), 0, 1e8), _f(p.get("nd"), -1e7, 1e7), returns, ranks, risk, nisa)
        store.set_star(fid, True)       # ページを取り込んだファンドは、追っているものとして♡に入れる
        out["old"] = (_int(p.get("v"), 0, 1000) or 0) < BOOKMARKLET_VERSION
        return out

    if p.get("kind") == "compare":
        rows = []
        # 1回に受け取る本数の上限。画面で選べる最大（64本）より多くしておく（少ないと「みんかぶが◯本までのようです」と誤って出る）
        for r in (p.get("rows") if isinstance(p.get("rows"), list) else [])[:80]:
            if not (isinstance(r, dict) and isinstance(r.get("id"), str) and ID_RE.fullmatch(r["id"])):
                continue

            def years(got, lo, hi):
                out = {}
                for term, v in (got.items() if isinstance(got, dict) else ()):
                    if str(term).isdigit() and int(term) in (12, 36, 60) and _f(v, lo, hi) is not None:
                        out[int(term)] = float(v)
                return out

            rows.append({"id": r["id"], "name": str(r.get("name") or "")[:120], "fee": _f(r.get("fee"), 0, 20),
                         "returns": years(r.get("r"), -100, 100000), "sharpe": years(r.get("p"), -100, 100),
                         "info": _compare_info(r)})
        if not rows:
            raise HTTPException(400, "比較の表のファンドが読めませんでした")
        out = store.import_compare(store.fund_period(date.today().isoformat()), rows)
        out["old"] = (_int(p.get("v"), 0, 1000) or 0) < BOOKMARKLET_VERSION     # 入ったけれど、なりたちの行を読まない古いブックマーク
        out["info"] = sum(1 for r in rows if any(v is not None for v in r["info"].values()))
        asked = _int(p.get("asked"), 1, 1000)
        if asked:
            out["asked"] = asked
        return out

    if p.get("kind") == "nisa":
        which = p.get("which")
        if which not in store.NISA_LABELS:
            raise HTTPException(400, "NISA のランキングのページで押してください")
        rows = []
        for r in (p.get("rows") if isinstance(p.get("rows"), list) else [])[:60]:
            if isinstance(r, list) and len(r) >= 2 and isinstance(r[0], str) and ID_RE.fullmatch(r[0]):
                rows.append((r[0], str(r[1] or "")[:120]))
        if not rows:
            raise HTTPException(400, "NISA のランキングの行が読めませんでした")
        page = _int(p.get("page"), 1, 1000) or 1
        out = store.mark_nisa(which, rows, page)
        if not p.get("pasted"):         # 貼り付けでは何ページ目か分からないので、「次を開く」は出さない
            out["next"] = {"url": f"{minkabu.BASE}/ranking/{which}?page={page + 1}",
                           "first": page * PAGE_SIZE + 1, "last": (page + 1) * PAGE_SIZE}
        return out

    if p.get("kind") == "unknown" or (_int(p.get("v"), 0, 1000) or 0) < BOOKMARKLET_VERSION:
        # ブックマークの中身が古い（v が今より小さい・v が無い＝最初のころのもの）か、読めないページ
        if (_int(p.get("v"), 0, 1000) or 0) < BOOKMARKLET_VERSION:
            raise HTTPException(400, "ブックマークの「取り込む」が古いままです。取り込みの面の「はじめの1回・入れ直し」から入れ直してください"
                                     "（古いものは右クリックで削除）。いまのページは、Ctrl+A → Ctrl+C → この画面で Ctrl+V でも入ります。")
        raise HTTPException(400, "このページは取り込めません。ランキング・ファンドの「リターン」のページ・比較のページ・NISA のランキングで押してください。")

    raise HTTPException(400, "ランキングか、ファンドのリターンのページか、比較のページで押してください")


@app.post("/api/picks")
async def api_picks(request: Request):
    """「取る」の印。op＝add（足す）／remove（外す）／clear（全部外す）。ids は付けたい順。"""
    p = await _json_body(request)
    op, ids = p.get("op"), p.get("ids")
    ids = [x for x in (ids if isinstance(ids, list) else []) if isinstance(x, str) and ID_RE.fullmatch(x)][:1000]
    cur = store.picks()
    if op == "add":
        cur = cur + [x for x in ids if x not in cur]
    elif op == "remove":
        cur = [x for x in cur if x not in ids]
    elif op == "clear":
        cur = []
    else:
        raise HTTPException(400, "op は add・remove・clear のどれか")
    return {"picks": store.set_picks(cur)}


@app.post("/api/holding")
async def api_holding(request: Request):
    """買った値段（1口の基準価額・買った日・買った金額）。nav が無い／remove＝消す。書いたファンドは♡にも入れる。"""
    p = await _json_body(request)
    fid = p.get("id")
    if not (isinstance(fid, str) and ID_RE.fullmatch(fid)):
        raise HTTPException(400, "ID の形が違います")
    if p.get("remove"):
        return {"holdings": store.set_holding(fid, None), "stars": store.stars()}
    if not store.fund_known(fid):
        raise HTTPException(400, "知らないファンドです（ランキングかファンドのページを取り込んでから）")
    nav = _f(p.get("nav"), 1, 1e7)
    if nav is None:
        raise HTTPException(400, "買ったときの基準価額（1万口あたりの円）を入れてください")
    day = _day(p.get("day"))
    if p.get("day") not in (None, "") and not (day and date(1990, 1, 1).isoformat() <= day <= (date.today() + timedelta(days=1)).isoformat()):
        raise HTTPException(400, "買った日は、1990年から今日までの日付で入れてください")
    yen = _f(p.get("yen"), 1, 1e10)
    if p.get("yen") not in (None, "") and yen is None:
        raise HTTPException(400, "買った金額は、1円から100億円までの数字で入れてください")
    hold = {"nav": nav, "day": day, "yen": yen}
    out = store.set_holding(fid, hold)
    stars = store.set_star(fid, True) if fid not in store.stars() else store.stars()
    return {"holdings": out, "stars": stars}


@app.post("/api/star")
async def api_star(request: Request):
    p = await _json_body(request)
    fid = p.get("id")
    if not (isinstance(fid, str) and ID_RE.fullmatch(fid)):
        raise HTTPException(400, "ID の形が違います")
    return {"stars": store.set_star(fid, bool(p.get("on")))}


JIDOU_LOG = store.DATA / "jidou.log"
_jidou = {"proc": None}


@app.post("/api/jidou")
async def api_jidou_start(request: Request):
    """「自動で取る」：jidou_torikomi.py を裏で回す（ブックマークを押さずに、全部を最後まで）。回っている間は2つ目を出さない。
    tsuchi＝終わったら Discord へ1通（毎朝の見回り notify.py が付ける）。"""
    body = await _json_body(request)
    depth = body.get("depth") if body.get("depth") in (60, 100, 200, 300) else 300
    pr = _jidou["proc"]
    if pr is None or pr.poll() is not None:
        fp = open(JIDOU_LOG, "w", encoding="utf-8")
        _jidou["proc"] = subprocess.Popen([sys.executable, str(HERE / "jidou_torikomi.py"), "--url", f"http://127.0.0.1:{PORT}",
                                           "--depth", str(depth)] + (["--tsuchi"] if body.get("tsuchi") is True else []),
                                          cwd=str(HERE), stdout=fp, stderr=subprocess.STDOUT,
                                          env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return api_jidou()


@app.get("/api/jidou")
def api_jidou():
    pr = _jidou["proc"]
    try:
        lines = [x for x in JIDOU_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                 if x.strip() and not x.lstrip().startswith("{")]          # Discord へ送った結果の行は出さない
    except OSError:
        lines = []
    return {"running": pr is not None and pr.poll() is None, "code": None if pr is None else pr.poll(),
            "last": lines[-1].strip() if lines else None}


def _drop_loop() -> None:
    """別のPCで取ったデータ（Google ドライブの 投資信託_*.json）を、5分ごとに見て入れる（import_file.scan）。"""
    while True:
        try:
            for r in import_file.scan():
                print(datetime.now().isoformat(timespec="seconds"), "Google ドライブから", json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as e:          # noqa: BLE001 — 見るところでつまずいても、画面は止めない
            print(datetime.now().isoformat(timespec="seconds"), "Google ドライブを見るところでつまずきました", e, flush=True)
        time.sleep(300)


if __name__ == "__main__":
    # 確かめ役は app を import するだけなので、この見回りは本番（python app.py）のときだけ動く
    threading.Thread(target=_drop_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
