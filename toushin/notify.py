"""毎月の「更新です」を、Discord へ1通送る係。

  python notify.py          送る条件を見て、要るときだけ送る（予約タスク Toushin-Notify が毎朝9時に呼ぶ）
  python notify.py --dry    送らずに、送る中身と、いま送る条件にあるかだけを見せる
  python notify.py --test   条件を見ずに、確かめの1通を送る

新しい月の成績がまだのときは、先に 8050 の「自動で取る」（/api/jidou・jidou_torikomi.py --tsuchi）を始める。
取り終わったら jidou_torikomi.py が「入れかえました」か「途中で止まりました」を1通送る（みんかぶがまだ新しい月でなければ送らず、あしたの朝にまた見る）。
8050 に届かないときだけ、下の「更新の時期です」を送る。

送る条件：新しい月の成績がまだ入っていない（いちばん新しい成績の月 ＜ 前の月の末日）、かつ、今月まだ送っていない。
みんかぶは毎月1日の朝には前の月の末までの成績に切り替わっている（2026-10-01 に確かめた）ので、ふつうは1日の9時に1通。
PC が止まっていた日は、起きたあとの9時か、予約タスクの取り返しで1回。今月ぶんを取り込んだあとは送らない。

送り先は Discord だけ。宛先の探し方：
  TOUSHIN_DISCORD_WEBHOOK（環境変数か、toushin/.env・その上のフォルダの .env）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
PAGE_URL = os.environ.get("TOUSHIN_PAGE_URL") or "http://localhost:8050"
SENDER_NAME = "投資信託ランキング"           # Discord に出る差出人の名前
USER_AGENT = "ToushinBan/1.0"  # 名乗りが無いと Discord（Cloudflare）に 403 で弾かれる（2026-08-07 に実測）
STATE = store.DATA / "notified.json"
ENV_FILES = (HERE / ".env", HERE.parent / ".env")


def _from_env(*keys: str) -> str | None:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            for key in keys:
                if line.startswith(f"{key}="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    return None


def webhook_url() -> str | None:
    return _from_env("TOUSHIN_DISCORD_WEBHOOK")


def newest_period() -> str | None:
    with store.connect() as con:
        row = con.execute("SELECT MAX(date) FROM rank_rows").fetchone()
    return row[0] if row else None


def sent_month() -> str | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("month")
    except (OSError, ValueError, AttributeError):
        return None


def due(today: date) -> tuple[bool, str]:
    """いま送るか。（送る／送らない, 理由）"""
    need = store.prev_month_end(today).isoformat()
    have = newest_period()
    if have and have >= need:
        return False, f"{need[:7]} 末までの成績は、もう取り込んであります"
    if sent_month() == today.strftime("%Y-%m"):
        return False, "今月はもう送りました"
    return True, f"新しい成績（{need[:7]} 末まで）がまだ入っていません"


def mark_sent(today: date) -> None:
    store.DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"month": today.strftime("%Y-%m"), "at": today.isoformat()}), encoding="utf-8")


def start_auto() -> dict | None:
    """8050 の「自動で取る」を、終わったら知らせる形で始める。8050 に届かなければ None。"""
    port = os.environ.get("TOUSHIN_PORT") or "8050"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/jidou", data=json.dumps({"depth": 300, "tsuchi": True}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)
    except Exception:                       # noqa: BLE001 — 届かなければ、いままでの「更新の時期です」へ
        return None


def message(today: date) -> str:
    need = store.prev_month_end(today)
    return (f"投資信託ランキングの更新の時期です📈\n"
            f"みんかぶの成績が {need.year}/{need.month:02d} 末までに変わっています。\n"
            f"投資信託の画面で「取り込む」をお願いします → {PAGE_URL}")


def send(url: str, text: str) -> dict:
    payload = {"content": text[:2000], "username": SENDER_NAME, "allowed_mentions": {"parse": []}}
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return {"sent": True, "status": res.status}
    except urllib.error.HTTPError as e:
        return {"sent": False, "error": f"HTTP {e.code}"}
    except Exception as e:                  # noqa: BLE001 — 送れなくても落ちずに、ログに残す
        return {"sent": False, "error": str(e)}


def run(argv: list[str], today: date | None = None) -> dict:
    today = today or date.today()
    ok, why = due(today)
    text = message(today)
    if "--dry" in argv:
        return {"due": ok, "why": why, "text": text, "to": "あり" if webhook_url() else "なし"}
    if "--test" in argv:
        text = "（ためしの1通です）\n" + text
    elif not ok:
        return {"sent": False, "why": why}
    url = webhook_url()
    if not url:
        return {"sent": False, "error": "Discord の宛先がありません（TOUSHIN_DISCORD_WEBHOOK が無い）"}
    out = send(url, text)
    if out.get("sent") and "--test" not in argv:
        mark_sent(today)
    return dict(out, why=why)


if __name__ == "__main__":
    if "--dry" not in sys.argv and "--test" not in sys.argv:
        import backup                   # 毎朝の見回りのついでに、データが変わった日だけ Google ドライブへ控え
        try:
            print(date.today().isoformat(), "控え", json.dumps(backup.run(), ensure_ascii=False))
        except Exception as e:          # noqa: BLE001 — 控えでつまずいても、お知らせは止めない
            print(date.today().isoformat(), "控えでつまずきました", e)
    ok, why = due(date.today())
    auto = start_auto() if ok and "--dry" not in sys.argv and "--test" not in sys.argv else None
    if auto is not None:
        print(date.today().isoformat(), "自動で取るを始めました", why, json.dumps(auto, ensure_ascii=False))
    else:
        print(date.today().isoformat(), json.dumps(run(sys.argv[1:]), ensure_ascii=False))
