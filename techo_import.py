# -*- coding: utf-8 -*-
"""クロード極 — 手帳を、これまでの会話から書き直す（POST /techo/redigest を押す道具）

2026-09-03、手帳を元にしたアプリと同じ形（会話そのままではなく、話題ごとの粒）にしたときに、
それまでの会話（data/threads）を新しい書き方で入れ直すための道具。

  - **動いている 8030 に頼む。** 索引を触るのは1プロセスだけ、の決まり
  - 中で Sonnet 5 を呼ぶ（サブスク枠）。会話 12,000 字につき1回。
  - ★`--wipe` は袋を選ばない＝**全部の袋**を空にする。1つの袋だけなら
    `POST /techo/redigest?persona=<袋>&wipe=1` を直に押す（2026-09-12）
    ★同時に何本か走らせる（techo_writer.PARALLEL・2026-09-04 持ち主の指示）
  - 途中で止まっても、済んだスレッドは data/techo_state.json に残る＝続きから
  - 呼べない回が2回続いたら自分で止まる。残りは明け方4時の係（techo_writer.tick）が続ける

使い方：
    cd claude && python techo_import.py            まだ書いていない分だけ書く
    cd claude && python techo_import.py --wipe     手帳を空にしてから、全部書き直す（写しは先に取ること）
    cd claude && python techo_import.py --status   進み具合を見るだけ
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8030"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _call(method: str, path: str) -> dict:
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _show(s: dict) -> None:
    print(f"  {s.get('done', 0)}/{s.get('total', 0)} スレッド ・ 粒 {s.get('grains', 0)} 件"
          + (f"  {s['note']}" if s.get("note") else "")
          + ("  （書いている途中）" if s.get("running") else ""))


def main() -> None:
    try:
        if "--status" in sys.argv:
            _show(_call("GET", "/techo/redigest"))
            return
        wipe = "--wipe" in sys.argv
        d = _call("POST", f"/techo/redigest?wipe={'1' if wipe else '0'}")
        if not d.get("ok"):
            print(f"[書き直し] 始められませんでした: {d.get('reason', d)}")
            return
        print("[書き直し] 8030 の中で始まりました" + ("（手帳を空にしてから）" if wipe else ""))
        while True:
            time.sleep(15)
            s = _call("GET", "/techo/redigest")
            _show(s)
            if not s.get("running"):
                break
    except urllib.error.URLError as e:
        print(f"[書き直し] 8030 に届きません（クロード極が動いているか見てください）: {e}")


if __name__ == "__main__":
    main()
