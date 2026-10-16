"""持ち主が用意した利回りのファイル（JSON）を、貯める係へ入れる（2026-10-01）。

別のPCで取った利回り（3ヶ月・6ヶ月・12ヶ月）のデータを Google ドライブに置いておくと、自動で取ってきて入れる。
→ 8050（app.py）が5分ごとに scan() を呼び、置き場（<置き場> の 投資信託_*.json と、投資信託データ フォルダの *.json）の
  新しいファイル・書き換わったファイルだけを入れる。同じファイルは2回入れない（data/drop_state.json に、ファイルごとの書き換えの時刻を覚える）。
  読めないファイル（同期の途中など）は「読めませんでした」と覚え、書き換わったら読み直す。TOUSHIN_DROP_DIR で確かめ役が一時フォルダに向ける。

  python import_file.py <ファイル.json>         入れる
  python import_file.py <ファイル.json> --dry   入れずに、何本入るかだけ見る

形：{"period": "2026-09-30", "funds": [{"code", "name", "co", "ret_3m", "ret_6m", "ret_1y", "ret_3y_nen", "ret_5y_nen", "nisa"}, …]}
入れるのは、成績の月（period）の r3・r6・r12・r36・r60（リターン %）と、NISA の印（「成長」「つみたて」。足すだけで消さない）。
順位（rank_3m・rank_6m・rank_1y・rank_3y）は、1位から切れ目なく20位以上そろっている期間だけ、その深さまでランキングとして入れる
（300位まで取る道具なら300位まで。100本だけのような穴あきは入れない）。4つの期間をそろえるには、どれかの上位に入るファンドを全部ファイルに入れる。評価は入れない。数字の形が違う欄は飛ばす。
period が月末でないとき（取った日が入っているファイル）は、その前の月の末を成績の月にする。
取り込みの記録（imports）には kind='file' で残す（「取る」の済み／未や、比較のページの済み／未には数えない）。
置かれたファイルを読むだけ。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import store

ID_RE = re.compile(r"[0-9A-Za-z]{8}")
COLS = {"ret_3m": "r3", "ret_6m": "r6", "ret_1y": "r12", "ret_3y_nen": "r36", "ret_5y_nen": "r60"}
NISA = {"成長": "NISA成長投資枠", "つみたて": "つみたて投資枠"}
RANKS = {"rank_3m": 3, "rank_6m": 6, "rank_1y": 12, "rank_3y": 36}      # ファイルの順位の欄 → 期間（月数）
DROP = Path(os.environ.get("TOUSHIN_DROP_DIR") or r"<置き場>")
SUB = "投資信託データ"
STATE = store.DATA / "drop_state.json"


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if -100 <= v <= 100000 else None


def run(path: Path, dry: bool = False) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    period = data.get("period")
    try:
        day = date.fromisoformat(period)
    except (TypeError, ValueError):
        return {"ok": False, "why": f"成績の月（period）が読めません：{period!r}"}
    if day > date.today():
        return {"ok": False, "why": f"period がこの先の日です：{period}"}
    if day != store.month_end(day.year, day.month):
        # 取った日が入るファイルは「取った日」（例 2026-10-01）が入る。みんかぶは毎月1日には前の月の末までの成績に変わっているので、
        # 成績の月は「取った日の前の月の末」（10/1 に取った → 9/30）
        period = store.prev_month_end(day).isoformat()
    rows, skipped, valid = [], 0, []
    for f in data.get("funds") or []:
        code = f.get("code") if isinstance(f, dict) else None
        if not (isinstance(code, str) and ID_RE.fullmatch(code)):
            skipped += 1
            continue
        valid.append((code, f))
        cols = {c: _num(f.get(k)) for k, c in COLS.items()}
        nisa = [NISA[k] for k in NISA if isinstance(f.get("nisa"), str) and k in f["nisa"]]
        if not any(v is not None for v in cols.values()) and not nisa:
            skipped += 1
            continue
        rows.append((code, str(f.get("name") or "")[:120], str(f.get("co") or "")[:40], cols, nisa))
    # 順位：その期間の順位が1位から切れ目なく20位以上そろっているときだけ、その深さまでランキングとして入れる
    # （100本だけのような穴あきを入れると、ランキングに穴があく。同じ順位が2本あるときも入れない）
    ranks = {}
    for key, term in RANKS.items():
        got = [(f.get(key), code) for code, f in valid
               if isinstance(f.get(key), int) and not isinstance(f.get(key), bool) and 1 <= f.get(key) <= 5000]
        by = dict(got)
        if len(by) != len(got):
            continue
        up = 0
        while up + 1 in by:
            up += 1
        if up >= 20:
            ranks[term] = [(r, by[r]) for r in range(1, up + 1)]
    names = {code: (str(f.get("name") or "")[:120], str(f.get("co") or "")[:40]) for code, f in valid}
    out = {"ok": True, "period": period, "funds": len(rows), "skipped": skipped,
           "cells": {c: sum(1 for r in rows if r[3][c] is not None) for c in COLS.values()},
           "nisa": sum(1 for r in rows if r[4]), "ranks": {str(t): len(p) for t, p in ranks.items()}}
    if dry or not (rows or ranks):
        return out
    with store.connect() as con:
        for code, name, co, cols, nisa in rows:
            store._touch_fund(con, code, name, co, period)
            store._merge_daily(con, period, code, cols)
            if nisa:
                store._add_nisa(con, code, nisa)
        for term, pairs in ranks.items():
            # 同じ月・同じ順位は置きかえ、同じファンドが別の順位で入っていたら外す（ブックマークの取り込み＝store.import_ranking と同じ）
            for rank, code in pairs:
                store._touch_fund(con, code, *names[code], period)
                con.execute("DELETE FROM rank_rows WHERE date=? AND term=? AND fund_id=? AND rank<>?", (period, term, code, rank))
                con.execute("INSERT OR REPLACE INTO rank_rows(date, term, rank, fund_id) VALUES(?,?,?,?)", (period, term, rank, code))
        con.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), period, "file", None, len(rows), None, None, Path(path).name[:60]))
    return out


def _files() -> list[Path]:
    if not DROP.exists():
        return []
    got = set(DROP.glob("投資信託_*.json"))
    if (DROP / SUB).is_dir():
        got |= set((DROP / SUB).glob("*.json"))
    return sorted(got)


def _state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(st, dict) and isinstance(st.get("files"), dict):
            return st
    except (OSError, ValueError):
        pass
    return {"files": {}, "last": None}


def scan() -> list[dict]:
    """置き場の、新しいファイル・書き換わったファイルだけを入れる。入れた（読もうとした）ものを返す。"""
    st = _state()
    done = []
    for f in _files():
        key = f.name if f.parent == DROP else f"{SUB}/{f.name}"
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if st["files"].get(key) == mtime:
            continue
        try:
            r = run(f)
        except Exception as e:          # noqa: BLE001 — 同期の途中などで読めなくても、止まらずに覚えておく
            r = {"ok": False, "why": f"読めませんでした（{type(e).__name__}）"}
        st["files"][key] = mtime
        st["last"] = dict(r, file=key, at=datetime.now().isoformat(timespec="seconds"))
        done.append(st["last"])
    if done:
        store.DATA.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE)
    return done


def status() -> dict:
    """画面に出す様子：置き場が見えるか・最後に読んだファイル。"""
    return {"dir": str(DROP), "seen": DROP.exists(), "last": _state().get("last")}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    print(json.dumps(run(Path(args[0]), dry="--dry" in sys.argv), ensure_ascii=False))
