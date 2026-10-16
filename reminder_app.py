# -*- coding: utf-8 -*-
"""リマインダー（クロード極が持つ一覧・2026-09-19 新設）

経緯：公式 Claude アプリでは iPhone のリマインダーに登録できるので、同じことを極でもできるようにしたい、という要望から作った。

★なぜ自前か：iPhone のリマインダーには、持ち主が iPhone で1回許可しないと外から書けない
  （ショートカットを作る形は採らなかった・icloud.com を動かす形は見張りに止められた）。
★だれが書くか：
  クロード極の全プロジェクト … 道具 remind_touroku（remind.py）が、ここを直接呼ぶ
  別のアプリ                  … 道具 remind_touroku が、この一覧の口 POST /reminders/api を呼ぶ
  持ち主                      … 画面 /reminders（reminders.html）から足す・済ませる・消す
★どう知らせるか：iPhone のリマインダーに入れて、iPhone に知らせてもらう（一覧の各行と会話の札の
  「iPhoneのリマインダーに入れる」ボタン → 公式 Claude アプリ）。ディスコードで知らせる fire_due() は残してあるが、
  ディスコードでの知らせは不要になったので見回りから外した（2026-09-19）。
★置き場は data/reminders.db（git に載らない）。生成AIは1回も呼ばない＝0円。
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta

import config

_lock = threading.Lock()
_WEEK = "月火水木金土日"
_FORMS = ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M:%S",
          "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
_DAY_FORMS = ("%Y/%m/%d", "%Y-%m-%d")
_MORNING = (9, 0)                        # 日付だけのときに知らせる時刻
TITLE_MAX = 200


def _db():
    # ★毎回 config.DATA_DIR から引く（確かめ役が一時フォルダへ向け替えたとき、そちらを使うため）
    p = config.DATA_DIR / "reminders.db"
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        due TEXT NOT NULL DEFAULT '',        -- 'YYYY-MM-DD HH:MM'（空＝日時なし）
        notes TEXT NOT NULL DEFAULT '',
        who TEXT NOT NULL DEFAULT '',        -- 登録した人（AI・持ち主 など）
        done INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        done_at TEXT NOT NULL DEFAULT '',
        notified_at TEXT NOT NULL DEFAULT ''
    )""")
    return con


def parse_due(raw: str, now: datetime | None = None) -> tuple[str, bool] | str:
    """日時の文字を ('YYYY-MM-DD HH:MM' か '', 朝9時にしたか) にする。読めなければ断り文。"""
    now = now or datetime.now()
    raw = (raw or "").strip()
    if not raw:
        return "", False
    due, morning = None, False
    for f in _FORMS:
        try:
            due = datetime.strptime(raw, f)
            break
        except ValueError:
            pass
    if due is None:
        for f in _DAY_FORMS:
            try:
                due = datetime.strptime(raw, f).replace(hour=_MORNING[0], minute=_MORNING[1])
                morning = True
                break
            except ValueError:
                pass
    if due is None:
        return f"日時「{raw}」が読めませんでした。YYYY/MM/DD HH:MM（日付だけなら YYYY/MM/DD）で渡してください。"
    if due < now - timedelta(minutes=1):
        return f"日時「{raw}」はもう過ぎています。いまの日時から数え直してください。"
    return due.strftime("%Y-%m-%d %H:%M"), morning


def label(due: str) -> str:
    """'2026-09-21 15:00' → '9月21日（月）15:00'。空なら空。"""
    if not due:
        return ""
    d = datetime.strptime(due, "%Y-%m-%d %H:%M")
    return f"{d.month}月{d.day}日（{_WEEK[d.weekday()]}）{d.strftime('%H:%M')}"


def _row(r) -> dict:
    d = dict(r)
    d["done"] = bool(d["done"])
    d["label"] = label(d["due"])
    return d


def add(title: str, due_raw: str = "", notes: str = "", who: str = "") -> dict | str:
    """1件足す。返すのは足した行（dict）か断り文（str）。"""
    title = (title or "").strip()[:TITLE_MAX]
    if not title:
        return "用件が空なので、登録していません。"
    p = parse_due(due_raw)
    if isinstance(p, str):
        return p + "登録していません。"
    due, morning = p
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        con = _db()
        try:
            cur = con.execute(
                "INSERT INTO reminders (title, due, notes, who, created_at) VALUES (?,?,?,?,?)",
                (title, due, (notes or "").strip()[:TITLE_MAX], (who or "").strip()[:40], now))
            con.commit()
            row = _row(con.execute("SELECT * FROM reminders WHERE id=?", (cur.lastrowid,)).fetchone())
        finally:
            con.close()
    row["morning"] = morning
    return row


def listing() -> dict:
    """画面に出す一覧。まだのもの（日時の早い順・日時なしは後ろ）と、済んだもの（新しい順・30件）。"""
    with _lock:
        con = _db()
        try:
            open_ = [_row(r) for r in con.execute(
                "SELECT * FROM reminders WHERE done=0 ORDER BY due='' , due, id")]
            done = [_row(r) for r in con.execute(
                "SELECT * FROM reminders WHERE done=1 ORDER BY done_at DESC LIMIT 30")]
        finally:
            con.close()
    return {"open": open_, "done": done, "now": datetime.now().strftime("%Y-%m-%d %H:%M")}


def _get(rid: int) -> dict | None:
    with _lock:
        con = _db()
        try:
            r = con.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        finally:
            con.close()
    return _row(r) if r else None


def set_done(rid: int, done: bool) -> dict | None:
    with _lock:
        con = _db()
        try:
            con.execute("UPDATE reminders SET done=?, done_at=? WHERE id=?",
                        (1 if done else 0, datetime.now().isoformat(timespec="seconds") if done else "", rid))
            con.commit()
            r = con.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        finally:
            con.close()
    return _row(r) if r else None


def edit(rid: int, title: str | None = None, due_raw: str | None = None) -> dict | str | None:
    """用件か日時を書き換える。日時を変えたら、もう一度知らせる（notified_at を空に戻す）。"""
    sets, vals = [], []
    if title is not None:
        t = title.strip()[:TITLE_MAX]
        if not t:
            return "用件が空です。"
        sets.append("title=?")
        vals.append(t)
    if due_raw is not None:
        cur = _get(rid)
        same = cur is not None and (due_raw or "").strip().replace("T", " ")[:16] == cur["due"]
        if not same:                     # 日時を変えないときは読み直さない（過ぎた予定の用件だけ直せるように）
            p = parse_due(due_raw)
            if isinstance(p, str):
                return p
            sets += ["due=?", "notified_at=''"]
            vals.append(p[0])
    if not sets:
        return _get(rid)                 # 変えるところが無い（同じ中身で保存された）＝そのまま返す
    with _lock:
        con = _db()
        try:
            con.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id=?", (*vals, rid))
            con.commit()
            r = con.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
        finally:
            con.close()
    return _row(r) if r else None


def delete(rid: int) -> bool:
    with _lock:
        con = _db()
        try:
            n = con.execute("DELETE FROM reminders WHERE id=?", (rid,)).rowcount
            con.commit()
        finally:
            con.close()
    return n > 0


# ── 知らせる ─────────────────────────────────────────────
def _webhook() -> str:
    return (os.environ.get("KIWAMI_DISCORD_WEBHOOK") or "").strip()


async def _send(text: str) -> bool:
    url = _webhook()
    if not url:
        print("[リマインダー] KIWAMI_DISCORD_WEBHOOK が無いので知らせられません")
        return False
    import httpx
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, json={"content": text[:1900], "username": "リマインダー"})
    return 200 <= r.status_code < 300


async def fire_due(now: datetime | None = None, send=None) -> int:
    """日時が来た（過ぎた）まだのものを、1件ずつ1回だけ知らせる。返すのは知らせた件数。

    ★送れなかったものは印を付けない＝次の見回り（1分後）でもう一度送る。
    ★アプリが止まっていた間に過ぎたものも、起きたときに1回だけ知らせる（予定の時刻を添える）。
    """
    now = now or datetime.now()
    send = send or _send
    stamp = now.strftime("%Y-%m-%d %H:%M")
    with _lock:
        con = _db()
        try:
            rows = [_row(r) for r in con.execute(
                "SELECT * FROM reminders WHERE done=0 AND due!='' AND notified_at='' AND due<=? ORDER BY due",
                (stamp,))]
        finally:
            con.close()
    n = 0
    for r in rows:
        late = (now - datetime.strptime(r["due"], "%Y-%m-%d %H:%M")) > timedelta(minutes=10)
        text = f"⏰ {r['title']}"
        if late:
            text += f"\n（{r['label']} の予定でした）"
        if r["notes"]:
            text += f"\n{r['notes']}"
        try:
            ok = await send(text)
        except Exception as e:                 # 通知の失敗で見張りを止めない
            print(f"[リマインダー] 知らせられませんでした: {type(e).__name__}: {e}")
            ok = False
        if not ok:
            continue
        with _lock:
            con = _db()
            try:
                con.execute("UPDATE reminders SET notified_at=? WHERE id=?",
                            (now.isoformat(timespec="seconds"), r["id"]))
                con.commit()
            finally:
                con.close()
        n += 1
    return n
