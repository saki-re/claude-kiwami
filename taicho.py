# -*- coding: utf-8 -*-
"""クロード極 — 体調の記録（AI が使う道具・2026-09-19 新設）

持病のある人が、体調の悪い日を日ごとに記録して数えるための道具。
設計の骨子：
   1. カレンダーは付ける。ただしベクトルにしない（日付をキーにした表・項目は4つだけ）
   2. 会話ログから後で拾わず、AI がその場で書く（記録に作り話を入れない）
   3. 道具は3本 ── 書く／読む（期間の一覧）／数える（期間内の悪い日の数）
   書き忘れ対策は二段 ──「記録して」で必ず書く／体調の話が出たら自分から書いて「書きました」

★おぼえていること（memory.py）とは別の入れ物。シスプロには1文字も積まない
  ＝道具で読みに来たときだけ中身が動く。件数が増えても入力は太らない。
★置き場は data/taicho.json の1ファイル。日付（YYYY-MM-DD）が鍵で、1日＝1行。
  同じ日にもう一度書くと、その日の行が書き換わる＝悪い日が二重に数えられない。
  data/ は git に載らない（体のことを GitHub へ出さない）。バックアップは別途取ること。
★読めないファイルの上には書かない。壊れた JSON を空として扱って上書きすると、
  それまでの記録が消える。書く前の中身は taicho.json.bak に1世代だけ残す。

★2026-10-01 から、ふだんは夜の係（taicho_writer.py・毎朝4時・Sonnet 5.5）が会話から拾って書く。
  手で書く形では、9/19〜10/1 のあいだに1日ぶんしか入っていなかったため、夜に自動で拾う形（Sonnet 5.5）にした。
  ・AI が書くのは「記録して」と頼まれたときだけ（道具の説明から「頼まれなくても書く」を外した）。
  ・どちらが書いた項目かを行の moto に残す（"te"＝AI が道具で／"yoru"＝夜の係）。
    moto の無い古い行は、全部 "te" として扱う。
  ・**夜の係は、手で書いた項目を1文字も変えない**（write_yoru）。夜が書いた項目にだけ書き足す。
  ・拾う根拠は、持ち主の言葉だけ（記録に作り話を入れない、は前のまま）。
"""
import calendar
import json
import os
import shutil
from datetime import date, datetime

from claude_agent_sdk import tool

import config

# kiwami_memory の MCP サーバーに載る名前（agent_bridge が allowed_tools へ入れる）
KAKU = "mcp__kiwami_memory__taicho_kaku"
YOMU = "mcp__kiwami_memory__taicho_yomu"
KAZOERU = "mcp__kiwami_memory__taicho_kazoeru"
TOOL_NAMES = [KAKU, YOMU, KAZOERU]

_WEEK = "月火水木金土日"


class Unreadable(Exception):
    """記録のファイルが読めない（壊れている）。この上には書かない。"""


def _path():
    # ★毎回 config.DATA_DIR から引く。確かめ役が一時フォルダへ向け替えたとき、そちらに書くため。
    return config.DATA_DIR / "taicho.json"


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise Unreadable(f"{type(e).__name__}: {e}")
    if not isinstance(d, dict):
        raise Unreadable("中身が表の形ではありません")
    return d


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".bak"))      # 書く前の中身を1世代だけ残す
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(dict(sorted(d.items())), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)                                     # 書きかけのファイルを残さない


# ── 形をそろえる ────────────────────────────────────────
def _day(s: str) -> date | None:
    """YYYY-MM-DD（/ 区切りも可）を日付にする。空なら今日。読めなければ None。"""
    s = (s or "").strip().replace("/", "-")
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _warui(s) -> tuple[bool, bool | None]:
    """(読めたか, 値)。値は True＝悪い日／False＝悪くない日／None＝空（渡されていない）。"""
    if isinstance(s, bool):
        return True, s
    s = str(s or "").strip().lower()
    if not s:
        return True, None
    if s in ("はい", "悪い", "悪い日", "yes", "true", "1"):
        return True, True
    if s in ("いいえ", "悪くない", "no", "false", "0"):
        return True, False
    return False, None


def _span(kara: str, made: str) -> tuple[date, date] | str:
    """期間を (はじめ, おわり) にする。読めなければ断り文を返す。

      両方空          … 今月
      kara が YYYY-MM … その月ぜんぶ（made は見ない）
      made だけ空     … kara から今日まで
      kara だけ空     … made の月の1日から made まで
    """
    kara = (kara or "").strip().replace("/", "-")
    made = (made or "").strip().replace("/", "-")
    today = date.today()

    def _month(y: int, m: int) -> tuple[date, date]:
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])

    if not kara and not made:
        return _month(today.year, today.month)
    if len(kara) == 7 and not made:
        try:
            y, m = kara.split("-")
            return _month(int(y), int(m))
        except ValueError:
            return f"期間「{kara}」が読めませんでした。YYYY-MM か YYYY-MM-DD で渡してください。"
    a = _day(kara) if kara else None
    b = _day(made) if made else None
    if (kara and a is None) or (made and b is None):
        return "期間が読めませんでした。kara・made は YYYY-MM-DD で渡してください。"
    if a is None:
        a = b.replace(day=1)
    if b is None:
        b = max(a, today)
    if a > b:
        a, b = b, a
    return a, b


def _label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{iso}（{_WEEK[d.weekday()]}）"


def _warui_word(v) -> str:
    return "はい" if v is True else "いいえ" if v is False else "まだ決めていない"


def _line(iso: str, row: dict) -> str:
    yoru = [k for k, v in (row.get("moto") or {}).items() if v == "yoru"]
    tail = "（夜に会話から拾った項目あり）" if yoru else ""
    return (f"{_label(iso)} 悪い日：{_warui_word(row.get('warui'))}"
            f"／やったこと：{row.get('yatta') or '（なし）'}"
            f"／代償：{row.get('daisho') or '（なし）'}{tail}")


def _in_span(d: dict, a: date, b: date) -> list[tuple[str, dict]]:
    lo, hi = a.isoformat(), b.isoformat()
    return [(k, v) for k, v in sorted(d.items()) if lo <= k <= hi and isinstance(v, dict)]


# ── 素の関数（道具からも、確かめ役からも呼ぶ）──────────────────
def write(hizuke: str, yatta: str, daisho: str, warui) -> str:
    d0 = _day(hizuke)
    if d0 is None:
        return f"日付「{hizuke}」が読めませんでした。YYYY-MM-DD で渡してください（今日なら空）。書いていません。"
    ok, w = _warui(warui)
    if not ok:
        return f"悪い日か「{warui}」が読めませんでした。「はい」「いいえ」か空で渡してください。書いていません。"
    yatta = (yatta or "").strip()
    daisho = (daisho or "").strip()
    if not yatta and not daisho and w is None:
        return "中身が空なので書いていません。"

    try:
        d = _load()
    except Unreadable as e:
        print(f"[taicho] 記録のファイルが読めません。上書きしません: {e}")
        return "記録のファイルが読めないので、書いていません（それまでの記録を守るため）。持ち主にお伝えしてください。"

    iso = d0.isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    before = dict(d.get(iso) or {})
    row = dict(before)
    moto = _moto_of(before)
    if yatta:
        row["yatta"] = yatta
        moto["yatta"] = "te"
    if daisho:
        row["daisho"] = daisho
        moto["daisho"] = "te"
    if w is not None:
        row["warui"] = w
        moto["warui"] = "te"
    row["moto"] = moto
    row.setdefault("yatta", "")
    row.setdefault("daisho", "")
    row.setdefault("warui", None)
    row.setdefault("kaita", now)          # はじめて書いた時刻（その場で書いた裏付け）
    row["naoshita"] = now                 # 最後に書いた時刻
    d[iso] = row
    _save(d)

    nl = chr(10)
    if before:
        return (f"書き換えました。{nl}前：{_line(iso, before)}{nl}後：{_line(iso, row)}")
    return f"書きました：{_line(iso, row)}"


def _moto_of(row: dict) -> dict:
    """項目ごとの書き手（"te"＝AI が道具で／"yoru"＝夜の係）。
    moto の無い古い行は、中身のある項目を全部 "te" として扱う（夜の係に上書きさせない）。"""
    moto = dict(row.get("moto") or {})
    for k in ("yatta", "daisho", "warui"):
        v = row.get(k)
        if k not in moto and v not in (None, ""):
            moto[k] = "te"
    return moto


def write_yoru(hizuke: str, yatta: str, daisho: str, warui) -> str:
    """夜の係（taicho_writer.py）が会話から拾った1日ぶんを足す。何も変えなければ "" を返す。

    ★手で書いた項目（moto が "te"）は1文字も変えない。
    ★夜が書いた項目は、新しい中身が前の中身に入っていなければ「／」でつなぐ（前の夜の分を消さない）。
    ★悪い日かは、まだ決まっていない（None）ときだけ入れる。
    """
    d0 = _day(hizuke)
    if d0 is None or not (hizuke or "").strip():
        return ""
    ok, w = _warui(warui)
    if not ok:
        w = None
    yatta = (yatta or "").strip()
    daisho = (daisho or "").strip()
    if not yatta and not daisho and w is None:
        return ""
    try:
        d = _load()
    except Unreadable as e:
        print(f"[taicho] 記録のファイルが読めません。夜の分は書きません: {e}")
        return ""

    iso = d0.isoformat()
    before = dict(d.get(iso) or {})
    row = dict(before)
    moto = _moto_of(before)
    changed = False
    for key, new in (("yatta", yatta), ("daisho", daisho)):
        if not new or moto.get(key) == "te":
            continue
        old = (row.get(key) or "").strip()
        if new in old:
            continue
        row[key] = f"{old}／{new}" if old else new
        moto[key] = "yoru"
        changed = True
    if w is not None and row.get("warui") is None and moto.get("warui") != "te":
        row["warui"] = w
        moto["warui"] = "yoru"
        changed = True
    if not changed:
        return ""

    now = datetime.now().isoformat(timespec="seconds")
    row["moto"] = moto
    row.setdefault("yatta", "")
    row.setdefault("daisho", "")
    row.setdefault("warui", None)
    row.setdefault("kaita", now)
    row["naoshita"] = now
    d[iso] = row
    _save(d)
    return f"夜に書きました：{_line(iso, row)}"


def read(kara: str, made: str) -> str:
    span = _span(kara, made)
    if isinstance(span, str):
        return span
    a, b = span
    try:
        rows = _in_span(_load(), a, b)
    except Unreadable as e:
        return f"記録のファイルが読めませんでした（{e}）。持ち主にお伝えしてください。"
    head = f"{a.isoformat()}〜{b.isoformat()} の体調の記録"
    if not rows:
        return f"{head}：1日もありません。"
    nl = chr(10)
    return nl.join([f"{head}（{len(rows)}日）："] + [_line(k, v) for k, v in rows])


def count(kara: str, made: str) -> str:
    span = _span(kara, made)
    if isinstance(span, str):
        return span
    a, b = span
    try:
        rows = _in_span(_load(), a, b)
    except Unreadable as e:
        return f"記録のファイルが読めませんでした（{e}）。持ち主にお伝えしてください。"

    # 「記録なし」は今日までの日だけ数える（まだ来ていない日は、書けないので数えない）
    last = min(b, date.today())
    days = (last - a).days + 1 if last >= a else 0

    def _tally(items):
        bad = [k for k, v in items if v.get("warui") is True]
        good = sum(1 for _, v in items if v.get("warui") is False)
        undecided = sum(1 for _, v in items if v.get("warui") is None)
        return bad, good, undecided

    bad, good, undecided = _tally(rows)
    nl = chr(10)
    out = [f"{a.isoformat()}〜{b.isoformat()}",
           f"悪い日 {len(bad)}日／悪くない日 {good}日／まだ決めていない {undecided}日"
           f"／記録なし {max(days - len(rows), 0)}日（今日までの {days}日のうち、記録のある日 {len(rows)}日）"]
    out.append("悪い日：" + ("、".join(_label(k) for k in bad) if bad else "なし"))

    # 月ごとの内訳（「月に何日」の集計用）。1か月の中なら上と同じなので出さない
    if a.strftime("%Y-%m") != b.strftime("%Y-%m"):
        out.append("月ごと：")
        m = date(a.year, a.month, 1)
        while m <= b:
            key = m.strftime("%Y-%m")
            items = [(k, v) for k, v in rows if k[:7] == key]
            mb, _, _ = _tally(items)
            out.append(f"  {key} 悪い日 {len(mb)}日（記録のある日 {len(items)}日）")
            m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return nl.join(out)


def event_text(args: dict) -> str:
    """画面の会話の中に残す1行（書いた中身を、持ち主がその場で確かめられるように）。"""
    args = args or {}
    ok, w = _warui(args.get("warui"))
    parts = [(args.get("hizuke") or "今日").strip()]
    if ok and w is not None:
        parts.append("悪い日" if w else "悪くない日")
    for k in ("yatta", "daisho"):
        v = str(args.get(k) or "").strip()
        if v:
            parts.append(v)
    return "／".join(parts)


# ── 道具の説明（AI が読む）──────────────────────────────
_KAKU_DESC = (
    "持ち主の体調の記録に、1日ぶんを書く。"
    "あとで見返す大事な記録なので、持ち主が言ったことだけを書く。言っていないこと・推し量ったことは書かない。"
    "★持ち主が「記録して」「書いておいて」「直して」と言ったときだけ、これで書く。"
    "ふだんの体調の話は、毎朝4時に夜の係が会話から拾って書くので、頼まれていないのに自分から書かない。"
    "hizuke … その日の日付（YYYY-MM-DD）。今日なら空でよい。翌朝の状態は、前の日（やったことの日）の行に書く。"
    "yatta … その日にやったこと。daisho … その代償（夜の痛み・翌朝の状態など）。"
    "warui … 悪い日なら「はい」、悪くない日なら「いいえ」。持ち主の言葉から決められなければ空にして、持ち主に尋ねる。"
    "その日の行がもうあれば、空で渡した項目は前のまま残り、中身を渡した項目だけが書き換わる（1日＝1行なので二重には数えられない）。"
    "書き足すときは、前の中身に足した全文を渡す（前の中身は返事に出る。taicho_yomu でも見られる）。"
)

_YOMU_DESC = (
    "持ち主の体調の記録を、期間を決めて一覧で見る。"
    "kara・made は YYYY-MM-DD。kara に YYYY-MM を渡して made を空にすると、その月ぜんぶ。両方空なら今月。"
    "前の中身を確かめてから書き足すときにも使う。"
)

_KAZOERU_DESC = (
    "持ち主の体調の記録から、期間内の悪い日を数える（「月に何日」の集計用）。"
    "kara・made は taicho_yomu と同じ。悪い日の数と日付、悪くない日・まだ決めていない日・記録のない日の数、"
    "期間が月をまたぐときは月ごとの内訳が返る。"
    "★悪い日の数を答えるときは、会話や記憶から数えず、必ずこれで数える。"
)


def _txt(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def tools() -> list:
    """kiwami_memory のサーバーに足す3本。agent_bridge._memory_server_for が呼ぶ。"""

    @tool("taicho_kaku", _KAKU_DESC,
          {"hizuke": str, "yatta": str, "daisho": str, "warui": str})
    async def _kaku(args):
        return _txt(write(args.get("hizuke", ""), args.get("yatta", ""),
                          args.get("daisho", ""), args.get("warui", "")))

    @tool("taicho_yomu", _YOMU_DESC, {"kara": str, "made": str})
    async def _yomu(args):
        return _txt(read(args.get("kara", ""), args.get("made", "")))

    @tool("taicho_kazoeru", _KAZOERU_DESC, {"kara": str, "made": str})
    async def _kazoeru(args):
        return _txt(count(args.get("kara", ""), args.get("made", "")))

    return [_kaku, _yomu, _kazoeru]
