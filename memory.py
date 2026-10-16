# -*- coding: utf-8 -*-
"""クロード極 — メモリ（差し込み式・1ファイル1件）

持ち主の指示：
  - **差し込み式**。ベクトルは使わない
  - 公式クロードのように**勝手に覚えていく**
  - 持ち主が**消したり編集できる**
  - **プロジェクトごとに袋を分ける**（2026-08-02 改定。もとは「シスプロ1のときだけ」）

全件をシスプロの後ろに差し込む形なので、件数が増えるとその分だけ入力が伸びる。
ベクトル検索を入れない代わりに、増えすぎたら持ち主が消せる形にしてある。

★袋を分けた理由（2026-08-02）：おしゃべり用の記憶が実務の入力を太らせないため。
  実務は検索オフで 265〜310 トークンで話せている。そこを崩さない。
"""
import json
import re
import uuid
from datetime import datetime

import config

MAX_INJECT = 100         # 差し込む上限（新しい順）。これを超えたら古いものは入れない
# ★2026-10-15 に 60 → 100。袋1が61件になり、いちばん古い記憶が黙って落ちていた。
#   長い資料をたたむようになって1件ずつが軽くなったので、上限で切るより件数を通すほうを採った。

# ── みんな共通の袋（プロジェクト貫通メモリ・2026-09-02 新設）──────────────
# 持ち主の指示：貫通メモリは新規でつくる。みんなそれぞれ書きこめて、見れるもの。
#
# ★いまの袋（"1" 実務・"2" クロード・"3" など）には1件も触らない。
#   **新しい袋を1つ足すだけ**。出し入れは、この下の add/update/delete/listing/find が
#   そのまま使える（pid のところへ SHARED を渡すだけ）＝ゴミ箱も戻すのもそのまま効く。
#
# ★ID を "_all" にした理由：プロジェクトの ID（"1"… や uuid の16進12文字）と
#   ぶつからない形にするため。頭に "_" が付く ID は画面からは作れない。
#   置き場は data/memory/_all/ で、ほかの袋と同じ並び。
SHARED = "_all"
SHARED_NAME = "みんな共通"

# 共通の袋は**どのプロジェクトにも積まれる**ので、上限は自分の袋より辛めにする。
# （実務は検索オフで 265〜310 トークンで話せている。そこを崩さないため）
MAX_INJECT_SHARED = 40

# ── たたむ（2026-10-14）──────────────────────────────────────
# 持ち主の困りごと：メモリが長すぎる。
# 実務の人格のシスプロ 37,744 字のうち 3万1千字がメモリで、その大半は1件 600〜1,200 字の資料
# （済んだ用事の一覧・書類の要点など）だった。
# ★たたんだ記憶は、シスプロへ「頭の部分」だけ積む。中身は1文字も変えない・消さない。
#   全文は listing() がそのまま返す＝recall_memories・画面・事実の見張りには全文が届く。
# ★たたむかどうかは1件ずつ（"fold": true）。持ち主が画面で切り替える。新しく覚えた記憶はひらいたまま。
#   作った日にアシスタントがたたんだのは、済んだ用事の資料4件だけ。
# ★頭（見出し）は AI に書かせない。文の頭をそのまま切る＝書き換えも作り話も起きない。
# ★「★」で始まる記憶（戒め・決まり）は、たたんであっても全文を積む。
FOLD_MARK = "（たたみ）"
HEAD_MAX = 60            # 頭の長さの上限。超えたら「…」で切る

_ROOT = config.DATA_DIR / "memory"
_ROOT.mkdir(exist_ok=True)


def _dir(pid: str):
    """そのプロジェクトの袋。無ければ作る。"""
    d = _ROOT / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(pid: str, mid: str):
    return _dir(pid) / f"{mid}.json"


def _trash_dir(pid: str):
    """捨てたものの置き場。listing() は見に行かない＝シスプロには積まれない。"""
    d = _dir(pid) / "_trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_flat() -> int:
    """2026-08-02 までは data/memory/ に平置きだった。実務（"1"）の袋へ移す。

    1回きりの引っ越し。起動のたびに呼んでよい（移すものが無ければ何もしない）。
    """
    moved = 0
    for p in _ROOT.glob("*.json"):
        target = _dir("1") / p.name
        if not target.exists():
            p.rename(target)
            moved += 1
    return moved


def add(pid: str, text: str, source: str = "ai", by: str = "") -> dict | None:
    """1件覚える。同じ袋の中にまったく同じ文がもうあれば覚えない（重複よけ）。

    by … 書いた相手の名前（2026-09-02）。**みんな共通の袋のためだけ**にある。
      共通の袋はどのプロジェクトからも書けるので、名前を残さないと
      画面の一覧で「これは誰が書いたのか」が分からなくなる。
      自分の袋では空のまま＝ファイルの形はいままでと1文字も変わらない。
    """
    text = (text or "").strip()
    if not text:
        return None
    for m in listing(pid):
        if m["text"] == text:
            return None
    m = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "source": source,                      # ai＝AIが書いた / user＝持ち主が書いた
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    if by:
        m["by"] = by
    _path(pid, m["id"]).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def update(pid: str, mid: str, text: str) -> dict | None:
    p = _path(pid, mid)
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    m["text"] = text.strip()
    m["edited"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def delete(pid: str, mid: str) -> bool:
    """1件忘れる。**消さずにゴミ箱へ移す**（2026-08-28）。

    ★アシスタント自身が忘れられるようにした日に、そのまま消す作りをやめた。
      AI が誤って捨てても、持ち主が戻せる場所に残っていれば実害が出ない。
      ゴミ箱は listing() に出ないので、シスプロにも一覧にも積まれない＝入力は増えない。
    """
    p = _path(pid, mid)
    if not p.exists():
        return False
    t = _trash_dir(pid) / p.name
    if t.exists():                       # 同じ id を2度捨てたとき（普通は起きない）
        t.unlink()
    p.rename(t)
    return True


def set_fold(pid: str, mid: str, on: bool) -> dict | None:
    """たたむ／ひらく（2026-10-14）。中身（text）には触らない。"""
    p = _path(pid, mid)
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    if on:
        m["fold"] = True
    else:
        m.pop("fold", None)              # ひらいた記憶は、たたむ前と同じ形に戻す
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m


def head(text: str) -> str:
    """たたんだ記憶の頭。【…】で始まればそこまで、ほかは最初の「。」の手前まで。

    ★閉じていない「（」で終わるときは、その「（」の手前で切る（括弧の中で途切れると読みにくい）。
    ★改行は空白にする（シスプロの1行に収める）。空になったら文の頭をそのまま使う。
    """
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if t.startswith("【") and "】" in t:
        s = t[:t.index("】") + 1]
    else:
        s = t.split("。", 1)[0]
        if s.count("（") > s.count("）"):
            s = s[:s.rfind("（")] or s
    s = s.strip() or t
    return s if len(s) <= HEAD_MAX else s[:HEAD_MAX] + "…"


def folded(m: dict) -> bool:
    """シスプロへ頭だけ積むか。★で始まる記憶は、たたんであっても全文を積む。"""
    return bool(m.get("fold")) and not str(m.get("text", "")).lstrip().startswith("★")


def _line(m: dict, full: bool) -> str:
    if full or not folded(m):
        return f"- {m['text']}"
    return f"- {head(m['text'])}{FOLD_MARK}"


_FOLD_NOTE = (f"（「{FOLD_MARK}」の付いた行は、記憶の頭だけです。その話になったら、答える前に "
              "recall_memories にその頭の言葉を渡して全文を開き、全文を見てから答えてください。"
              "書き換える・忘れる前も、先に全文を開いて番号を確かめてください。）")


def trash_listing(pid: str) -> list[dict]:
    """ゴミ箱の中身（新しい順）。シスプロには積まれない。"""
    out = []
    for p in _trash_dir(pid).glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def restore(pid: str, mid: str) -> dict | None:
    """ゴミ箱から戻す。"""
    t = _trash_dir(pid) / f"{mid}.json"
    if not t.exists():
        return None
    t.rename(_path(pid, mid))
    return json.loads(_path(pid, mid).read_text(encoding="utf-8"))


def find(pid: str, needle: str) -> list[dict]:
    """文の一部で探す。id を取り違えても目当てに辿り着けるようにする道。

    ★空白と全角半角のゆれを落として突き合わせる。持ち主の書き方と
      AI の書き方で空白の入り方が違っても当たるようにするため。
    """
    key = _norm(needle)
    if not key:
        return []
    # ★たたんだ記憶の頭は、文の頭そのもの（「…」で切ったぶんだけ短い）。シスプロの行をそのまま
    #   写して渡されても当たるよう、「（たたみ）」・行頭の「-」・囲みのカギ括弧・終わりの「…」を落として
    #   から突き合わせる（2026-10-14）。空白は _norm が先に落とすので、間に空白があっても効く。
    key = (key.replace(_norm(FOLD_MARK), "").lstrip("-・「").rstrip("」").rstrip("….")) or key
    return [m for m in listing(pid) if key in _norm(m.get("text", ""))]


def _norm(s: str) -> str:
    return re.sub(r"[\s　]+", "", (s or "")).lower()


def listing(pid: str) -> list[dict]:
    """新しい順。画面の一覧にも、シスプロへの差し込みにも、これを使う。"""
    out = []
    for p in _dir(pid).glob("*.json"):        # _trash/ の中は拾わない
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def block(pid: str) -> str:
    """シスプロの後ろに差し込む文。1件も無ければ空文字（＝何も積まない）。"""
    items = listing(pid)[:MAX_INJECT]
    if not items:
        return ""
    lines = "\n".join(_line(m, False) for m in reversed(items))   # 古い順に並べ直す
    note = f"\n{_FOLD_NOTE}" if any(folded(m) for m in items) else ""
    return f"おぼえていること：{note}\n{lines}"


def block_shared(full: bool = False) -> str:
    """みんな共通の袋を、シスプロの後ろに差し込む文（2026-09-02）。1件も無ければ空文字。

    ★1件も無いうちは空文字＝**いまの入力の重さは1トークンも変わらない**。
      共通の袋に何か書かれた日から、その分だけ伸びる。
    ★自分の袋とは見出しを分ける。混ぜてしまうと、どちらの袋へ書き足せばよいのか
      AI 側で見分けられなくなるため。
    ★full=True … recall_memories を持たないプロジェクト（「おぼえない」設定）用。
      たたんだ記憶を開く道具が無いので、全文を積む（2026-10-14）。
    """
    items = listing(SHARED)[:MAX_INJECT_SHARED]
    if not items:
        return ""
    lines = "\n".join(_line(m, full) for m in reversed(items))
    note = f"\n{_FOLD_NOTE}" if not full and any(folded(m) for m in items) else ""
    return ("みんな共通のおぼえていること"
            "（どのプロジェクトからも読めて、どこからでも書き足せる袋です）：" + note + "\n" + lines)
