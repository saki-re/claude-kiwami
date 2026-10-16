"""貯める係（SQLite）。「取り込む」で1ページずつ入ってくる分を、足していく。

みんかぶの順位とリターンは月末ごとの数字（ランキングに「2026/03 ~ 2026/08」と成績の期間が出る）。
基準価額は日ごと。そこで日付を2つに分けて貯める：
  成績の月（period）… 成績の期間の終わりの月の末日（例 2026-08-31）。順位・リターンはここへ。同じ月に何度押しても1組
  基準価額の日（nav day）… ランキングはページの「更新：2026/10/01 07:09」の日（18時より前なら前の営業日・app._nav_day）、
                            個別ページは「基準価額 09/30」の日（nav_day_from_md）。基準価額・信託報酬・レーティングはここへ

  funds       ファンドの名前と運用会社
  rank_rows   成績の月・期間ごとのランキングの並び（みんかぶの表の順位そのまま）
  term_meta   成績の月・期間ごとの「成績の期間」の表示（2026/03~2026/08）
  fund_daily  日付ごとのファンドの数字（成績の月の行と、基準価額の日の行）
  imports     取り込んだ記録
  fund_info   ファンドのなりたち（比較のページの「手数料」「分配金」「決算回数」「設定日」「信託期間」「RR分類」）。
              月ごとに変わる数字ではないので、ファンドごとに1行（いちばん新しく写したもの）

買った値段（holdings.json）は、持ち主が画面で書くもの。{番号: {nav, day, yen}}
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from minkabu import TERMS, is_leveraged

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TOUSHIN_DATA") or HERE / "data")
DB = DATA / "toushin.db"
STARS = DATA / "stars.json"

RETURN_TERMS = (3, 6, 12, 36, 60)   # fund_daily に列がある期間
LOOK_BACK_DAYS = 130                # いちばん新しい日からこの日数までさかのぼって、入っている値を拾う（月1回の取り込みでも残る）

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds(
  id TEXT PRIMARY KEY, name TEXT, company TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS rank_rows(
  date TEXT, term INTEGER, rank INTEGER, fund_id TEXT,
  PRIMARY KEY(date, term, rank));
CREATE TABLE IF NOT EXISTS term_meta(
  date TEXT, term INTEGER, span TEXT, PRIMARY KEY(date, term));
CREATE TABLE IF NOT EXISTS fund_daily(
  date TEXT, fund_id TEXT, nav REAL, nav_diff REAL, fee REAL, rating INTEGER,
  r3 REAL, r6 REAL, r12 REAL, r36 REAL, r60 REAL,
  k3 INTEGER, k6 INTEGER, k12 INTEGER, k36 INTEGER, k60 INTEGER,
  PRIMARY KEY(date, fund_id));
CREATE INDEX IF NOT EXISTS fund_daily_by_fund ON fund_daily(fund_id, date);
CREATE TABLE IF NOT EXISTS imports(
  at TEXT, date TEXT, kind TEXT, term INTEGER, n INTEGER, first INTEGER, last INTEGER, fund_id TEXT);
CREATE TABLE IF NOT EXISTS fund_info(
  fund_id TEXT PRIMARY KEY, buy_fee REAL, dist_last REAL, dist_year REAL, settle TEXT, setup TEXT, until TEXT,
  rr TEXT, at TEXT);
"""

RISK_TERMS = (6, 12, 36, 60)        # 値動きの荒さ（s）・荒さの割の増え方（p）が個別ページの表に並ぶ期間
# あとから足した欄（前からある DB にも、無ければ足す）。s＝標準偏差・sk＝その順位・p＝シャープレシオ・pk＝その順位
ADDED_COLUMNS = {"fund_daily": [f"{c}{t} {'REAL' if c in ('s', 'p') else 'INTEGER'}"
                                for c in ("s", "sk", "p", "pk") for t in RISK_TERMS],
                 "funds": ["nisa TEXT"]}


def _add_columns(con) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col.split()[0] not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        _add_columns(con)
        yield con
        con.commit()
    finally:
        con.close()


def month_end(year: int, month: int) -> date:
    return (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)


def prev_month_end(day: date) -> date:
    return date(day.year, day.month, 1) - timedelta(days=1)


def _touch_fund(con, fid: str, name: str, company: str, day: str) -> None:
    con.execute(
        "INSERT INTO funds(id, name, company, first_seen, last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE name END, "
        "company=CASE WHEN excluded.company<>'' THEN excluded.company ELSE company END, "
        "first_seen=MIN(first_seen, excluded.first_seen), last_seen=MAX(last_seen, excluded.last_seen)",
        (fid, name, company, day, day))


def _merge_daily(con, day: str, fid: str, cols: dict) -> None:
    """渡された欄だけ直す（None の欄は、前に入っていた値のまま）。"""
    cols = {k: v for k, v in cols.items() if v is not None}
    if not cols:
        return
    con.execute("INSERT OR IGNORE INTO fund_daily(date, fund_id) VALUES(?,?)", (day, fid))
    con.execute("UPDATE fund_daily SET %s WHERE date=? AND fund_id=?" % ", ".join(f"{k}=?" for k in cols),
                (*cols.values(), day, fid))


def import_ranking(period: str, nav_day: str, term: int, rows: list[dict], span: str | None = None) -> dict:
    """ランキング1ページぶんを足す。rows＝[{rank, id, name, company, nav, nav_diff, ret12, fee, rating}]

    period＝成績の月（末日）、nav_day＝基準価額の日。同じ月・同じ順位は置きかえる。
    その期間に、もっと新しい月の並びがもう入っていたら stale にその月を入れて返す（開いたままの古いタブで押したとき。
    古い月の分も記録として残すが、画面は新しい月のまま）。
    """
    with connect() as con:
        row = con.execute("SELECT MAX(date) FROM rank_rows WHERE term=?", (term,)).fetchone()
        stale = row[0] if row and row[0] and row[0] > period else None
        for r in rows:
            con.execute("DELETE FROM rank_rows WHERE date=? AND term=? AND fund_id=? AND rank<>?",
                        (period, term, r["id"], r["rank"]))
            con.execute("INSERT OR REPLACE INTO rank_rows(date, term, rank, fund_id) VALUES(?,?,?,?)",
                        (period, term, r["rank"], r["id"]))
            _touch_fund(con, r["id"], r.get("name") or "", r.get("company") or "", nav_day)
            _merge_daily(con, period, r["id"], {"r12": r.get("ret12")})
            _merge_daily(con, nav_day, r["id"], {"nav": r.get("nav"), "nav_diff": r.get("nav_diff"),
                                                 "fee": r.get("fee"), "rating": r.get("rating")})
        if span:
            con.execute("INSERT OR REPLACE INTO term_meta(date, term, span) VALUES(?,?,?)", (period, term, span))
        ranks = [r["rank"] for r in rows]
        first, last = (min(ranks), max(ranks)) if ranks else (None, None)
        con.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), period, "rank", term, len(rows), first, last, None))
    return {"kind": "rank", "date": period, "term": term, "n": len(rows), "first": first, "last": last, "span": span,
            "stale": stale}


def nav_day_from_md(nav_md: str | None, today: date | None = None) -> str:
    """個別ページの「09/30」に年を付ける。年またぎ（1月に 12/30 と出る）は前の年。読めなければ今日。"""
    today = today or date.today()
    if nav_md:
        try:
            m, d = map(int, nav_md.split("/"))
            day = date(today.year, m, d)
            if day > today + timedelta(days=1):
                day = date(today.year - 1, m, d)
            return day.isoformat()
        except ValueError:
            pass
    return today.isoformat()


def fund_period(nav_day: str) -> str:
    """個別ページの成績を入れる月。ランキングのいちばん新しい成績の月（個別ページの数字は、ランキングと同じ月のもの）。
    ランキングをまだ取り込んでいないか、古すぎるなら、基準価額の日の前の月の末日。"""
    day = date.fromisoformat(nav_day)
    with connect() as con:
        row = con.execute("SELECT MAX(date) FROM rank_rows WHERE date<=?", (nav_day,)).fetchone()
    if row and row[0] and (day - date.fromisoformat(row[0])).days <= 70:
        return row[0]
    return prev_month_end(day).isoformat()


def import_fund(period: str, nav_day: str, fid: str, name: str, nav: float | None, nav_diff: float | None,
                returns: dict[int, float], ranks: dict[int, int], risk: dict | None = None,
                nisa: list[str] | None = None) -> dict:
    """個別ファンドのリターンのページ1枚ぶんを足す。成績は period へ、基準価額は nav_day へ。

    risk＝{"s": {月数: 値動きの荒さ}, "sk": {…順位}, "p": {月数: 荒さの割の増え方}, "pk": {…順位}}
    nisa＝名前の上の NISA の印の文字（[] は印なし・None は分からない＝前の値のまま）
    """
    cols: dict = {}
    for t in RETURN_TERMS:
        cols[f"r{t}"] = returns.get(t)
        cols[f"k{t}"] = ranks.get(t)
    for key, by_term in (risk or {}).items():
        for t in RISK_TERMS:
            cols[f"{key}{t}"] = by_term.get(t)
    with connect() as con:
        _touch_fund(con, fid, name or "", "", nav_day)
        if nisa is not None:
            _add_nisa(con, fid, nisa)
        _merge_daily(con, period, fid, cols)
        _merge_daily(con, nav_day, fid, {"nav": nav, "nav_diff": nav_diff})
        con.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), period, "fund", None, 1, None, None, fid))
        shown = con.execute("SELECT name FROM funds WHERE id=?", (fid,)).fetchone()
    return {"kind": "fund", "date": period, "id": fid, "name": shown["name"] if shown else name,
            "terms": sorted(returns)}


NISA_LABELS = {"yield_nisa_growth": "NISA成長投資枠", "yield_nisa": "つみたて投資枠"}


def _add_nisa(con, fid: str, labels: list[str]) -> None:
    """NISA の印を足す（前の印は残す。印が無いことは「買えない」とは限らないので、消さない）。
    labels が [] のとき（ページに印が無かった）は、まだ何も無ければ [] を置く＝「ページを見たが印は無かった」。"""
    row = con.execute("SELECT nisa FROM funds WHERE id=?", (fid,)).fetchone()
    have = json.loads(row["nisa"]) if row and row["nisa"] else None
    merged = list(dict.fromkeys((have or []) + labels)) if (have or labels) else []
    con.execute("UPDATE funds SET nisa=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), fid))


def mark_nisa(which: str, rows: list[tuple[str, str]], page: int) -> dict:
    """NISA のランキングのページ1枚ぶん：並んでいるファンドに NISA の印（成長投資枠／つみたて投資枠）を付ける。"""
    label = NISA_LABELS[which]
    today = date.today().isoformat()
    with connect() as con:
        for fid, name in rows:
            _touch_fund(con, fid, name or "", "", today)
            _add_nisa(con, fid, [label])
        con.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), today, "nisa", None, len(rows), page, None, which))
    return {"kind": "nisa", "which": which, "label": label, "n": len(rows), "page": page}


INFO_COLS = ("buy_fee", "dist_last", "dist_year", "settle", "setup", "until", "rr")


def _merge_info(con, fid: str, info: dict) -> None:
    """なりたちの行を足す（None の欄は前の値のまま。「前回の分配金 -」のように、ページに値が無い欄も前のまま）。"""
    cols = {k: info.get(k) for k in INFO_COLS if info.get(k) is not None}
    if not cols:
        return
    cols["at"] = date.today().isoformat()
    con.execute("INSERT OR IGNORE INTO fund_info(fund_id) VALUES(?)", (fid,))
    con.execute("UPDATE fund_info SET %s WHERE fund_id=?" % ", ".join(f"{k}=?" for k in cols), (*cols.values(), fid))


def import_compare(period: str, rows: list[dict]) -> dict:
    """比較のページ1枚ぶんを足す。rows＝[{id, name, fee, returns: {12,36,60}, sharpe: {12,36,60}, info: {…}}]
    リターンと効率（シャープレシオ）は成績の月へ。基準価額はいつの値段か分からないので入れない。
    info（手数料・分配金・決算回数・設定日・信託期間・RR分類）は fund_info へ（ブックマークの版6から）。"""
    with connect() as con:
        for r in rows:
            _merge_info(con, r["id"], r.get("info") or {})
            _touch_fund(con, r["id"], r.get("name") or "", "", period)
            cols = {"fee": r.get("fee")}
            for t in (12, 36, 60):
                cols[f"r{t}"] = r["returns"].get(t)
                cols[f"p{t}"] = r["sharpe"].get(t)
            _merge_daily(con, period, r["id"], cols)
            con.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), period, "compare", None, 1, None, None, r["id"]))
    return {"kind": "compare", "date": period, "n": len(rows), "ids": [r["id"] for r in rows]}


def nav_days() -> list[str]:
    """基準価額が入っている日。"""
    with connect() as con:
        return [r[0] for r in con.execute("SELECT DISTINCT date FROM fund_daily WHERE nav IS NOT NULL ORDER BY date")]


def month_ago_date(all_dates: list[str], latest_day: str, months: int = 1, tol: int = 4) -> str | None:
    """latest_day の months か月前にいちばん近い、貯めてある日（前後 tol 日まで）。無ければ None。"""
    y, m, d = map(int, latest_day.split("-"))
    py, pm = divmod(y * 12 + (m - 1) - months, 12)
    pm += 1
    target = None
    for dd in range(d, 0, -1):          # 31日が無い月は、その月の末日へ
        try:
            target = date(py, pm, dd)
            break
        except ValueError:
            continue
    near = [x for x in all_dates if abs((date.fromisoformat(x) - target).days) <= tol]
    return min(near, key=lambda x: abs((date.fromisoformat(x) - target).days)) if near else None


def _coverage(ranks: list[int]) -> dict:
    """1位から切れ目なく何位まで入っているか（upto）と、入っている件数（count）。"""
    have = set(ranks)
    upto = 0
    while upto + 1 in have:
        upto += 1
    return {"upto": upto, "count": len(have)}


def _mi(day: str) -> int:
    """月の通し番号（2026-09-30 → 2026*12+8）。となりの月かどうかを見るため。"""
    return int(day[:4]) * 12 + int(day[5:7]) - 1


def _by_month(con) -> dict[int, dict]:
    """期間ごとの、成績の月の並び。{期間: {"dates": [古い順], "ranks": {月: {番号: 順位}}}}"""
    out: dict[int, dict] = {}
    for r in con.execute("SELECT date, term, rank, fund_id FROM rank_rows"):
        t = out.setdefault(r["term"], {"dates": set(), "ranks": {}})
        t["dates"].add(r["date"])
        t["ranks"].setdefault(r["date"], {})[r["fund_id"]] = r["rank"]
    for t in out.values():
        t["dates"] = sorted(t["dates"])
    return out


def _streaks(bm: dict) -> tuple[dict, dict]:
    """stay＝いちばん新しい月まで、続けて何か月そのランキングに居るか／down＝何か月続けて順位が下がっているか。
    どちらも2以上だけ返す（1か月の上下は「先月から」の↑↓で足りる）。取り込まなかった月があると、そこで数えるのをやめる。"""
    stay: dict[str, dict] = {}
    down: dict[str, dict] = {}
    for t, info in bm.items():
        ds, R = info["dates"], info["ranks"]
        if len(ds) < 2:
            continue
        for fid in R[ds[-1]]:
            n, i = 1, len(ds) - 1
            while i > 0 and _mi(ds[i]) - _mi(ds[i - 1]) == 1 and fid in R[ds[i - 1]]:
                n, i = n + 1, i - 1
            if n >= 2:
                stay.setdefault(fid, {})[str(t)] = n
            n, i = 0, len(ds) - 1
            while (i > 0 and _mi(ds[i]) - _mi(ds[i - 1]) == 1 and fid in R[ds[i - 1]]
                   and R[ds[i]][fid] > R[ds[i - 1]][fid]):
                n, i = n + 1, i - 1
            if n >= 2:
                down.setdefault(fid, {})[str(t)] = n
    return stay, down


def _month_moves(bm: dict, names: dict) -> dict:
    """今月の動き：期間ごとに、ひとつ前の月と比べて「新しく入った」「外れた」ファンド。
    比べるのは、両方の月で1位から切れ目なく取り込んである深さ（depth）まで（取り込んでいない所を「外れた」と言わないため）。"""
    out = {}
    for t, info in bm.items():
        ds, R = info["dates"], info["ranks"]
        if len(ds) < 2:
            continue
        L, P = ds[-1], ds[-2]
        upL, upP = _coverage(list(R[L].values()))["upto"], _coverage(list(R[P].values()))["upto"]
        depth = min(upL, upP)
        if not depth:
            continue
        inn = sorted((rk, fid) for fid, rk in R[L].items() if rk <= depth and not R[P].get(fid, 10 ** 9) <= upP)
        gone = sorted((rk, fid) for fid, rk in R[P].items() if rk <= depth and not R[L].get(fid, 10 ** 9) <= upL)
        out[str(t)] = {"date": L, "prev": P, "depth": depth, "in": [[fid, rk] for rk, fid in inn],
                       "out": [[fid, rk, names[fid]["name"] if fid in names else "", R[L].get(fid)] for rk, fid in gone]}
    return out


def _month_navs(con, ids: list[str], newest: str) -> dict[str, list]:
    """♡と買った値段のあるファンドの、月ごとの基準価額（その月の、いちばん新しい値段の日）。13か月まで。"""
    if not ids:
        return {}
    since = (date.fromisoformat(newest) - timedelta(days=400)).isoformat()
    got: dict[str, dict] = {}
    q = "SELECT fund_id, date, nav FROM fund_daily WHERE nav IS NOT NULL AND date>=? AND fund_id IN (%s) ORDER BY date"
    for r in con.execute(q % ",".join("?" * len(ids)), (since, *ids)):
        got.setdefault(r["fund_id"], {})[r["date"][:7]] = [r["date"], r["nav"]]
    return {fid: [m[k] for k in sorted(m)][-13:] for fid, m in got.items()}


def latest() -> dict:
    """画面に渡す1組。

    ランキングは期間ごとに、その期間を最後に取り込んだ成績の月の並び（今月は1年だけ取り込んでも、6ヶ月は前の分が見える）。
    ファンドの数字は、欄ごとに、いちばん新しく入っている値。並ぶのは、どれかのランキングに居るファンドと、♡・「取る」・買った値段のあるファンド。
    """
    starred = stars()
    held = holdings()
    coverage = {str(t): {"upto": 0, "count": 0, "date": None, "span": None, "prev": None, "months": 0} for t in TERMS}
    navs = nav_days()
    with connect() as con:
        row = con.execute("SELECT MAX(date) FROM fund_daily").fetchone()
        newest = row[0] if row else None
        if not newest:
            return {"period": None, "nav_date": None, "months": 0, "nav_days": 0, "funds": [], "m1_from": None,
                    "m3_from": None, "m6_from": None, "coverage": coverage, "stars": starred, "page_dates": {},
                    "moves": {}}
        bm = _by_month(con)
        tops: dict[str, dict[int, int]] = {}
        moves: dict[str, dict[int, int]] = {}
        for t in TERMS:
            info = bm.get(t)
            if not info:
                continue
            last = info["dates"][-1]
            ranks = []
            for fid, rk in info["ranks"][last].items():
                tops.setdefault(fid, {})[t] = rk
                ranks.append(rk)
            meta = con.execute("SELECT span FROM term_meta WHERE term=? AND date=?", (t, last)).fetchone()
            # 先月からの順位の上がり下がり：その期間の、ひとつ前の成績の月の並びと比べる（両方に居るファンドだけ。プラス＝上がった）
            prev = info["dates"][-2] if len(info["dates"]) > 1 else None
            if prev:
                for fid, rk in info["ranks"][prev].items():
                    now = tops.get(fid, {}).get(t)
                    if now is not None:
                        moves.setdefault(fid, {})[t] = rk - now
            coverage[str(t)] = dict(_coverage(ranks), date=last, span=meta["span"] if meta else None, prev=prev,
                                    months=len(info["dates"]))
        stay, down = _streaks(bm)
        since = (date.fromisoformat(newest) - timedelta(days=LOOK_BACK_DAYS)).isoformat()
        cols = ("nav", "nav_diff", "fee", "rating", "r3", "r6", "r12", "r36", "r60", "k3", "k6", "k12", "k36",
                "s12", "s36", "s60", "p12", "p36", "p60", "pk12", "pk36", "pk60")
        vals: dict[str, dict] = {}
        for r in con.execute("SELECT * FROM fund_daily WHERE date>=? ORDER BY date DESC", (since,)):
            v = vals.setdefault(r["fund_id"], {})
            for c in cols:
                if v.get(c) is None and r[c] is not None:
                    v[c] = r[c]
                    if c == "nav":
                        v["nav_date"] = r["date"]
                    elif c in ("r3", "r6", "r12", "r36", "r60") and "r_date" not in v:
                        v["r_date"] = r["date"]
                    if c in ("r12", "r36", "r60", "p12", "p36", "p60"):
                        v[c + "@"] = r["date"]          # その値の成績の月（荒さの目安で、月がそろっているかを見る）
        nav_day = navs[-1] if navs else None
        # 貯めた基準価額から出す値上がり：1ヶ月（前後4日）・3ヶ月・6ヶ月（前後7日）前の値段の日と比べる
        ago = {k: (month_ago_date(navs, nav_day, k, 4 if k == 1 else 7) if nav_day else None) for k in (1, 3, 6)}
        old_nav = {k: ({r["fund_id"]: r["nav"] for r in con.execute(
            "SELECT fund_id, nav FROM fund_daily WHERE date=? AND nav IS NOT NULL", (d,))} if d else {})
            for k, d in ago.items()}
        names = {r["id"]: r for r in con.execute("SELECT id, name, company, nisa FROM funds")}
        info_rows = {r["fund_id"]: {k: r[k] for k in INFO_COLS} for r in con.execute("SELECT * FROM fund_info")}
        # NISA のランキングを何ページ目まで取り込んだか（imports の fund_id 欄にランキングの名前・first 欄にページを置いている）
        nisa_pages = {r[0]: r[1] for r in con.execute(
            "SELECT fund_id, MAX(first) FROM imports WHERE kind='nisa' GROUP BY fund_id")}
        page_dates = {r[0]: r[1] for r in con.execute(
            "SELECT fund_id, MAX(date) FROM imports WHERE kind='fund' GROUP BY fund_id")}
        compare_dates = {r[0]: r[1] for r in con.execute(
            "SELECT fund_id, MAX(date) FROM imports WHERE kind IN ('fund','compare') GROUP BY fund_id")}
        months = con.execute("SELECT COUNT(DISTINCT date) FROM rank_rows").fetchone()[0]
        month_moves = _month_moves(bm, names)
        navm = _month_navs(con, list(dict.fromkeys(starred + list(held))), newest)
    funds = []
    picked = picks()
    extra = [s for s in starred + picked + list(held) if s not in tops and s in names]
    for fid in list(tops) + list(dict.fromkeys(extra)):
        v, nm = vals.get(fid, {}), names.get(fid)

        def grow(k):
            old = old_nav[k].get(fid)
            if v.get("nav") and old and v.get("nav_date") == nav_day:
                return round((v["nav"] / old - 1) * 100, 2)
            return None
        funds.append({
            "id": fid, "name": nm["name"] if nm else "", "co": nm["company"] if nm else "",
            "nav": v.get("nav"), "nd": v.get("nav_diff"), "fee": v.get("fee"), "rate": v.get("rating"),
            "nav_date": v.get("nav_date"), "r_date": v.get("r_date"),
            "lev": is_leveraged(nm["name"] if nm else ""),
            "r": {"1": grow(1), "3": v.get("r3"), "6": v.get("r6"), "12": v.get("r12"), "36": v.get("r36"),
                  "60": v.get("r60")},
            # 貯めた基準価額から出した3ヶ月・6ヶ月の値上がり（みんかぶの r3・r6 が無いファンドの代わり。分配金は入らない）
            "g": {"3": grow(3), "6": grow(6)},
            "k": {"3": v.get("k3"), "6": v.get("k6"), "12": v.get("k12"), "36": v.get("k36")},
            "t": {str(t): rk for t, rk in tops.get(fid, {}).items()},
            "mv": {str(t): d for t, d in moves.get(fid, {}).items()},
            "stay": stay.get(fid, {}), "down": down.get(fid, {}),
            # 値動きの荒さ（年率の標準偏差 %）と、荒さの割の増え方（シャープレシオ）とその全ファンド中の順位
            "risk": {"s": {"12": v.get("s12"), "36": v.get("s36"), "60": v.get("s60")},
                     "p": {"12": v.get("p12"), "36": v.get("p36"), "60": v.get("p60")},
                     "pk": {"12": v.get("pk12"), "36": v.get("pk36"), "60": v.get("pk60")}},
            "nisa": json.loads(nm["nisa"]) if nm and nm["nisa"] else None,
            # 増え方（r）と効率（p）が別の成績の月の期間（月が替わってランキングだけ取り込んだあいだ）。画面と Claude の道具は、ここでは荒さの目安を出さない
            "pmis": [t for t in ("12", "36", "60") if v.get(f"r{t}") is not None and v.get(f"p{t}") is not None
                     and v.get(f"r{t}@") != v.get(f"p{t}@")],
            "info": info_rows.get(fid),
            "hold": held.get(fid),
            "navm": navm.get(fid),
        })
    periods = [c["date"] for c in coverage.values() if c["date"]]
    return {"period": max(periods) if periods else None, "nav_date": nav_day, "months": months, "nav_days": len(navs),
            "funds": funds, "m1_from": ago[1], "m3_from": ago[3], "m6_from": ago[6], "coverage": coverage,
            "stars": starred, "page_dates": page_dates, "picks": picked, "taken": compare_dates,
            "nisa_pages": nisa_pages, "moves": month_moves}


def history(fund_id: str) -> list[dict]:
    """そのファンドの、日ごとの数字と、みんかぶの表での順位（成績の月の行と、基準価額の日の行が混ざる）。"""
    with connect() as con:
        days = {r["date"]: dict(r) for r in con.execute(
            "SELECT date, nav, r3, r6, r12, r36, k3, k6, k12, k36 FROM fund_daily WHERE fund_id=? ORDER BY date",
            (fund_id,))}
        for r in con.execute("SELECT date, term, rank FROM rank_rows WHERE fund_id=?", (fund_id,)):
            days.setdefault(r["date"], {"date": r["date"]})[f"t{r['term']}"] = r["rank"]
    return [days[d] for d in sorted(days)]


PICKS = DATA / "picks.json"
HOLDINGS = DATA / "holdings.json"


def fund_known(fund_id: str) -> bool:
    """そのファンドが、貯めた中にあるか（買った値段を、知らない番号に付けないため）。"""
    with connect() as con:
        return con.execute("SELECT 1 FROM funds WHERE id=?", (fund_id,)).fetchone() is not None


def holdings() -> dict[str, dict]:
    """買った値段（持ち主が画面で書く）。{番号: {"nav": 1口の基準価額, "day": 買った日 or None, "yen": 買った金額 or None}}"""
    try:
        got = json.loads(HOLDINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(got, dict):
        return {}
    return {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, dict) and v.get("nav")}


def set_holding(fund_id: str, hold: dict | None) -> dict[str, dict]:
    """hold が None なら消す。"""
    cur = holdings()
    if hold is None:
        cur.pop(fund_id, None)
    else:
        cur[fund_id] = hold
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = HOLDINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    tmp.replace(HOLDINGS)
    return cur


def picks() -> list[str]:
    """「取る」の印（データを取りに行きたいファンド）。♡（お気に入り）とは別。並びは付けた順。"""
    try:
        got = json.loads(PICKS.read_text(encoding="utf-8"))
        return [x for x in got if isinstance(x, str)] if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def set_picks(ids: list[str]) -> list[str]:
    ids = list(dict.fromkeys(ids))[:1000]
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = PICKS.with_suffix(".tmp")
    tmp.write_text(json.dumps(ids), encoding="utf-8")
    tmp.replace(PICKS)
    return ids


def stars() -> list[str]:
    try:
        got = json.loads(STARS.read_text(encoding="utf-8"))
        return [x for x in got if isinstance(x, str)] if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def set_star(fund_id: str, on: bool) -> list[str]:
    cur = [x for x in stars() if x != fund_id]
    if on:
        cur.append(fund_id)
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STARS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur), encoding="utf-8")
    tmp.replace(STARS)
    return cur
