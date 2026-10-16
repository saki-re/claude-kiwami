# -*- coding: utf-8 -*-
"""クロード極 — スレッドとログの置き場

1スレッド＝1つの JSON ファイル（data/threads/<id>.json）。
持ち主の指示で **バックアップは取らない**。
"""
import json
import uuid
from datetime import datetime
from typing import Optional

import config

THREADS_DIR = config.DATA_DIR / "threads"
THREADS_DIR.mkdir(exist_ok=True)

# 画面に出す「いつ」の形をここで決める
def _when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return ""
    now = datetime.now()
    delta = now - dt
    if delta.total_seconds() < 3600:
        return "いま"
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if (now.date() - dt.date()).days == 1:
        return "きのう"
    # %-m は Windows の strftime にない。数字を直接組む。
    return f"{dt.month}月{dt.day}日"


def _path(tid: str):
    return THREADS_DIR / f"{tid}.json"


def _blank(tid: str, persona: str, model: str) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": tid, "title": "", "persona": persona, "model": model,
        "created": now, "updated": now,
        "heart": False, "star": False, "folder": "", "archived": False,
        "messages": [],
    }


def load(tid: str) -> Optional[dict]:
    try:
        return json.loads(_path(tid).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save(thread: dict) -> None:
    thread["updated"] = datetime.now().isoformat(timespec="seconds")
    _path(thread["id"]).write_text(
        json.dumps(thread, ensure_ascii=False, indent=1), encoding="utf-8")


def create(persona: str = "1", model: str = "", title: str = "") -> dict:
    t = _blank(uuid.uuid4().hex[:12], persona, model)
    t["title"] = title                   # 引き継ぎで作るスレは「〇〇（続き）」を先に付ける
    save(t)
    return t


# ── スレ引き継ぎ（2026-09-17）─────────────────────────────
# ★引き継ぎメモは、新しいスレの**最初の発言**（相手の発言）として置く。印は kind。
#   別の欄にしないのは、①一覧は発言の無いスレを出さない ②起こし直したあとの送りなおし
#   （history_text）にそのまま乗る ③「画面を閉じても書き切る」の仕組みがそのまま使える、から。
HIKITSUGI = "hikitsugi"


def continued_title(title: str) -> str:
    """引き継いだ先の題名。続きの続きでも「（続き）（続き）」と重ねない。"""
    base = (title or "").strip()
    while base.endswith("（続き）"):
        base = base[: -len("（続き）")].rstrip()
    return f"{base or '名前なし'}（続き）"


def delete(tid: str) -> bool:
    p = _path(tid)
    if p.exists():
        p.unlink()
        return True
    return False


def add_message(tid: str, role: str, text: str, *, persona: str = "1",
                model: str = "", thinking: str = "", photos: list | None = None,
                kind: str = "", extra: dict | None = None,
                reminders: list | None = None) -> dict:
    """発言を1つ足す。スレッドが無ければ作る。最初の持ち主の発言をタイトルにする。

    kind・extra … 引き継ぎメモのときだけ付く（kind="hikitsugi"・extra={"from","from_title"}）。
      ふだんの発言には1文字も足さない＝いままでのスレの形は変わらない。
    reminders … その返事で出したリマインダーの札（2026-09-19・remind.py）。札があるときだけ付く。
      画面は開き直したときに、返事の上へ札とボタンを並べ直す。
    """
    t = load(tid) or _blank(tid, persona, model)
    msg = {
        "role": role, "text": text, "thinking": thinking,
        "photos": list(photos or []),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    if kind:
        msg["kind"] = kind
        msg.update(extra or {})
    if reminders:
        msg["reminders"] = list(reminders)
    t["messages"].append(msg)
    if model:
        t["model"] = model
    t["persona"] = persona
    if not t["title"] and role == "user":
        head = text.strip().replace("\n", " ")
        t["title"] = head[:22] + ("…" if len(head) > 22 else "")
    save(t)
    return t


def update_meta(tid: str, **fields) -> Optional[dict]:
    """タイトル・♡・★・フォルダ・奥 を書き換える（画面の ⋯ メニューから）。"""
    t = load(tid)
    if not t:
        return None
    for k in ("title", "heart", "star", "folder", "archived"):
        if k in fields and fields[k] is not None:
            t[k] = fields[k]
    save(t)
    return t


# 一覧の控え。id →（(更新時刻, 大きさ), 一覧1行ぶんの dict）
#
# ★2026-08-20：それまでは一覧を出すたびに、スレを**全部**丸ごと JSON パースしていた
#   （実測 24本・924KB）。出来上がりは本文を載せないので軽いが、読む量は毎回まるごとだった。
#   更新時刻か大きさが動いたものだけ読み直す。動かなければ前に組み立てた1行を使い回す。
#   ・控えが古くなることは無い（書けば必ずどちらかが動く）
#   ・再起動で消えてよい（次に開いたときに1回だけ全部読み直すだけ）
#   ・**「いま」「きのう」だけは毎回作り直す**（時間で変わる表示なので、控えに固めてはいけない）
_listing_cache: dict = {}


def listing() -> list[dict]:
    """一覧に出す分だけ（本文は載せない＝軽くする）。新しい順。"""
    out = []
    seen = set()
    for p in THREADS_DIR.glob("*.json"):
        if not p.is_file():
            continue                     # `〜.json` という名前のフォルダを拾わない
        key = p.stem
        seen.add(key)
        try:
            st = p.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            continue
        hit = _listing_cache.get(key)
        if hit is not None and hit[0] == stamp:
            out.append(dict(hit[1]))     # 控えそのものを渡さない（呼ぶ側に汚されないため）
            continue
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _listing_cache.pop(key, None)
            continue
        if not t.get("messages"):
            _listing_cache.pop(key, None)
            continue                     # 何も話していない空のスレは出さない
        row = {
            "id": t["id"],
            "title": t.get("title") or "（名前なし）",
            "persona": t.get("persona", "1"),
            "model": t.get("model", ""),
            # 話しかけられた回数＝「◯往復」（2026-09-01）。控えに固めてよい
            # ＝発言が増えればファイルの大きさが変わるので、そのとき読み直される。
            "turns": sum(1 for m in t["messages"] if m.get("role") == "user"),
            "updated": "",               # ← 下で毎回入れ直す（控えに固めない）
            "updated_raw": t.get("updated", ""),
            "heart": t.get("heart", False),
            "star": t.get("star", False),
            "folder": t.get("folder", ""),
            "archived": t.get("archived", False),
        }
        _listing_cache[key] = (stamp, row)
        out.append(dict(row))
    # 消えたスレの控えを捨てる（際限なく溜めない）
    for gone in set(_listing_cache) - seen:
        _listing_cache.pop(gone, None)
    # ★「いま」「きのう」は時間で変わるので、控えを使った行でも必ず作り直す
    for r in out:
        r["updated"] = _when(r.get("updated_raw", ""))
    out.sort(key=lambda x: x["updated_raw"], reverse=True)
    return out


def hikitsugi_block(m: dict) -> str:
    """引き継ぎメモの発言を、会話の頭に置く形にする。"""
    src = m.get("from_title") or "前のスレ"
    return (f"【前のスレ「{src}」から、あなた自身が書いた引き継ぎメモ】\n"
            "（前のスレの会話はここには無い。続きは、このメモを踏まえて話す）\n"
            f"{m.get('text', '')}")


def history_text(tid: str, limit: int | None = 40, max_chars: int = 0) -> str:
    """CLI セッションが切れているときに送りなおす「これまでの流れ」。

    limit     … 新しいほうから何発言ぶん。None＝全部（引き継ぎメモを書かせるとき）
    max_chars … 0 より大きければ、流れがその字数に収まるよう古いほうから落とす

    ★引き継ぎメモ（先頭の kind="hikitsugi"）は、limit・max_chars に関わらず**いつもいちばん上**に置く
      （2026-09-17）。40発言を超えたスレで起こし直しても、前のスレの話が抜けないように。
    """
    t = load(tid)
    if not t or not t["messages"]:
        return ""
    msgs = t["messages"]
    head = ""
    if msgs[0].get("kind") == HIKITSUGI:
        head = hikitsugi_block(msgs[0])
        msgs = msgs[1:]
    if limit is not None:
        msgs = msgs[-limit:]
    lines = []
    for m in msgs:
        who = "持ち主" if m["role"] == "user" else "あなた"
        lines.append(f"{who}：{m['text']}")
    if max_chars > 0:
        total = sum(len(s) + 1 for s in lines)
        while lines and total > max_chars:
            total -= len(lines[0]) + 1
            lines.pop(0)
    body = ("これまでの流れ：\n" + "\n".join(lines)) if lines else ""
    return "\n\n".join(p for p in (head, body) if p)
