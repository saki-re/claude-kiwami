# -*- coding: utf-8 -*-
"""クロード極 — 声かけ（AI から話しかける）

持ち主の狙いは **5時間枠の始動**。決めた時刻に生成を1回走らせれば、それで達成される。
（キャッシュ温めは「要らない」との指示なので載せていない。）

作りは元にした別のアプリの proactive.py を薄くしたもの。極は実用の道具なので、
無音時間・おやすみ検知・連続上限といった細かい条件は持たせず、
「決めた時刻に1日1回」だけにしてある。

通知先（ntfy）が設定されていなければ、通知は黙って飛ばす。
声かけ自体はスレッドに残るので、朝ひらけば待っている。

★この1分ごとの見回りは、手帳のまとめ書き（techo_writer.tick）も呼んでいる。
  こちらは **明け方4時すぎに1日1回**（2026-09-04）。
  前は1時間に1回だった。判定は techo_due()。
★手帳のあとに、体調の記録の夜の係（taicho_writer.tick・Sonnet 5.5）も続けて呼ぶ（2026-10-01）。
  今まで手でしまっていたのを、夜に自動で拾う形（Sonnet 5.5）にした。
"""
import asyncio
import json
from datetime import datetime

import config
import store

try:
    import techo_writer            # 手帳のまとめ書き係。部品が無くても声かけは動かす
except Exception as e:
    techo_writer = None
    print(f"[声かけ] 手帳の部品を読み込めませんでした（声かけは続けます）: {type(e).__name__}: {e}")

try:
    import taicho_writer           # 体調の記録の夜の係（2026-10-01）。部品が無くても声かけは動かす
except Exception as e:
    taicho_writer = None
    print(f"[声かけ] 体調の夜の係を読み込めませんでした（声かけは続けます）: {type(e).__name__}: {e}")

STATE_FILE = config.DATA_DIR / "proactive.json"

_techo_tasks: set = set()   # 投げっぱなしにした task を掴んでおく（GC に持っていかれないように）


async def _techo_run() -> None:
    """まとめ書きを1回。**例外はここで全部止める**（声かけの見回りに漏らさない）。"""
    try:
        n = await techo_writer.tick()
        if n:
            print(f"[手帳] まとめを {n} 件書きました")
    except Exception as e:
        print(f"[手帳] まとめ書きで例外（声かけは続けます）: {type(e).__name__}: {e}")
    if taicho_writer is None:
        return
    try:
        n = await taicho_writer.tick()
        if n:
            print(f"[体調の夜] {n} 日ぶんを書きました")
    except Exception as e:
        print(f"[体調の夜] 例外（声かけは続けます）: {type(e).__name__}: {e}")

DEFAULTS = {
    "enabled": False,        # 既定はオフ。持ち主が画面でオンにする
    "hour": 7,
    "minute": 0,
    "last_sent": "",         # 最後に送った日（YYYY-MM-DD）＝1日1回にするための印
    "session_id": "koekake", # 声かけを積んでいくスレッド
    # ── 手帳のまとめ書き（2026-09-04）──
    "techo_hour": 4,         # この時刻を過ぎたら、その日の分を1回だけ書く
    "techo_last": "",        # 最後に書いた日（YYYY-MM-DD）＝1日1回にするための印
}


def techo_due(cfg: dict, now: datetime | None = None) -> bool:
    """いま、手帳のまとめ書きを走らせる時刻か。**1日に1回だけ真になる。**

    ・その日の techo_hour 時を過ぎていて、その日まだ書いていなければ真
    ・アプリが止まっていて4時をまたいだ日は、**起きて最初の見回りで**真になる
      （元にしたアプリの「日付の変わり目の見回り」と同じ拾い方。
        4時ちょうどに起きていなければ書かない、にすると丸1日ぶんが落ちる）
    ★時刻を渡して呼べる形にしてあるのは、0円で確かめられるようにするため（CLAUDE.md 3-2）。
    """
    now = now or datetime.now()
    try:
        hour = int(cfg.get("techo_hour", DEFAULTS["techo_hour"]))
    except (TypeError, ValueError):
        hour = DEFAULTS["techo_hour"]
    if now.hour < hour:
        return False                      # まだ今日の4時前＝前の日の続きなので書かない
    return str(cfg.get("techo_last", "")) != now.strftime("%Y-%m-%d")

# 声かけのときだけ足す合図。表に出ないシスプロ（前置きを書かない等）はシスプロ1がそのまま効く。
TRIGGER = (
    "（これはシステムからの合図です。持ち主はまだ何も言っていません。"
    "今日はじめての声かけとして、2〜3行だけ書いてください。"
    "短い挨拶のあと、今日の予定や手続きで手伝えそうなことを1つだけ聞く。"
    "体調をたずねない。健康の一般論も言わない。長くしない。）"
)


def load() -> dict:
    try:
        return {**DEFAULTS, **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def save(cfg: dict) -> dict:
    cur = load()
    cur.update({k: v for k, v in cfg.items() if k in DEFAULTS and v is not None})
    STATE_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    return cur


async def _notify(text: str) -> None:
    """iPhone へ1通。設定が無ければ黙って何もしない（声かけ自体は止めない）。"""
    topic = getattr(config, "NTFY_TOPIC", "")
    if not topic:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post("https://ntfy.sh", json={
                "topic": topic,
                "title": "アシスタント",
                "message": text.strip()[:120],
                "tags": ["sparkles"],
            })
    except Exception as e:
        print(f"[声かけ] 通知だけ失敗（声かけ自体は残っています）: {type(e).__name__}: {e}")


async def send_now(model: str = "") -> str:
    """いま1回だけ声をかける（画面の「ためす」からも呼ぶ）。返すのは本文。"""
    import agent_bridge          # 循環 import を避けるためここで読む

    cfg = load()
    sid = cfg["session_id"]
    body, reminders = [], []
    async for kind, payload in agent_bridge.stream_reply(
        sid, TRIGGER, persona="1", account="sdk1",
        model=model or "", effort=None, web_search=False,
        seed_text=store.history_text(sid) if agent_bridge.needs_seed(sid) else "",
    ):
        if kind == "text":
            body.append(payload)
        elif kind == "tool" and isinstance(payload, dict) and payload.get("remind"):
            reminders.append(payload["remind"])     # リマインダーの札（app._reply_worker と同じ）

    text = "".join(body).strip()
    if not text:
        return ""

    # add_message はスレッドが無ければ作ってくれる。タイトルは声かけ用に固定する。
    t = store.add_message(sid, "assistant", text, persona="1", model=model, reminders=reminders)
    if not t.get("title"):
        store.update_meta(sid, title="アシスタントからの声かけ")
    await _notify(text)
    return text


async def loop() -> None:
    """1分ごとに見て、決めた時刻を過ぎていて今日まだ送っていなければ1回だけ送る。"""
    while True:
        try:
            cfg = load()
            if cfg["enabled"]:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                due = now.hour * 60 + now.minute >= cfg["hour"] * 60 + cfg["minute"]
                if due and cfg["last_sent"] != today:
                    save({"last_sent": today})      # 先に印を付ける（二重送信よけ）
                    print(f"[声かけ] {today} {cfg['hour']:02d}:{cfg['minute']:02d} の分を送ります")
                    await send_now()
        except Exception as e:
            print(f"[声かけ] ループで例外（続けます）: {type(e).__name__}: {e}")

        try:
            if techo_writer:
                cfg = load()
                if techo_due(cfg):
                    today = datetime.now().strftime("%Y-%m-%d")
                    save({"techo_last": today})     # 先に印を付ける（二重に走らせない）
                    print(f"[手帳] {today} の分をまとめます（{cfg.get('techo_hour', 4)} 時の係）")
                    # ★待たない（2026-09-02・レビューでの指摘）。まとめ書きは Haiku を
                    #   何本も呼ぶので数十秒〜数分かかる。ここで待つと、そのあいだ
                    #   1分ごとの見回りが止まり、朝の声かけがその分だけ遅れる。
                    t = asyncio.create_task(_techo_run())
                    _techo_tasks.add(t)
                    t.add_done_callback(_techo_tasks.discard)
        except Exception as e:
            print(f"[手帳] まとめ書きの呼び出しで例外（声かけは続けます）: {type(e).__name__}: {e}")

        # リマインダーのディスコードへの知らせは止めた（2026-09-19）。知らせるのは iPhone のリマインダー（札のボタン → 公式 Claude アプリ）。
        # 戻すときは reminder_app.fire_due() をここで呼ぶ（git の履歴にある）。

        await asyncio.sleep(60)
