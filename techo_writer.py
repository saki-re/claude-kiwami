# -*- coding: utf-8 -*-
"""クロード極 — 手帳の「粒を書く係」

持ち主の指示（2026-09-03）：
  手帳に話した内容そのままが入っていたので、元にしたアプリと同じ形に直す。
  → 手帳に入るのは、話題ごとの粒だけ。会話そのままは手帳に入れない（会話はスレッドに残る）。

持ち主の指示（2026-09-04）：
  覚える内容が長すぎる。意味のあること、また同じキャラと話した感じになる記憶であること。
  長さは箇条書きで構わない。その場限りの雑談でも少しは残す（「この前言ってたね」と続けられるように）。
  Haiku は並列で何本か同時に出して時短する。
  → **1粒＝1行の箇条書き**（40〜120字めやす・上限200字）。段落では書かない。
    残す基準は「意味のあること」＋「同じ相手として続けるための手がかり」の6つ（_prompt_for に）。
    雑談も、印象に残ったものを1日1〜2件だけ残す（「この前言ってたね」と続けられるように）。
  ※前は、元にしたアプリの日記プロンプトをそのまま写して
    「話題ごとに2〜5文の段落」と頼んでいた。日記は読み物なので段落が正しいが、
    実務の手帳には長すぎた（13日で286件・平均370字・実機で実測）。

いつ書くか：
  1分ごとの声かけループが **明け方4時すぎに1日1回** tick() を呼ぶ（proactive.py）。
  最後の発言から30分あいたスレッドを、新しい順に MAX_THREADS 本まで読む。
  ★日ごとに分けて書く（元にしたアプリが1日単位なのと同じ）。長い日は MATERIALS_MAX_CHARS ずつに分けて、頭を落とさない。
  一度に書き直すときは backfill()（POST /techo/redigest・道具は techo_import.py）。

★生成AIは呼ぶが、モデルは Sonnet 5・tools=[]・max_turns=1 の一問一答だけ。
  サブスク枠（5時間枠・サブスク1＝持ち主のアカウント）の中なので追加のお金はかからない。
★**Sonnet は PARALLEL 本まで同時に走らせる**（2026-09-04 持ち主の指示）。
  待ち時間はほぼ全部が Sonnet の返事待ちなので、ここを並べるとそのぶん短くなる。
  ただし1本ごとに CLI の子プロセスが1つ立つので、動かしている古いPCが苦しくならない本数で止める。
★呼び方は agent_bridge.py の _options()／stream_reply と同じ「claude_agent_sdk の
  query() ＋ ClaudeAgentOptions」をそのまま真似ている（新しい呼び方は作らない）。
★例外は必ずここで握りつぶす。手帳の書きものが失敗しても、声かけ（proactive.py の
  1分ループ）を止めてはいけない。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query as agent_query,
)

import config
import projects
import store
import techo

# ── 目盛り ─────────────────────────────────────────────
IDLE_MINUTES = 30          # 最後の発言からこれだけ間が空いたスレッドだけを見る
MAX_THREADS = 5            # 1回の tick で処理するスレッドの上限。★1日1回になったので3→5（2026-09-04）。
                           #   1時間ごとなら取りこぼしても次の回で拾えたが、いまは次が翌朝4時になる
MIN_MATERIALS = 2          # 材料（まだ書いていない発言）がこれ未満の日は飛ばす
GRAIN_MAX_CHARS = 200      # 1粒の長さの保険。★600→200（2026-09-04 長すぎたため）。
                           #   Sonnet には 40〜120字の箇条書き1行と頼む。ここは万一のときの手すり
MATERIALS_MAX_CHARS = 12000  # 1回に渡す材料の上限。★超えたら**分けて**渡す（頭を落とさない・2026-09-03）
PARALLEL = 4               # Sonnet を同時に走らせる本数（2026-09-04 時短のため）。
                           # ★1本ごとに CLI の子プロセスが1つ立つ。古いPCで動かす想定なので、増やすときは
                           #   ファンの音と _claude_console.log を見てから
BACKFILL_BATCH = 5         # 書き直しのとき、いちどに束ねるスレッドの本数（この束の中で PARALLEL 本ずつ走る）
BACKFILL_GAP_SEC = 1.0     # 書き直しのとき、束と束のあいだに置く間
BACKFILL_RETRY_SEC = 30    # 呼べなかったあと、もう一度だけ試すまでの間

# ★2026-09-12 ハイク → ソネット5（事実がねじ曲がっていたため）。
#   ハイクは「会話の写しはしない」「誰が言ったか取り違えない」を守りきれず、
#   相手の返事の丸写しや、取り違えた事実が粒になっていた（袋3・279件で実測）。
WRITER_MODEL = "claude-sonnet-5"

STATE_FILE = config.DATA_DIR / "techo_state.json"

# Sonnet の出力が「- ・ 1. 」のような前置きを付けてきたときに剥がす保険
_BULLET_RE = re.compile(r"^\s*(?:[\-\*・•]|\d+[\.\)、])\s*")
# 「0件」「（書くことはありません）」のような**返事そのもの**を粒にしない（2026-09-02 に「0件」が粒として残っていた）
_META_RE = re.compile(r"(0件|書くこと|覚えておく|値打ち|何も(書|出)さ|ありません|特にな[しい])")
# 名前として字になっているか（絵文字だけの名前の袋では、名前を名乗らせない）
_WORDY = re.compile(r"[0-9A-Za-zぁ-んァ-ヶ一-龠ー]")
# 極の返事のうち、覚える値打ちの無いもの（部品の不調がそのまま返事になった回）
_BROKEN_REPLY = ("Failed to authenticate", "API Error", "……（うまく言葉が出ませんでした")

# 書き直し（backfill）の進み具合。画面や道具が GET /techo/redigest で読む
STATUS: dict = {"running": False, "done": 0, "total": 0, "grains": 0,
                "note": "", "started": "", "ended": ""}
_busy = asyncio.Lock()     # tick と backfill を同時に走らせない（進捗ファイルを取り合わない）


# ── 進捗（data/techo_state.json）───────────────────────────
def _load_state() -> dict:
    """{"<thread_id>": {"done_upto": int, "at": str}}。壊れていても空dictで続ける。"""
    try:
        # utf-8-sig … PowerShell が付けた BOM があっても読めるように（この家の決まり）
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    """BOM無しの UTF-8 で書く（この家の決まり）。"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 対象さがし（読むだけ・書き込みはしない）───────────────────────
def pick_targets(now: datetime | None = None, *, everything: bool = False,
                 pid: str | None = None) -> list[dict]:
    """書く対象のスレッドを選ぶ。**生成AIは呼ばない／state.jsonへは書かない**。

    条件：
      ・最後の発言から IDLE_MINUTES 分以上たっている
      ・data/techo_state.json の done_upto より messages が増えている
      ・projects.get(pid).get("memory") が真の袋だけ（pid を渡したときは、その袋だけ）

    ふだん（everything=False）は新しいスレッドから MAX_THREADS 本。
    書き直し（everything=True）は、全部を**古い順**に（手帳が当時の並びで埋まっていく）。
    1件の形：{"thread_id","pid","done_upto","messages"（材料そのもの）,"last_ts"}
    """
    now = now or datetime.now()
    state = _load_state()
    out = []
    for p in sorted(store.THREADS_DIR.glob("*.json")):
        if not p.is_file():
            continue
        tid = p.stem
        try:
            t = store.load(tid)
        except Exception as e:
            print(f"[techo_writer] {tid} を読めませんでした（飛ばします）: {type(e).__name__}: {e}")
            continue
        if not t:
            continue
        messages = t.get("messages") or []
        if not messages:
            continue

        tpid = str(t.get("persona", "1"))
        if pid and tpid != str(pid):
            continue
        try:
            if not projects.get(tpid).get("memory"):
                continue
        except Exception:
            continue          # プロジェクトの札が読めない＝安全側で飛ばす

        last_ts = messages[-1].get("ts", "")
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except (ValueError, TypeError):
            continue
        if (now - last_dt).total_seconds() < IDLE_MINUTES * 60:
            continue

        done_upto = int((state.get(tid) or {}).get("done_upto", 0) or 0)
        done_upto = max(0, min(done_upto, len(messages)))
        materials = messages[done_upto:]
        if len(materials) < MIN_MATERIALS:
            continue

        out.append({
            "thread_id": tid, "pid": tpid, "done_upto": done_upto,
            "messages": materials, "last_ts": last_ts,
        })

    if everything:
        out.sort(key=lambda c: c["last_ts"])
        return out
    # ★新しいスレッドから順に選ぶ（2026-09-02）。
    #   古い順にすると、今日話したことが手帳に入るのが何時間も先になる。
    out.sort(key=lambda c: c["last_ts"], reverse=True)
    return out[:MAX_THREADS]


def _threads_of(pid: str) -> set:
    """その袋のスレッド id（書き直しで進捗を消すときに使う）。"""
    out = set()
    for p in store.THREADS_DIR.glob("*.json"):
        try:
            t = store.load(p.stem)
        except Exception:
            continue
        if t and str(t.get("persona", "1")) == str(pid):
            out.add(p.stem)
    return out


# ── 材料 → プロンプト ─────────────────────────────────────
def _me(name: str) -> str:
    """やりとりの中で「自分」に付ける札。名前が字なら名前、絵文字だけなら「私」。"""
    return name if _WORDY.search(name or "") else "私"


def _persona_name(pid: str) -> str:
    try:
        return projects.name_of(pid) or ""
    except Exception:
        return ""


def _prompt_for(name: str) -> str:
    """粒を書くときの頼み方。**1粒＝1行の箇条書き**（2026-09-04 持ち主の指示）。

    残す基準は6つ。①〜⑤が「意味のあること」、⑥が「同じ相手として続けるための手がかり」。
    ⑥は「その場限りの雑談でも少しは残す。次に『この前言ってたね』と続けられるから」という方針を形にしたもの。
    """
    me = _me(name)
    head = f"あなたは「{me}」。" if me != "私" else ""
    return (
        f"{head}持ち主と交わした次のやりとりを読んで、自分の手帳に覚え書きを書きます。\n"
        "これは会話の写しではなく、あとで読み返して思い出すための、自分の記憶です。\n"
        "\n"
        "# 残すこと（この6つに当てはまるものだけ）\n"
        "1. 決まったこと・変わったこと。金額・日付・場所・品名・数は、やりとりに出たとおり正確に。\n"
        "2. 続きがある用事。次に何をするか、誰に何を出すか。\n"
        "3. 持ち主のこと。体のこと・好み・できないこと・お決めごと。\n"
        "4. 自分が引き受けた約束。\n"
        "5. 呼び方・言葉づかい・態度の取り決め。次も同じ相手として続けられるように。\n"
        "6. 雑談でも、印象に残ったこと。**この日ぜんぶで1つか2つだけ**。\n"
        "   次に会ったとき「この前言ってたね」と続けられるもの（見たもの・食べたもの・"
        "あった出来事・好きだと言われたもの）。\n"
        "\n"
        "# 残さないこと\n"
        "- あいさつ・相槌そのもの（「おはよう」「了解」「ありがとう」）。\n"
        "- 作業の実況（「いま読んでいます」「書けました」「探してみます」）。\n"
        "- 調べれば分かる一般論。制度や仕組みの説明そのものは書かず、"
        "**持ち主に当てはめた結論だけ**を残す。\n"
        "- 自分の感想だけの文。\n"
        "- 会話の写し。表・箇条書きの丸写し・かぎかっこの引用はしない。\n"
        "- やりとりに無いこと。本当にあったことだけを書く。\n"
        "- 残すものが無ければ、何も書かずに終える（無理に絞り出さない）。\n"
        "\n"
        "# 書き方\n"
        "- **1件を1行**にする。1行に1つのことだけ。行と行は改行で分ける。\n"
        "- 1行は **40〜120字**くらい。長くても200字を超えない。\n"
        "- 体言止めでよい。ていねいな地の文にしなくてよい。前置き・見出し・番号・締めの言葉は付けない。\n"
        f"- 自分のことは「私」と書く。誰が言ったことか取り違えない（「持ち主：」の行が持ち主、「{me}：」の行が自分）。\n"
        "\n"
        "# 例（この長さ・この書き方）\n"
        "ゴミの日は火曜と金曜。資源ごみは第2水曜\n"
        "作り置きは日曜にまとめて作る。3日で食べきる\n"
        "プリンターのインクは黒だけ先に切れる。黒を2本置いておく\n"
        "傘は玄関の右の棚。折りたたみは鞄に入れたまま\n"
        "\n"
        "# 出力の形\n"
        "箇条書きの行だけを、1行に1件ずつ。日付・題名・記号・番号・「・」は付けない。"
        "書くことが無ければ、何も出さない。\n"
        "\n"
        "やりとり：\n"
    )


def _by_day(messages: list[dict]) -> list[tuple[str, list[dict]]]:
    """発言の並びを、日ごとのかたまりに分ける（順番はそのまま）。

    ★元にしたアプリの記憶が1日単位なのと同じ。スレッドが何日にもまたがっていても、
      粒はその話をした日に入る（前は全部が「最後の日」に入っていた）。
    """
    groups: list[tuple[str, list[dict]]] = []
    for m in messages:
        day = (m.get("ts") or "")[:10]
        if not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
            day = ""
        if groups and groups[-1][0] == day:
            groups[-1][1].append(m)
        else:
            groups.append((day, [m]))
    return groups


def _material_lines(messages: list[dict], me: str) -> list[str]:
    """発言を「誰：本文」の行にする。部品の不調がそのまま返事になった回は材料にしない。"""
    lines = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if m.get("role") != "user" and text.startswith(_BROKEN_REPLY):
            continue
        # 引き継ぎメモ（2026-09-17）は前のスレの話のまとめ。前のスレから粒を書いているので、
        # ここでも材料にすると手帳に同じ話が二重に入る。
        if m.get("kind") == "hikitsugi":
            continue
        who = "持ち主" if m.get("role") == "user" else me
        lines.append(f"{who}：{text}")
    return lines


def _chunks(lines: list[str], limit: int = MATERIALS_MAX_CHARS) -> list[str]:
    """行の並びを、limit 字以内のかたまりに分ける（発言の途中では切らない）。

    ★前は「超えたら頭を落とす」だった。書き直しでは1日18万字の日もあり、
      頭を落とすとその日の大半が手帳に入らない。分けて全部渡す（2026-09-03）。
    """
    out, cur, n = [], [], 0
    for ln in lines:
        if len(ln) > limit:
            ln = ln[:limit] + "…"          # 1発言だけで上限を超える＝そこだけ末尾を切る
        if cur and n + len(ln) + 1 > limit:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _cut(s: str) -> str:
    """長すぎる粒を、文の切れ目（。）で止める。切れ目が手前に無ければ末尾に…。

    ★「…」も1字に数える（2026-09-04 の確かめで、上限を1字だけ超えていた）。
    """
    head = s[:GRAIN_MAX_CHARS]
    i = head.rfind("。")
    return head[: i + 1] if i >= GRAIN_MAX_CHARS // 2 else head[:GRAIN_MAX_CHARS - 1] + "…"


GRAIN_MIN_CHARS = 20       # これ未満は粒にしない（箇条書きの1行にならない切れ端・見出しだけの行）


def _is_table(s: str) -> bool:
    """マークダウンの表の行か。★表がそのまま1粒として残っていた（2026-09-04 実機で実測）。

    表は会話の写しであって、覚え書きではない。区切り線（|---|---|）もここで落ちる。
    """
    return s.startswith("|") or s.count("|") >= 2


def _split_grains(raw: str, me: str = "私") -> list[str]:
    """Sonnet の出力を、粒に分ける。★**1行＝1粒**（2026-09-04）。

    ・行ごとに切る。前は「空行で区切った段落」を1粒にしていたが、箇条書きを頼むように
      なったので、行をつなげてはいけない（つなげると1粒に2つの話が混ざる）
    ・頭の記号（- ・ 1.）と、太字・見出しの飾り（* #）は剥がす
    ・「0件」「書くことはありません」のような返事そのものは粒にしない
    ・「持ち主：」「{me}：」の札が残っている行＝会話の写しなので粒にしない（2026-09-03・書き直しで9件混ざった）
    ・表の行は粒にしない（2026-09-04）
    """
    labels = ("持ち主：", f"{me}：")
    out = []
    for ln in (raw or "").strip().splitlines():
        s = _BULLET_RE.sub("", ln.strip()).strip().strip("*#＊ 　")
        if len(s) < GRAIN_MIN_CHARS:
            continue
        if _META_RE.search(s) and len(s) < 60:
            continue
        if any(lb in s for lb in labels) or s.startswith(_BROKEN_REPLY):
            continue
        if _is_table(s):
            continue
        if len(s) > GRAIN_MAX_CHARS:
            s = _cut(s)
        out.append(s)
    return out


async def _ask_writer(prompt: str, model: str = "") -> str | None:
    """Sonnet 5 に一問一答で聞く。tools=[]・setting_sources=[]・max_turns=1。

    model … 渡したときだけ、そのモデルで聞く（体調の夜の係 taicho_writer.py が Sonnet 5.5 で使う）。

    ★呼び方は agent_bridge._options()／stream_reply と同じ形（新しい呼び方は作らない）。
    ★返しは3つに分かれる ── 文字列＝書けた／`""`＝読んだけれど粒は0件だった／
      **`None`＝呼べなかった**（鍵切れ・枠切れ・ネット断など）。
      ここを分けるのが要（2026-09-02）。分けないと、呼べなかった回まで「済み」にしてしまい、
      **枠が細っていた時間の会話が、二度と書かれなくなる**。
    """
    common = dict(
        model=model or WRITER_MODEL,
        tools=[],                 # 標準ツールは1つも積まない（道具を使わせない一問一答）
        setting_sources=[],       # CLAUDE.md 等を読み込ませない
        max_turns=1,
    )
    tok1, _tok2 = config.sdk_tokens()   # サブスク枠（sdk1）で叩く。毎回 .env から読み直す
    if tok1:
        # 設定の場所を分けて、メールアドレスを添えさせない（2026-09-13・config.CLI_HOME のコメント参照）
        common["env"] = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": tok1,
                         "CLAUDE_CONFIG_DIR": str(config.CLI_HOME)}
    options = ClaudeAgentOptions(**common)

    body = []
    agen = agent_query(prompt=prompt, options=options)
    try:
        async for msg in agen:
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        body.append(block.text)
    except Exception as e:
        print(f"[techo_writer] Sonnet を呼べませんでした（この回は済みにしません）: "
              f"{type(e).__name__}: {e}")
        return None
    finally:
        try:
            await agen.aclose()
        except BaseException:
            pass
    return "".join(body).strip()


async def _ask_many(prompts: list[str]) -> list:
    """まとめて Sonnet に聞く。**PARALLEL 本ずつ同時**に走らせ、渡した順のまま返す。

    ★待ち時間のほとんどは Sonnet の返事待ちなので、ここを並べたぶんだけ短くなる
      （2026-09-04 時短のため）。
    ★1本ずつの返しの意味は _ask_writer と同じ ── 文字列＝書けた／`""`＝粒は0件だった／
      `None`＝呼べなかった。**None を「粒0件」と混ぜてはいけない**（呼べなかった回まで
      済みにすると、枠が細っていた時間の会話が二度と書かれなくなる）。
    """
    if not prompts:
        return []
    sem = asyncio.Semaphore(PARALLEL)

    async def one(p: str):
        async with sem:
            return await _ask_writer(p)

    return await asyncio.gather(*(one(p) for p in prompts))


# ── 何を聞くか決める（生成AIは呼ばない）────────────────────────
def _plan_thread(t: dict) -> list[dict]:
    """1本のスレッドを、日ごとの仕事に割る。**Sonnet は呼ばない。**

    1件の形：{"day", "count"（その日の発言数）, "me", "prompts"（0本のこともある）}
    """
    name = _persona_name(t["pid"])
    head, me = _prompt_for(name), _me(name)
    days = []
    for day, msgs in _by_day(t["messages"]):
        lines = _material_lines(msgs, me)
        chunks = _chunks(lines) if len(lines) >= MIN_MATERIALS else []
        days.append({"day": day, "count": len(msgs), "me": me,
                     "prompts": [head + c for c in chunks]})
    return days


# ── 何本かのスレッドを、まとめて書く ──────────────────────────
async def _write_threads(targets: list[dict], state: dict) -> tuple[int, int, int]:
    """(書いた粒の数, 最後まで読めた本数, 呼べずに残した本数) を返す。

    ★Sonnet の呼び出しだけを先に全部並べて走らせ、**手帳へ書くのは返事がそろってから順番に**。
      書くほうを並べないのは、原本（jsonl）の並びと重複よけを素直に保つため。
    ★日ごとに済んだ位置を書く。呼べなかった日が出たら、そのスレッドはそこで止める＝
      済んでいない日は、次の回にもう一度読まれる（同じ材料を二度払わない・取りこぼさない）。
    """
    plans = [(t, _plan_thread(t)) for t in targets]

    prompts, index = [], []
    for ti, (_t, days) in enumerate(plans):
        for di, d in enumerate(days):
            for p in d["prompts"]:
                index.append((ti, di))
                prompts.append(p)
    answers = await _ask_many(prompts)

    got: dict = {}
    for key, a in zip(index, answers):
        got.setdefault(key, []).append(a)

    total, done, skipped = 0, 0, 0
    for ti, (t, days) in enumerate(plans):
        tid, pid = t["thread_id"], t["pid"]
        pos, ok = t["done_upto"], True
        for di, d in enumerate(days):
            raws = got.get((ti, di), [])
            if any(r is None for r in raws):
                ok = False
                break                     # 呼べなかった＝この日から先は済みにしない
            for raw in raws:
                for grain in _split_grains(raw, d["me"]):
                    try:
                        if await techo.remember_note(pid, tid, grain, day=d["day"] or None):
                            total += 1
                    except Exception as e:
                        print(f"[techo_writer] 粒を入れられませんでした（次へ進みます）: "
                              f"{type(e).__name__}: {e}")
            pos += d["count"]
            state[tid] = {"done_upto": pos, "at": datetime.now().isoformat(timespec="seconds")}
            try:
                _save_state(state)        # 1日ごとに書く（途中で再起動が入っても二重に払わない）
            except Exception as e:
                print(f"[techo_writer] 進捗の保存に失敗（次回また読みに来ます）: {type(e).__name__}: {e}")
        if ok:
            done += 1
        else:
            skipped += 1
    return total, done, skipped


# ── ふだんの見回り（明け方4時すぎに1日1回・proactive.loop から） ──────────
async def tick() -> int:
    """書けるスレッドがあれば、粒にして手帳へ。書いた粒の数を返す。

    例外は中で握りつぶす（声かけを止めないため）。書き直し（backfill）の最中は何もしない。
    """
    if _busy.locked():
        return 0
    async with _busy:
        try:
            # スレッドを全部読む同期の仕事なので、別のスレッドへ逃がす
            # （そのあいだサーバー全体が止まらないように・2026-09-02・レビューでの指摘）
            targets = await anyio.to_thread.run_sync(pick_targets)
        except Exception as e:
            print(f"[techo_writer] 対象さがしで例外（今回は見送ります）: {type(e).__name__}: {e}")
            return 0
        if not targets:
            return 0

        state = _load_state()
        try:
            total, _done, skipped = await _write_threads(targets, state)
        except Exception as e:
            print(f"[techo_writer] まとめ書きで例外（次の回にやり直します）: {type(e).__name__}: {e}")
            return 0
        if skipped:
            print(f"[techo_writer] {skipped} 本は Sonnet を呼べず残りました（次の回にやり直します）")
        return total


# ── 書き直し（POST /techo/redigest・1回きり） ──────────────────────
async def backfill(pid: str | None = None, wipe: bool = False) -> dict:
    """これまでの会話を、新しい書き方で手帳へ入れ直す。**8030 の中で走らせる**（索引は1プロセスから）。

    wipe=True … 先にその袋の手帳を空にする（写しは呼ぶ側が取っておく）。
    ★Sonnet を呼べない回が2回続いたら止める（CLAUDE.md 3-1）。残りは tick() が翌朝4時に続ける。
    """
    if STATUS.get("running"):
        return {"ok": False, "reason": "いま書いている途中です"}
    now = datetime.now().isoformat(timespec="seconds")
    # ★先に「走っている」印を立ててから鍵を待つ（2026-09-03）。起動直後は tick() が鍵を持っていることが
    #   あり、ここで諦めると「始まりました」と返したのに何も書かれない（実際に1回そうなった）。
    STATUS.update(running=True, done=0, total=0, grains=0,
                  note="明け方の見回りが終わるのを待っています", started=now, ended="")
    try:
        async with _busy:
            STATUS["note"] = ""
            state = _load_state()
            if wipe:
                if pid:
                    pids = [str(pid)]
                else:
                    pids = [str(p["id"]) for p in projects.listing() if p.get("memory")]
                for one in pids:
                    n = await techo.clear(one)
                    for tid in await anyio.to_thread.run_sync(_threads_of, one):
                        state.pop(tid, None)
                    print(f"[techo_writer] 袋 {one} の手帳を空にしました（{n} 件）")
                _save_state(state)

            targets = await anyio.to_thread.run_sync(
                lambda: pick_targets(everything=True, pid=pid))
            STATUS["total"] = len(targets)
            # ★BACKFILL_BATCH 本ずつ束ねて渡す。束の中では Sonnet が PARALLEL 本ずつ同時に走る
            #   （2026-09-04）。全部いちどに投げないのは、古いPCに子プロセスを
            #   積み上げないため。止める判定は「束ごと」に見る。
            fails, skipped, stopped = 0, 0, False
            for i in range(0, len(targets), BACKFILL_BATCH):
                batch = targets[i:i + BACKFILL_BATCH]
                n, done, miss = await _write_threads(batch, state)
                STATUS["grains"] += n
                STATUS["done"] += done
                skipped += miss
                if done:
                    fails = 0
                    await asyncio.sleep(BACKFILL_GAP_SEC)
                    continue
                fails += 1
                if fails >= 2:
                    STATUS["note"] = ("Sonnet を2回続けて呼べなかったので止めました"
                                      "（残りは明け方4時の係が続けます）")
                    stopped = True
                    break
                await asyncio.sleep(BACKFILL_RETRY_SEC)
            if not stopped:
                STATUS["note"] = ("書き終えました" if not skipped else
                                  f"{skipped} 本は Sonnet を呼べず残りました（明け方4時の係が続けます）")
    except Exception as e:
        STATUS["note"] = f"途中で例外: {type(e).__name__}: {e}"
        print(f"[techo_writer] 書き直しで例外: {type(e).__name__}: {e}")
    finally:
        STATUS["running"] = False
        STATUS["ended"] = datetime.now().isoformat(timespec="seconds")
    return dict(STATUS)
