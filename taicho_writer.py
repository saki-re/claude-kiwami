# -*- coding: utf-8 -*-
"""クロード極 — 体調の記録の「夜の係」（2026-10-01 新設）

経緯：
  手で書く形では、9/19〜10/1 のあいだに1日ぶんしか入っていなかったため、夜に自動で拾う形（Sonnet 5.5）にした。

いつ書くか：
  手帳のまとめ書き（techo_writer.tick）と同じ、明け方4時すぎの1日1回（proactive._techo_run から続けて呼ぶ）。
  対象のプロジェクト（config.TAICHO_PROJECTS）のスレッドだけを読む。最後の発言から30分あいたものを、新しい順に MAX_THREADS 本。
  日の切れ目は3時（深夜1時に話した「今日」は、前の日の話）。

守ること（taicho.py の決まりのまま）：
  ★拾う根拠は、持ち主の言葉だけ。AI の推し量り・励まし・提案は根拠にしない（記録に作り話を入れない）。
  ★悪い日かは、持ち主がはっきり言ったときだけ。決められなければ空のまま。
  ★手で書いた項目は1文字も変えない（taicho.write_yoru）。夜が書いた項目にだけ書き足す。
  ★体調の言葉が1つも無い日は、Sonnet を呼ばない（0回）。
  ★呼べなかった日（枠切れ・ネット断）は済みにしない＝次の朝にもう一度読む。
    返事が読めなかった日は済みにする（同じ材料で枠を何度も払わないため）。ログに残す。

★呼び方は techo_writer._ask_writer（tools=[]・setting_sources=[]・max_turns=1）をそのまま使う。
★例外は必ずここで握りつぶす。声かけの見回り（proactive.loop）を止めない。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta

import anyio

import config
import store
import taicho
import techo_writer

# ── 目盛り ─────────────────────────────────────────────
MODEL = "claude-sonnet-5-5"   # 夜の係に使うモデルの指定
IDLE_MINUTES = 30             # 最後の発言からこれだけ間が空いたスレッドだけを見る（手帳と同じ）
MAX_THREADS = 5               # 1回に読むスレッドの上限（手帳と同じ。残りは次の朝）
PARALLEL = 2                  # Sonnet を同時に走らせる本数（古いPCでも動くように控えめにしてある。手帳の4本のあとに走る）
BACK_DAYS = 3                 # 会話の日から、さかのぼって書いてよい日数（「おととい」の話まで）
MATERIALS_MAX_CHARS = 12000   # 1回に渡す会話の上限。超えたら分けて渡す
FIELD_MAX_CHARS = 200         # 1項目の長さの保険

STATE_FILE = config.DATA_DIR / "taicho_state.json"

_busy = asyncio.Lock()
_WEEK = "月火水木金土日"

# 体調の話があるかの下見（当たらない日は Sonnet を呼ばない）。広めに取る＝取りこぼすより呼ぶほうを選ぶ
_HINT = re.compile(
    r"痛|いた[いく]|疲|つか?れ|だる|しんど|つら|辛|寝込|ねこ|動けな|うごけな|起きれ|起きられ|起き上が|起きあが|眠れ|寝れ|ねむれ|"
    r"体調|具合|調子|元気|休[んみ]|横にな|頭|熱|吐|気持ち悪|めまい|しびれ|こわば|凝|筋|"
    r"薬|くすり|飲んだ|痛み止め|解熱剤|湿布|シップ|病院|通院|診察|主治医|先生|"
    r"悪い日|よくな|ひどい|限界|ぐったり|むり|無理|代償|反動|寝てた|ねてた")

_BROKEN_REPLY = techo_writer._BROKEN_REPLY


# ── 進捗（data/taicho_state.json）──────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 対象さがし（読むだけ）──────────────────────────────────
def pick_targets(now: datetime | None = None) -> list[dict]:
    """対象プロジェクトのスレッドで、まだ読んでいない発言があるものを新しい順に。生成AIは呼ばない。"""
    now = now or datetime.now()
    st = _load_state()
    out = []
    for p in sorted(store.THREADS_DIR.glob("*.json")):
        if not p.is_file():
            continue
        tid = p.stem
        try:
            t = store.load(tid)
        except Exception as e:
            print(f"[体調の夜] {tid} を読めませんでした（飛ばします）: {type(e).__name__}: {e}")
            continue
        if not t or str(t.get("persona", "1")) not in config.TAICHO_PROJECTS:
            continue
        messages = t.get("messages") or []
        if not messages:
            continue
        last_ts = messages[-1].get("ts", "")
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except (ValueError, TypeError):
            continue
        if (now - last_dt).total_seconds() < IDLE_MINUTES * 60:
            continue
        done_upto = int((st.get(tid) or {}).get("done_upto", 0) or 0)
        done_upto = max(0, min(done_upto, len(messages)))
        if done_upto >= len(messages):
            continue
        out.append({"thread_id": tid, "done_upto": done_upto,
                    "messages": messages[done_upto:], "last_ts": last_ts})
    out.sort(key=lambda c: c["last_ts"], reverse=True)
    return out[:MAX_THREADS]


# ── 材料 → 頼み方 ─────────────────────────────────────
def _day_of(ts: str) -> str:
    """発言の時刻を「記録のうえで何日か」にする。3時より前は前の日。読めなければ ""。"""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    return (dt - timedelta(hours=3)).date().isoformat()


def _by_day(messages: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: list[tuple[str, list[dict]]] = []
    for m in messages:
        day = _day_of(m.get("ts") or "")
        if groups and groups[-1][0] == day:
            groups[-1][1].append(m)
        else:
            groups.append((day, [m]))
    return groups


def _lines(messages: list[dict]) -> list[str]:
    out = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if not text or m.get("kind") == "hikitsugi":
            continue
        if m.get("role") != "user" and text.startswith(_BROKEN_REPLY):
            continue
        who = "持ち主" if m.get("role") == "user" else "アシスタント"
        out.append(f"{who}：{text}")
    return out


def has_hint(messages: list[dict]) -> bool:
    """持ち主の言葉に、体調の話らしい言葉が1つでもあるか（無ければ Sonnet を呼ばない）。"""
    return any(m.get("role") == "user" and _HINT.search(m.get("text") or "") for m in messages)


def _label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{iso}（{_WEEK[d.weekday()]}）"


def _existing(day: str) -> str:
    """いまの記録（会話の日の BACK_DAYS 日前から会話の日まで）。重ねて書かないための参考。"""
    d = date.fromisoformat(day)
    a = (d - timedelta(days=BACK_DAYS)).isoformat()
    try:
        text = taicho.read(a, day)
    except Exception:
        return "（読めませんでした）"
    return text


def prompt_for(day: str, chunk: str) -> str:
    d = date.fromisoformat(day)
    oldest = (d - timedelta(days=BACK_DAYS)).isoformat()
    return (
        "あなたは、持ち主の体調の記録係です。この記録は、体調の経過をあとで見返すために使われます。\n"
        f"次の会話は {_label(day)} の分です。読んで、体調の記録に書くことを拾ってください。\n"
        "\n"
        "# 拾うもの\n"
        "- 持ち主の体の具合（痛み・だるさ・疲れ・寝込んだ・動けなかった・眠れなかった・頭痛 など）\n"
        "- その日にやったこと（体調とつながるものだけ：外出・通院・家事・買い物 など）\n"
        "- その代償（夜の痛み・翌朝の状態・次の日動けなかった など）\n"
        "- 悪い日かどうか：持ち主が「悪い日」「つらい」「動けなかった」「寝込んだ」など、はっきり悪いと言ったときだけ「はい」。"
        "「調子いい」「わりと平気」など、はっきり悪くないと言ったときだけ「いいえ」。決められなければ空。\n"
        "\n"
        "# 守ること\n"
        "- 根拠は「持ち主：」の行だけ。「アシスタント：」の行（推し量り・励まし・提案・質問）は根拠にしない。"
        "アシスタントの質問に持ち主が「うん」と答えたときだけ、その答えを根拠にしてよい。\n"
        "- 言っていないことを書かない。推し量らない。盛らない・弱めない。数（錠・枚・時間・回）は言ったとおり。\n"
        f"- 日付：その日の話はその日。「きのう」「おととい」の話はその日付。翌朝の状態（「今朝つらい」＝前の日にやったことの代償）は、"
        f"前の日（やったことの日）の「代償」に入れる。{oldest} より前の日付と、{day} より後の日付は書かない。\n"
        "- 体調の話が無ければ、何も書かない（空の配列 []）。\n"
        "- 薬・医者の話は、体の具合とつながるときだけ短く。\n"
        "\n"
        "# いまの記録（重ねて書かないための参考。同じことはもう一度書かない）\n"
        f"{_existing(day)}\n"
        "\n"
        "# 出力の形（JSON の配列だけ。前置き・説明・コードの囲みは付けない）\n"
        '[{"hizuke": "YYYY-MM-DD", "yatta": "やったこと", "daisho": "代償・体の具合", "warui": "はい か いいえ か 空"}]\n'
        "1日＝1つ。同じ日は1つにまとめる。それぞれ80字くらいまで。体言止めでよい。\n"
        "\n"
        "会話：\n"
        f"{chunk}\n"
    )


def _chunks(lines: list[str]) -> list[str]:
    out, cur, n = [], [], 0
    for ln in lines:
        if cur and n + len(ln) > MATERIALS_MAX_CHARS:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln[:MATERIALS_MAX_CHARS])
        n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def parse(raw: str, day: str) -> list[dict] | None:
    """Sonnet の返事を行の並びにする。読めなければ None。日付が範囲の外の行は捨てる。"""
    s = (raw or "").strip()
    if not s:
        return []
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1 or b < a:
        return None
    try:
        items = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None
    d = date.fromisoformat(day)
    lo = (d - timedelta(days=BACK_DAYS)).isoformat()
    hi = min(d, date.today()).isoformat()
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        h = str(it.get("hizuke") or "").strip().replace("/", "-")
        if not (len(h) == 10 and lo <= h <= hi):
            if h:
                print(f"[体調の夜] 範囲の外の日付なので書きません: {h}（会話の日 {day}）")
            continue
        row = {"hizuke": h,
               "yatta": str(it.get("yatta") or "").strip()[:FIELD_MAX_CHARS],
               "daisho": str(it.get("daisho") or "").strip()[:FIELD_MAX_CHARS],
               "warui": str(it.get("warui") or "").strip()}
        if row["yatta"] or row["daisho"] or row["warui"]:
            out.append(row)
    return out


# ── Sonnet に聞く（ここだけ差しかえれば 0円で確かめられる）──────────────
async def _ask(prompt: str) -> str | None:
    """文字列＝読めた／None＝呼べなかった（techo_writer._ask_writer と同じ約束）。"""
    return await techo_writer._ask_writer(prompt, model=MODEL)


async def _ask_many(prompts: list[str]) -> list:
    sem = asyncio.Semaphore(PARALLEL)

    async def one(p: str):
        async with sem:
            return await _ask(p)

    return await asyncio.gather(*(one(p) for p in prompts))


# ── 見回り（明け方4時すぎに1日1回・proactive._techo_run から）──────────────
async def tick() -> int:
    """読めるスレッドがあれば、体調の記録へ。書いた（変えた）日の数を返す。例外は中で止める。"""
    if _busy.locked():
        return 0
    async with _busy:
        try:
            targets = await anyio.to_thread.run_sync(pick_targets)
        except Exception as e:
            print(f"[体調の夜] 対象さがしで例外（今回は見送ります）: {type(e).__name__}: {e}")
            return 0
        if not targets:
            return 0
        st = _load_state()
        written = 0
        for t in targets:
            try:
                written += await _one_thread(t, st)
            except Exception as e:
                print(f"[体調の夜] {t['thread_id']} で例外（次の朝にやり直します）: {type(e).__name__}: {e}")
        return written


async def _one_thread(t: dict, st: dict) -> int:
    tid = t["thread_id"]
    pos = t["done_upto"]
    written = 0
    for day, msgs in _by_day(t["messages"]):
        prompts = []
        if day and has_hint(msgs):
            prompts = [prompt_for(day, c) for c in _chunks(_lines(msgs))]
        answers = await _ask_many(prompts) if prompts else []
        if any(a is None for a in answers):
            print(f"[体調の夜] {tid} の {day} は Sonnet を呼べませんでした（次の朝にもう一度読みます）")
            return written
        for raw in answers:
            rows = parse(raw, day)
            if rows is None:
                print(f"[体調の夜] {tid} の {day} は返事が読めませんでした（この日は済みにします）: {raw[:120]!r}")
                continue
            for r in rows:
                msg = taicho.write_yoru(r["hizuke"], r["yatta"], r["daisho"], r["warui"])
                if msg:
                    written += 1
                    print(f"[体調の夜] {msg}")
        pos += len(msgs)
        st[tid] = {"done_upto": pos, "at": datetime.now().isoformat(timespec="seconds")}
        try:
            _save_state(st)          # 1日ごとに書く（途中で再起動が入っても二重に払わない）
        except Exception as e:
            print(f"[体調の夜] 進捗の保存に失敗（次の朝にまた読みます）: {type(e).__name__}: {e}")
    return written
