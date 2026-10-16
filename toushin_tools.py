# -*- coding: utf-8 -*-
"""Claude のアプリ — 投資信託ランキングの道具（2026-10-01 新設）

Claude のチャットで「♡のファンド、今月どう？」「この荒さ33%ってどのくらい？」と聞くと、
  投資信託ランキングの数字を読んで答える。

★数字は**このパソコンで動いている投資信託ランキング（toushin/・8050）に、HTTP の GET で聞くだけ**です。
  ・聞くのは /api/latest と /api/fund/{8桁の番号} の2つだけ。POST はしません。
  ・行き先は 127.0.0.1 だけ（TOUSHIN_URL を変えても、127.0.0.1 以外は使いません）。
  ・取り込みは 8050 の画面の仕事です（この道具は 8050 に聞くだけ）。
  ・8050 に届かないときは、例外を投げずに「届きませんでした」と文で返します。
★スイッチが入のときだけ、この道具が載ります（ほかの道具のスイッチとまったく同じ形）。
  切のあいだは1本も載らないので、ふだんの会話の重さは1トークンも変わりません。
★生成AIは呼びません＝0円・すぐ返る。
★助言はしません。読み上げるのは過去の数字だけで、「買え」「売れ」とは書きません。
"""
import asyncio
import json
import math
import os
import re
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlparse

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    AVAILABLE = True
except Exception as _e:                       # 部品が無くても会話は止めない
    AVAILABLE = False
    print(f"[toushin] 投資信託ランキングの道具は使えません: {type(_e).__name__}: {_e}")

SERVER_NAME = "toushin"
_N_HOSHI, _N_SAGASU, _N_KUWASHIKU, _N_UGOKI = "toushin_hoshi", "toushin_sagasu", "toushin_kuwashiku", "toushin_ugoki"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{n}" for n in (_N_HOSHI, _N_SAGASU, _N_KUWASHIKU, _N_UGOKI)]
TOOL_LABELS = {
    f"mcp__{SERVER_NAME}__{_N_HOSHI}": "♡の投資信託を読んでいます",
    f"mcp__{SERVER_NAME}__{_N_SAGASU}": "投資信託を探しています",
    f"mcp__{SERVER_NAME}__{_N_KUWASHIKU}": "投資信託をくわしく読んでいます",
    f"mcp__{SERVER_NAME}__{_N_UGOKI}": "今月の順位の動きを読んでいます",
}
TOOL_FIELDS = {}
KEEP_IN_CHAT = frozenset()

DEFAULT_URL = "http://127.0.0.1:8050"
TIMEOUT_SEC = 5
MAX_BYTES = 30_000_000
ID_RE = re.compile(r"[0-9A-Za-z]{8}")
TERMS = (("3", "3ヶ月"), ("6", "6ヶ月"), ("12", "1年"), ("36", "3年"))
TLABEL = dict(TERMS)
TLABEL["60"] = "5年"
SAGASU_MAX = 8

FOOT = "過去の数字です。この先を約束するものではありません。"
FOOT_ROUGH = ("荒さ＝1年でどのくらい上下にぶれたか（年率の標準偏差）。10%未満おだやか・20%未満ふつう・35%未満荒め・"
              "それ以上とても荒い（債券は数%・世界の株は15〜20%・2倍型は40%以上のことが多い）。"
              "効率＝荒さの割の増え方（大きいほど、ぶれの割によく増えた）。")


# ── 8050 に聞く（GET だけ・127.0.0.1 だけ・外へは行かない） ──────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """よそへ飛ばされても付いて行かない（行き先は 127.0.0.1 のまま）。"""
    def redirect_request(self, *a, **k):
        return None


# 環境のプロキシ設定は使わない（127.0.0.1 へ行く道に、よその中継を挟まない）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
_warned = set()


def _base() -> str:
    """聞き先。TOUSHIN_URL で変えられるが、127.0.0.1 の http 以外は採らず、既定（8050）に戻す。"""
    raw = (os.environ.get("TOUSHIN_URL") or DEFAULT_URL).strip().rstrip("/")
    try:
        u = urlparse(raw)
        if (u.scheme == "http" and u.hostname == "127.0.0.1" and not u.username and not u.password
                and u.path in ("", "/") and not u.query and not u.fragment):
            return f"http://127.0.0.1:{u.port or 80}"
    except Exception:                          # noqa: BLE001 — ポート番号が変など
        pass
    if raw not in _warned:
        _warned.add(raw)
        print(f"[toushin] TOUSHIN_URL が 127.0.0.1 の http ではないので使いません（{raw}）。{DEFAULT_URL} に聞きます。")
    return DEFAULT_URL


def _port() -> int:
    try:
        return urlparse(_base()).port or 80
    except Exception:                          # noqa: BLE001
        return 8050


def _get(path: str):
    """GET して JSON を返す。(中身, None) か (None, 日本語の文)。例外は外へ出さない。"""
    url = _base() + path
    try:
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        with _OPENER.open(req, timeout=TIMEOUT_SEC) as r:
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return None, f"投資信託ランキング（{_port()}）が、ほかの住所へ案内してきたので読みませんでした。"
        return None, (f"投資信託ランキング（{_port()}）から、うまく答えが返りませんでした（HTTP {e.code}）。"
                      "数字は作らず、読めなかったとお伝えする。")
    except Exception:                          # noqa: BLE001 — 届かない・時間切れなど
        return None, (f"投資信託ランキング（{_port()}）に届きませんでした。"
                      "いまは数字を読めないので、数字は作らず、届かなかったとお伝えする。")
    if len(raw) > MAX_BYTES:
        return None, f"投資信託ランキング（{_port()}）の答えが大きすぎて読めませんでした。"
    try:
        return json.loads(raw.decode("utf-8-sig")), None
    except Exception:                          # noqa: BLE001
        return None, f"投資信託ランキング（{_port()}）の答えが読めませんでした。数字は作らず、読めなかったとお伝えする。"


def _latest():
    """/api/latest を取る。(d, None) か (None, 文)。ファンドが1本も無いときも文で返す。"""
    d, err = _get("/api/latest")
    if err:
        return None, err
    if not isinstance(d, dict) or not isinstance(d.get("funds"), list):
        return None, "投資信託ランキングの答えの形が、思っていたものと違いました。"
    if not d.get("funds") and not d.get("period"):
        return None, "投資信託ランキングには、まだ取り込んだ成績がありません（みんかぶで「取り込む」を押すと入ります）。"
    return d, None


# ── 文にする小さな部品 ──────────────────────────────────────────
def _has(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pct(v, nd: int = 1) -> str:
    return f"{v:+.{nd}f}%" if _has(v) else "—"


def _yen(v) -> str:
    return f"{v:,.0f}円" if _has(v) else "—"


def _norm(s) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).lower()


def _word(s: float) -> str:
    return "おだやか" if s < 10 else "ふつう" if s < 20 else "荒め" if s < 35 else "とても荒い"


def _rough(f: dict, k: str):
    """荒さ（年率の標準偏差 %）。(値, "本物"|"目安") か None。画面（toushin/index.html）と同じ決め方：
    risk.s[期間] があればそれ（本物）／無ければ r[期間] ÷ risk.p[期間] を、abs(p) >= 0.3 かつ
    答えが 0 より大きく 200 以下のときだけ目安として出す。"""
    risk = f.get("risk") if isinstance(f.get("risk"), dict) else {}
    s = (risk.get("s") or {}).get(k)
    if _has(s):
        return float(s), "本物"
    if k in (f.get("pmis") or []):          # 増え方と効率が別の月なら割らない（画面と同じ）
        return None
    p = (risk.get("p") or {}).get(k)
    r = (f.get("r") or {}).get(k)
    if _has(p) and _has(r) and abs(p) >= 0.3:
        v = r / p
        if 0 < v <= 200:
            return float(v), "目安"
    return None


def _rough_txt(f: dict, k: str):
    got = _rough(f, k)
    if not got:
        return None
    v, kind = got
    if kind == "本物":
        return f"{v:.0f}%（{_word(v)}・本物の値）"
    return f"約{v:.0f}%（{_word(v)}・目安：リターン÷効率からの逆算で、数%ずれる）"


def _eff_txt(f: dict, k: str, rank: bool = False):
    risk = f.get("risk") if isinstance(f.get("risk"), dict) else {}
    p = (risk.get("p") or {}).get(k)
    if not _has(p):
        return None
    pk = (risk.get("pk") or {}).get(k)
    return f"{p:.2f}" + (f"（全ファンド中{int(pk)}位）" if rank and _has(pk) else "")


def _rank_of(f: dict, k: str):
    """その期間の順位（数）。ランキングの順位 t が先、無ければファンドのページの全ファンド中の順位 k。無ければ None。"""
    t = (f.get("t") or {}).get(k)
    if _has(t):
        return int(t)
    kk = (f.get("k") or {}).get(k)
    return int(kk) if _has(kk) else None


def _rank_txt(f: dict, cov: dict, k: str) -> str:
    t = (f.get("t") or {}).get(k)
    mv = (f.get("mv") or {}).get(k)
    mvs = f"（先月比{'↑' if mv > 0 else '↓'}{abs(int(mv))}）" if _has(mv) and mv != 0 else ""
    if _has(t):
        return f"{int(t)}位{mvs}"
    kk = (f.get("k") or {}).get(k)
    if _has(kk):
        return f"全ファンド中{int(kk)}位"
    upto = ((cov or {}).get(k) or {}).get("upto") or 0
    return "圏外" if upto else "—"


def _rank_line(f: dict, cov: dict, keys=("3", "6", "12", "36")) -> str:
    return "／".join(f"{TLABEL[k]} {_rank_txt(f, cov, k)}" for k in keys)


def _sort_key(f: dict):
    r = _rank_of(f, "12")
    return (r is None, r if r is not None else 0, str(f.get("name") or ""))


def _nisa_txt(f: dict) -> str:
    n = f.get("nisa")
    if n is None:
        return "NISA不明（ファンドのページが未取り込み）"
    if not n:
        return "NISA対象外"
    return "・".join(str(x) for x in n)


def _label(f: dict) -> str:
    co = f.get("co")
    return f"{f.get('name') or '（名前なし）'}" + (f"（{co}）" if co else "") + f"［{f.get('id')}］"


def _hold_txt(f: dict):
    h = f.get("hold")
    if not isinstance(h, dict) or not _has(h.get("nav")) or not h["nav"]:
        return None
    parts = [f"買った値段 {_yen(h['nav'])}"]
    if h.get("day"):
        parts.append(str(h["day"]))
    now = f.get("nav")
    head = "買った値段はあるが、いまの値段が無い"
    if _has(now):
        gain = (now / h["nav"] - 1) * 100
        head = f"買ってから {_pct(gain)}"
        if _has(h.get("yen")) and h["yen"]:
            parts.append(f"買った金額 {_yen(h['yen'])}→いま約{_yen(h['yen'] * now / h['nav'])}・分配金は入らない")
    return head + "（" + "・".join(parts) + "）"


def _navm_pairs(f: dict) -> list:
    out = []
    for x in f.get("navm") or []:
        if isinstance(x, (list, tuple)) and len(x) == 2 and _has(x[1]) and x[0]:
            out.append((str(x[0]), float(x[1])))
    return out


def _navm_last(f: dict):
    ps = _navm_pairs(f)
    if len(ps) < 2 or not ps[-2][1]:
        return None
    (d0, v0), (d1, v1) = ps[-2], ps[-1]
    try:                                    # 2つの値段の日が20日より近いとき（月末と月初め）は、月ごとと言えないので出さない
        from datetime import date as _d
        if (_d.fromisoformat(d1[:10]) - _d.fromisoformat(d0[:10])).days < 20:
            return None
    except ValueError:
        return None
    return f"月ごとの値段：{d0[:10]} → {d1[:10]} で {_pct((v1 / v0 - 1) * 100)}"


def _streak_txt(f: dict):
    bits = []
    for k, label in TERMS:
        n = (f.get("stay") or {}).get(k)
        if _has(n) and n >= 3:              # 常連は3か月から（画面と同じ）
            bits.append(f"常連：{label}のランキングに{int(n)}か月続けて入っている")
    for k, label in TERMS:
        n = (f.get("down") or {}).get(k)
        if _has(n):
            bits.append(f"続けて下がっている：{label}のランキングで{int(n)}か月続けて順位が下がっている")
    return "／".join(bits) if bits else None


def _compact(i: int, f: dict, cov: dict) -> list:
    out = [f"{i}. {_label(f)}", f"   順位：{_rank_line(f, cov)}"]
    row = []
    r12 = (f.get("r") or {}).get("12")
    row.append(f"1年 {_pct(r12)}")
    rt = _rough_txt(f, "12")
    row.append(f"荒さ（1年）{rt}" if rt else "荒さ（1年）—")
    ef = _eff_txt(f, "12")
    row.append(f"効率 {ef}" if ef else "効率 —")
    out.append("   " + "／".join(row))
    row = [f"信託報酬 {f['fee']}%" if _has(f.get("fee")) else "信託報酬 —", _nisa_txt(f)]
    if f.get("lev"):
        row.append("2倍・3倍型")
    out.append("   " + "／".join(row))
    for extra in (_hold_txt(f), _navm_last(f), _streak_txt(f)):
        if extra:
            out.append("   " + extra)
    return out


def _find_matches(funds: list, kotoba: str) -> list:
    """名前と運用会社に、kotoba（空白で区切ると全部を含む）を含むファンド。NFKC にそろえて小文字で比べる。"""
    words = [w for w in _norm(kotoba).split() if w]
    if not words:
        return []
    hit = []
    for f in funds:
        if not isinstance(f, dict):
            continue
        hay = _norm(f.get("name")) + " " + _norm(f.get("co"))
        if all(w in hay for w in words):
            hit.append(f)
    return sorted(hit, key=_sort_key)


# ── 道具の本体（同期・0円）──────────────────────────────────────
def hoshi() -> str:
    """♡のファンドの一覧。"""
    d, err = _latest()
    if err:
        return err
    funds = [f for f in d["funds"] if isinstance(f, dict)]
    by_id = {str(f.get("id")): f for f in funds}
    stars = [str(s) for s in (d.get("stars") or [])]
    if not stars:
        return "♡を付けたファンドは、まだありません（投資信託ランキングの画面で、行の♡を押すと入ります）。"
    cov = d.get("coverage") or {}
    out = [f"♡のファンド {len(stars)}本（成績の月 {d.get('period') or '—'}・基準価額の日 {d.get('nav_date') or '—'}）"]
    n = 0
    for sid in stars:
        f = by_id.get(sid)
        if f is None:
            out.append(f"- {sid}：♡は付いているが、いまの一覧にデータが見つかりません")
            continue
        n += 1
        out += _compact(n, f, cov)
    out += ["", FOOT_ROUGH,
            "↑↓＝先月からの順位の上がり下がり／圏外＝取り込んだランキング（上位○位まで）に入っていない／—＝まだ取り込んでいない",
            FOOT]
    return "\n".join(out)


def sagasu(kotoba) -> str:
    """名前・運用会社に kotoba を含むファンドを、1年の順位の良い順に最大8本。"""
    kotoba = str(kotoba or "").strip()
    if not kotoba:
        return "探す言葉（kotoba）を入れる。ファンドの名前か運用会社の一部。"
    d, err = _latest()
    if err:
        return err
    cov = d.get("coverage") or {}
    hits = _find_matches([f for f in d["funds"] if isinstance(f, dict)], kotoba)
    if not hits:
        return (f"「{kotoba}」を名前か運用会社に含むファンドは、取り込んだデータにありませんでした"
                "（ランキングに入ったもの・♡・買った値段のあるものだけが並びます）。")
    out = [f"「{kotoba}」に当たるファンド {len(hits)}本"
           + (f"のうち、1年の順位の良い上位{SAGASU_MAX}本" if len(hits) > SAGASU_MAX else "")
           + f"（成績の月 {d.get('period') or '—'}）"]
    for i, f in enumerate(hits[:SAGASU_MAX], 1):
        rt = _rough_txt(f, "12")
        ef = _eff_txt(f, "12")
        fee = f"信託報酬 {f['fee']}%" if _has(f.get("fee")) else "信託報酬 —"
        out.append(f"{i}. {_label(f)}")
        out.append(f"   順位：{_rank_line(f, cov)}")
        out.append(f"   1年 {_pct((f.get('r') or {}).get('12'))}／荒さ（1年）{rt or '—'}／効率 {ef or '—'}／{fee}／{_nisa_txt(f)}")
    if len(hits) > SAGASU_MAX:
        out.append(f"（ほか{len(hits) - SAGASU_MAX}本。言葉を足して絞るか、番号を指定してくわしく見る）")
    out += ["", FOOT_ROUGH, "圏外＝取り込んだランキング（上位○位まで）に入っていない／—＝まだ取り込んでいない", FOOT]
    return "\n".join(out)


def _find_one(funds: list, key: str):
    """番号（8桁）か名前の一部から1本を選ぶ。(ファンド, None) か (None, 文)。"""
    key = str(key or "").strip()
    if not key:
        return None, "番号（8桁）か名前の一部（bango）を入れる。"
    if ID_RE.fullmatch(key):
        for f in funds:
            if str(f.get("id") or "").upper() == key.upper():
                return f, None
    hits = _find_matches(funds, key)
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, (f"「{key}」に当たるファンドは、取り込んだデータにありませんでした"
                      "（ランキングに入ったもの・♡・買った値段のあるものだけが並びます）。")
    out = [f"「{key}」に当たるファンドが{len(hits)}本あります。番号（8桁）を指定して、もう一度くわしく見る。"]
    for f in hits[:SAGASU_MAX]:
        out.append(f"- {_label(f)}　1年 {_rank_txt(f, {}, '12')}")
    if len(hits) > SAGASU_MAX:
        out.append(f"（ほか{len(hits) - SAGASU_MAX}本）")
    return None, "\n".join(out)


def kuwashiku(bango) -> str:
    """1本をくわしく。"""
    d, err = _latest()
    if err:
        return err
    funds = [f for f in d["funds"] if isinstance(f, dict)]
    f, msg = _find_one(funds, bango)
    if f is None:
        return msg
    cov = d.get("coverage") or {}
    r = f.get("r") or {}
    g = f.get("g") or {}
    out = [f"【{f.get('name') or '（名前なし）'}】［{f.get('id')}］" + (f"　運用会社：{f['co']}" if f.get("co") else ""),
           f"成績の月 {f.get('r_date') or d.get('period') or '—'}・基準価額の日 {f.get('nav_date') or '—'}"
           + ("・2倍・3倍型" if f.get("lev") else "")]
    price = f"基準価額 {_yen(f.get('nav'))}"
    if _has(f.get("nd")):
        price += f"（前日比 {f['nd']:+,.0f}円）"
    out.append(price)
    out.append("順位：" + _rank_line(f, cov))

    def ret(k, label):
        v = r.get(k)
        if _has(v):
            return f"{label} {_pct(v)}"
        gv = g.get(k)
        if _has(gv):
            return f"{label} {_pct(gv)}（貯めた値段からの値上がり・分配金は入らない）"
        return f"{label} —"

    rows = [ret("3", "3ヶ月"), ret("6", "6ヶ月"), f"1年 {_pct(r.get('12'))}", f"3年 {_pct(r.get('36'))}（1年あたり）",
            f"5年 {_pct(r.get('60'))}（1年あたり）"]
    if _has(r.get("1")):
        rows.insert(0, f"1ヶ月 {_pct(r['1'])}")
    out.append("リターン：" + "／".join(rows))
    for k, label in (("12", "1年"), ("36", "3年"), ("60", "5年")):
        rt, ef = _rough_txt(f, k), _eff_txt(f, k, rank=True)
        if rt or ef:
            out.append(f"{label}：荒さ {rt or '—'}／効率 {ef or '—'}")
    if not any(_rough(f, k) or _eff_txt(f, k) for k in ("12", "36", "60")):
        out.append("荒さ・効率：まだ取り込んでいない（みんかぶでこのファンドの「リターン」のページを開いて「取り込む」を押すと出ます）")
    row = [f"信託報酬 {f['fee']}%" if _has(f.get("fee")) else "信託報酬 —", _nisa_txt(f)]
    if f.get("rate"):
        row.append(f"みんかぶのレーティング ★{int(f['rate'])}")
    out.append("／".join(row))
    info = f.get("info")
    if isinstance(info, dict) and info:
        bits = []
        if info.get("rr"):
            bits.append(f"分類：{info['rr']}")
        if info.get("setup"):
            bits.append(f"設定日 {info['setup']}")
        if info.get("until"):
            bits.append("信託期間：無期限" if info["until"] == "none" else f"信託期間：{info['until']} まで")
        if info.get("settle"):
            bits.append(f"決算 {info['settle']}")
        if _has(info.get("buy_fee")):
            bits.append(f"購入時手数料 {info['buy_fee']}%")
        if _has(info.get("dist_year")) or _has(info.get("dist_last")):
            bits.append("分配金：" + "・".join(x for x in (
                f"直近 {_yen(info['dist_last'])}" if _has(info.get("dist_last")) else "",
                f"1年で {_yen(info['dist_year'])}" if _has(info.get("dist_year")) else "") if x))
        if bits:
            out.append("なりたち：" + "／".join(bits))
    else:
        out.append("なりたち：まだ取り込んでいない（取り込みの面の「まとめて取る」＝比較のページを、版6のブックマークで取り込むと出ます）")
    hold = _hold_txt(f)
    if hold:
        out.append(hold)
    ps = _navm_pairs(f)
    if len(ps) >= 2:
        bits = [f"{d1[:7]} {_yen(v1)}（{_pct((v1 / v0 - 1) * 100) if v0 else '—'}）"
                for (d0, v0), (d1, v1) in zip(ps, ps[1:])]
        out.append(f"月ごとの値段：{ps[0][0][:7]} {_yen(ps[0][1])} → " + " → ".join(bits))
    streak = _streak_txt(f)
    if streak:
        out.append(streak)
    hist, herr = _get(f"/api/fund/{f.get('id')}")
    hl = hist.get("history") if isinstance(hist, dict) else None
    if isinstance(hl, list):
        navs = [(str(x.get("date")), x.get("nav")) for x in hl if isinstance(x, dict) and _has(x.get("nav"))][-6:]
        if navs:
            out.append("基準価額の推移（最近）：" + " → ".join(f"{dt} {_yen(v)}" for dt, v in navs))
        for k, label in TERMS:
            pts = [(str(x.get("date")), x.get(f"t{k}")) for x in hl if isinstance(x, dict) and _has(x.get(f"t{k}"))][-6:]
            if len(pts) >= 2:
                out.append(f"{label}の順位の推移：" + " → ".join(f"{dt[:7]} {int(v)}位" for dt, v in pts))
    elif herr:
        out.append("（推移は読めませんでした）")
    out += ["", FOOT_ROUGH, FOOT]
    return "\n".join(out)


_KIKAN_ALIAS = {"3ヶ月": "3", "3か月": "3", "3カ月": "3", "三ヶ月": "3", "6ヶ月": "6", "6か月": "6", "6カ月": "6",
                "1年": "12", "一年": "12", "12ヶ月": "12", "12か月": "12", "3年": "36", "三年": "36",
                "36ヶ月": "36", "36か月": "36"}


def _kikan_key(v):
    s = _norm(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s in ("3", "6", "12", "36"):
        return s
    return _KIKAN_ALIAS.get(s)


def ugoki(kikan=None) -> str:
    """今月の動き。期間を渡さなければ全部。"""
    s = "" if kikan is None else str(kikan).strip()
    if s:
        k = _kikan_key(s)
        if k is None:
            return f"期間（kikan）は \"3\"（3ヶ月）・\"6\"（6ヶ月）・\"12\"（1年）・\"36\"（3年）のどれか。いま「{s}」でした。"
        keys = [k]
    else:
        keys = [k for k, _ in TERMS]
    d, err = _latest()
    if err:
        return err
    funds = [f for f in d["funds"] if isinstance(f, dict)]
    by_id = {str(f.get("id")): f for f in funds}
    moves = d.get("moves") if isinstance(d.get("moves"), dict) else {}
    cap = 20 if len(keys) == 1 else 8

    def nm(fid, fallback=""):
        f = by_id.get(str(fid))
        return (f.get("name") if f else "") or fallback or str(fid)

    out = [f"今月の順位の動き（成績の月 {d.get('period') or '—'}）"]
    for k in keys:
        out.append(f"■ {TLABEL[k]}のランキング")
        m = moves.get(k) if isinstance(moves.get(k), dict) else None
        ups = sorted((f for f in funds if _has((f.get("mv") or {}).get(k)) and f["mv"][k] > 0
                      and _has((f.get("t") or {}).get(k))), key=lambda f: (-f["mv"][k], f["t"][k]))
        downs = sorted((f for f in funds if _has((f.get("mv") or {}).get(k)) and f["mv"][k] < 0
                        and _has((f.get("t") or {}).get(k))), key=lambda f: (f["mv"][k], f["t"][k]))
        if not m and not any(_has((f.get("mv") or {}).get(k)) for f in funds):
            out.append("  比べる前の月がまだありません（月に1回の取り込みが2か月そろうと出ます）")
            continue
        if m:
            out.append(f"  {m.get('prev') or '—'} → {m.get('date') or '—'}（上位{m.get('depth') or '—'}位まで比べました）")
            inn = [x for x in (m.get("in") or []) if isinstance(x, (list, tuple)) and len(x) >= 2]
            gone = [x for x in (m.get("out") or []) if isinstance(x, (list, tuple)) and len(x) >= 3]
            out.append(f"  新しく入った {len(inn)}本" + ("：" if inn else "（なし）"))
            for fid, rk in [(x[0], x[1]) for x in inn[:cap]]:
                out.append(f"    {rk}位 {nm(fid)}")
            if len(inn) > cap:
                out.append(f"    （ほか{len(inn) - cap}本）")
            out.append(f"  外れた {len(gone)}本" + ("：" if gone else "（なし）"))
            for x in gone[:cap]:
                now = x[3] if len(x) > 3 else None
                out.append(f"    前は{x[1]}位 {nm(x[0], x[2])} → いま " + (f"{now}位" if _has(now) else "圏外"))
            if len(gone) > cap:
                out.append(f"    （ほか{len(gone) - cap}本）")
        else:
            out.append("  （新しく入った・外れたの比べは、まだ出せません。順位の上がり下がりだけ出します）")
        out.append(f"  順位が上がった（大きい順に{min(5, len(ups))}本）：" if ups else "  順位が上がったファンド：なし")
        for f in ups[:5]:
            now = int(f["t"][k])
            out.append(f"    {now + int(f['mv'][k])}位 → {now}位（{int(f['mv'][k])}つ上がった）{nm(f['id'])}")
        out.append(f"  順位が下がった（大きい順に{min(5, len(downs))}本）：" if downs else "  順位が下がったファンド：なし")
        for f in downs[:5]:
            now = int(f["t"][k])
            out.append(f"    {now + int(f['mv'][k])}位 → {now}位（{abs(int(f['mv'][k]))}つ下がった）{nm(f['id'])}")
    out += ["", FOOT]
    return "\n".join(out)


# ── Agent SDK に渡す形 ─────────────────────────────────────────
def _txt(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


_EMPTY_SCHEMA = {"type": "object", "properties": {}}
_SAGASU_SCHEMA = {
    "type": "object",
    "properties": {"kotoba": {"type": "string", "description": "ファンドの名前か運用会社の一部（空白で区切ると全部を含むものだけ）"}},
    "required": ["kotoba"],
}
_KUWASHIKU_SCHEMA = {
    "type": "object",
    "properties": {"bango": {"type": "string", "description": "8桁の番号（例：0331418A）、または名前の一部"}},
    "required": ["bango"],
}
_UGOKI_SCHEMA = {
    "type": "object",
    "properties": {"kikan": {"type": "string",
                             "description": "\"3\"・\"6\"・\"12\"・\"36\" のどれか（3ヶ月・6ヶ月・1年・3年）。省略すると全部"}},
}


def build_server():
    """投資信託ランキングの道具を、Agent SDK に渡せる形で返す。使えなければ None。"""
    if not AVAILABLE:
        return None

    async def _run(fn, *a):
        try:
            return _txt(await asyncio.to_thread(fn, *a))
        except Exception as e:                      # noqa: BLE001 — 道具のつまずきで会話を止めない
            return _txt(f"投資信託ランキングを読めませんでした（{type(e).__name__}）。数字は作らず、読めなかったとお伝えする。")

    @tool(_N_HOSHI,
          "投資信託ランキング（持ち主のパソコンの 8050）から、♡を付けたファンドの一覧を読む。0円・すぐ返る。"
          "1本ずつ：名前・期間ごとの順位（3ヶ月・6ヶ月・1年・3年）・1年のリターン・荒さ（値動きの荒さ）・効率・信託報酬・NISA・"
          "買った値段があれば「買ってから +x%」・月ごとの値段の増減・常連（続けてランキングに居る）・続けて下がっている。"
          "★「♡のファンド、今月どう？」「持ってるファンドは？」と聞かれたら、頭で答えず、まずこれを呼ぶ。"
          "荒さが『目安』と書いてあるものは、リターン÷効率からの逆算なので数%ずれる、と添える。"
          "★数字を読んで伝えるだけにする。買う・売る・持ち続けるの助言はしない（決めるのは持ち主）。"
          "8050 に届かないと返ったら、数字を作らず、届かなかったと伝える。",
          _EMPTY_SCHEMA)
    async def _hoshi(args):
        return await _run(hoshi)

    @tool(_N_SAGASU,
          "投資信託ランキングの中から、名前か運用会社に kotoba を含むファンドを探す（最大8本・1年の順位の良い順）。0円。"
          "各行に番号（8桁）・名前・順位・1年のリターン・荒さ・効率・信託報酬・NISA。"
          "kotoba＝文字（例：「全世界」「eMAXIS」「インド」）。全角半角・大文字小文字は気にしなくてよい。"
          "★載っているのは、ランキングに入ったもの・♡・買った値段のあるものだけ（市場の全部ではない）。"
          "助言はしない。数字を読んで伝えるだけ。",
          _SAGASU_SCHEMA)
    async def _sagasu(args):
        return await _run(sagasu, (args or {}).get("kotoba", ""))

    @tool(_N_KUWASHIKU,
          "投資信託ランキングの1本を、くわしく読む。0円。♡の一覧や sagasu の上の全部に加えて、"
          "5年のリターン・荒さ・効率（全ファンド中の順位つき）・なりたち（設定日・信託期間・決算・分類・購入時手数料・分配金）・"
          "3ヶ月と6ヶ月の値上がり・基準価額の推移・順位の推移を返す。"
          "bango＝8桁の番号（例：0331418A）か名前の一部（1本に絞れないときは、候補の番号が返る）。"
          "★「この荒さ33%ってどのくらい？」には、荒さの言葉（10%未満おだやか・20%未満ふつう・35%未満荒め・それ以上とても荒い）と、"
          "債券は数%・世界の株は15〜20%・2倍型は40%以上のことが多い、という目安で答える。"
          "助言はしない。数字を読んで伝えるだけ。",
          _KUWASHIKU_SCHEMA)
    async def _kuwashiku(args):
        return await _run(kuwashiku, (args or {}).get("bango", ""))

    @tool(_N_UGOKI,
          "投資信託ランキングの、今月の動き。0円。ひとつ前の月と比べて、新しく入ったファンド・外れたファンドと、"
          "順位が大きく上がった5本・下がった5本を返す。"
          "kikan＝\"3\"（3ヶ月）・\"6\"（6ヶ月）・\"12\"（1年）・\"36\"（3年）のどれか。省略すると全部の期間。"
          "月に1回の取り込みが2か月そろうまでは、比べる前の月がないと返る（そのときは、そのまま伝える）。"
          "助言はしない。数字を読んで伝えるだけ。",
          _UGOKI_SCHEMA)
    async def _ugoki(args):
        return await _run(ugoki, (args or {}).get("kikan"))

    return create_sdk_mcp_server(name=SERVER_NAME, tools=[_hoshi, _sagasu, _kuwashiku, _ugoki])
