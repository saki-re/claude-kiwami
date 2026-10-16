# -*- coding: utf-8 -*-
"""クロード極 — 老後シミュレーターの道具（2026-09-29 新設）

★計算の中身は1か所だけです。**docs/rougo_keisan.py** をそのまま読み込んで使います。
  画面（rougo.html の window.ROUGO）と1円まで同じ答えになるように作ってあるファイルです。
  ここで計算を書き直してはいけません（書き直すと、画面と極で答えがずれます）。

★画面に入れた数字は iPhone の中（localStorage）にしか無く、ここからは読めません。
  なので、クロードが会話や記憶から数字を入れて呼びます。見本A・見本B のプリセット
  （画面と同じ・架空の数字）から始めて、変える所だけ渡せます。

★スイッチが入のときだけ、この道具が載ります（ほかの道具とまったく同じ形）。
  切のあいだは1本も載らないので、ふだんの会話の重さは1トークンも変わりません。
★生成AIは呼びません＝0円・すぐ返る。
★答えには「入れた条件」を必ず添えます（2026-10-14）。プリセットに入っていない出費を、
  クロードが「入っています」と答えてしまったことがあったため。何を足し引きしたかを答えに出し、
  年金が途中で変わる形（手当が止まるなど）も nenkin_kawaru で入れられるようにした。
"""
import copy
import importlib.util
import json
from pathlib import Path

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    AVAILABLE = True
except Exception as _e:                       # 部品が無くても会話は止めない
    AVAILABLE = False
    print(f"[rougo] 老後シミュレーターの道具は使えません: {type(_e).__name__}: {_e}")

_SEIKAI = Path(__file__).resolve().parent / "docs" / "rougo_keisan.py"
try:
    _spec = importlib.util.spec_from_file_location("rougo_seikai", _SEIKAI)
    G = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(G)
except Exception as _e:
    G = None
    AVAILABLE = False
    print(f"[rougo] 計算のファイルが読めません（{_SEIKAI}）: {type(_e).__name__}: {_e}")

SERVER_NAME = "rougo"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__rougo_keisan"]
TOOL_LABELS = {f"mcp__{SERVER_NAME}__rougo_keisan": "老後シミュレーターで計算しています"}
TOOL_FIELDS = {}
KEEP_IN_CHAT = frozenset()

# プリセット（画面の mihonA／mihonB と同じ数字・架空の見本。rougo_keisan.py の mihon_a／mihon_b と同じ形）
# 道具の側だけ、貯める額の行は5つ目に名前・一時費用の行は1つ目に名前を持つ（rougo_keisan.py へ渡すときに外す）。
PRESETS = {
    "mihon_a": dict(
        name="見本A", age=45, mokuhyo=90, yokin=3000000, nisa=500000, r=0.03, kirikae=None, kirikae_label="一人暮らしになる年齢",
        kirikae_ato="一人暮らしになってから", aite=("親", 28),
        nenkin_direct=70000, kaishi=65, nenkin_kawaru=[],
        tameru=[(45, 200, 20000, True, "NISA（つみたて）"), (45, 200, 10000, False, "普通預金")],
        shishutsu=79000, kawaru=[(65, 81000)],
        ichiji=[("家電（エアコン・洗濯機・冷蔵庫）", "kurikaeshi", 300000, 50, 10, 3), ("給湯器", "1kai", 150000, 60),
                ("家の修理（毎年少しずつ）", "narasu", 2000000, 46, 89)],
        isan=[], isan_kirikae=2000000),
    "mihon_b": dict(
        name="見本B", age=35, mokuhyo=90, yokin=1000000, nisa=0, r=0.03, kirikae=None, kirikae_label="辞める年齢",
        kirikae_ato="辞めてから",
        kaishi=65, nenkin_kawaru=[], noufu_m=60, menjo_m=0, yuuyo_m=0, kousei_m=120, nenshu=3500000, mirai=None,
        tameru=[(35, 200, 20000, False, "貯金")], shishutsu=120000, kawaru=[], ichiji=[], isan=[], isan_kirikae=0),
}
_BLANK = dict(name="", age=None, mokuhyo=90, yokin=0, nisa=0, r=0.03, kirikae=None, kirikae_label="切り替わる年齢",
              nenkin_direct=None, kaishi=65, nenkin_kawaru=[], noufu_m=0, menjo_m=0, yuuyo_m=0, kousei_m=0, nenshu=0,
              mirai=None, tameru=[], shishutsu=0, kawaru=[], ichiji=[], isan=[], isan_kirikae=0)
_TOOL_ONLY = ("name", "kirikae_label", "kirikae_ato", "aite", "isan_kirikae", "mirai")


def _txt(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _num(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"「{name}」は数字で入れてください（いま {v!r}）")
    return v


def _man(v) -> str:
    if v is None:
        return "—"
    return f"{'−' if v < 0 else ''}{round(abs(v) / 10000):,}万円" if abs(v) >= 10000 else f"{round(v):,}円"


def _apply(base: dict, d: dict) -> dict:
    """base に、渡された欄（円・歳）を上書きする。"""
    p = copy.deepcopy(base)
    simple = {"age": "age", "mokuhyo": "mokuhyo", "yokin": "yokin", "nisa": "nisa", "kirikae": "kirikae",
              "nenkin_direct": "nenkin_direct", "nenkin_kaishi": "kaishi", "nenshu": "nenshu",
              "shishutsu": "shishutsu", "isan_kirikae": "isan_kirikae"}
    for k, key in simple.items():
        if k in d:
            p[key] = None if d[k] is None else _num(d[k], k)
    if "name" in d:
        p["name"] = str(d["name"])
    if "rimawari" in d:
        p["r"] = _num(d["rimawari"], "rimawari") / 100
    for k, key in (("noufu_nen", "noufu_m"), ("kousei_nen", "kousei_m"), ("menjo_nen", "menjo_m"), ("yuuyo_nen", "yuuyo_m")):
        if k in d:
            p[key] = round(_num(d[k], k) * 12)
    if "nenkin_jidou" in d and d["nenkin_jidou"]:
        p["nenkin_direct"] = None
    if "mirai" in d:
        p["mirai"] = [(r[0], 59 if r[1] is None else r[1], str(r[2])) for r in d["mirai"]]
    if "tameru" in d:
        p["tameru"] = [(_num(r[0], "tameru の何歳から"), 200 if r[1] is None else _num(r[1], "tameru の何歳まで"),
                        _num(r[2], "tameru の月額"), str(r[3]).lower() == "nisa",
                        str(r[4]) if len(r) >= 5 and r[4] is not None else "")
                       for r in d["tameru"]]
    if "nenkin_kawaru" in d:
        rows = d["nenkin_kawaru"] or []
        if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != 2 for r in rows):
            raise ValueError("「nenkin_kawaru」は [[年齢, 新しい年金の月額], …] の形で入れてください")
        p["nenkin_kawaru"] = sorted((_num(r[0], "nenkin_kawaru の年齢"), _num(r[1], "nenkin_kawaru の月額")) for r in rows)
    if "kawaru" in d:
        p["kawaru"] = [(_num(r[0], "kawaru の年齢"), _num(r[1], "kawaru の月額")) for r in d["kawaru"]]
    if "isan" in d:
        p["isan"] = [(_num(r[0], "isan の年齢"), _num(r[1], "isan の金額")) for r in d["isan"]]
    if "ichiji" in d:
        p["ichiji"] = []
    for r in d.get("ichiji", []) + d.get("ichiji_tasu", []):
        # ["名前", 金額, 年齢] ／ ["名前", 金額, "くり返し", はじめ, 何年ごと, 何回] ／ ["名前", 金額, "ならす", から, まで]
        na = str(r[0])
        if len(r) >= 6 and r[2] == "くり返し":
            p["ichiji"].append((na, "kurikaeshi", _num(r[1], "一時費用"), _num(r[3], "くり返しのはじめ"),
                                _num(r[4], "何年ごと"), _num(r[5], "何回")))
        elif len(r) >= 5 and r[2] == "ならす":
            p["ichiji"].append((na, "narasu", _num(r[1], "一時費用"), _num(r[3], "ならす はじめ"), _num(r[4], "ならす おわり")))
        else:
            p["ichiji"].append((na, "1kai", _num(r[1], "一時費用"), _num(r[2], "一時費用の年齢")))
    if "aite" in d:                                           # [呼び名, 年の差]（親が30歳上なら ["親", 30]）
        a = d["aite"]
        if a is None:
            p["aite"] = None
        elif isinstance(a, list) and len(a) == 2:
            p["aite"] = (str(a[0]), _num(a[1], "aite の年の差"))
        else:
            raise ValueError("「aite」は [呼び名, 年の差] の形で入れてください（例：[\"親\", 30]）")
    return p


def _en(v) -> str:
    return f"{'−' if v < 0 else ''}{round(abs(v)):,}円"


def _ichiji_txt(r) -> str:
    na, kind, g = r[0], r[1], r[2]
    if kind == "kurikaeshi":
        return f"{na} {_en(g)}（{r[3]}歳から{r[4]}年ごとに{r[5]}回）"
    if kind == "narasu":
        return f"{na} {_en(g)}（{r[3]}〜{r[4]}歳にならす）"
    return f"{na} {_en(g)}（{r[3]}歳）"


def _tameru_txt(t) -> str:
    kara, made, g, nisa = t[0], t[1], t[2], t[3]
    na = t[4] if len(t) > 4 and t[4] else ("NISA" if nisa else "預金")
    when = f"{kara}歳から" if made >= 200 else (f"{kara}歳だけ" if made == kara else f"{kara}〜{made}歳")
    return f"{na} {when} 月{_en(g)}（{'NISA' if nisa else '預金'}）"


_MIRAI_TXT = {"harau": "払う（国民年金の保険料 月{hoken:,}円を預金から引く）", "menjo": "免除",
              "yuuyo": "猶予（50歳からは未納）", "minou": "払わない"}


def _jouken(p: dict) -> list:
    """答えに添える「入れた条件」。ここに無いものは、計算に入っていない（2026-10-14）。
    計算に効いていないもの（切り替わる年齢が空のときの貯める額など）は「使われない」と書く。"""
    lab = p.get("kirikae_label") or "切り替わる年齢"
    kk = p.get("kirikae")
    out = [f"・いまの年齢 {p['age']}歳（この年から計算）・いまの預金 {_en(p.get('yokin') or 0)}・いまの NISA {_en(p.get('nisa') or 0)}"]
    if p.get("nenkin_direct") is not None:
        s = f"・年金：月{_en(p['nenkin_direct'])}（{p['kaishi']}歳から）"
    else:
        s = f"・年金：自動計算（{p['kaishi']}歳から受け取る）"
    out.append(s + "".join(f"→{a}歳から月{_en(g)}" for a, g in p.get("nenkin_kawaru", [])))
    if p.get("nenkin_direct") is None:
        y = lambda m: f"{m / 12:g}年"
        out.append(f"・年金の元：納めた{y(p['noufu_m'])}・厚生年金{y(p['kousei_m'])}（平均の年収{_en(p['nenshu'])}・"
                   f"{lab}までの働く年も足す）・免除{y(p['menjo_m'])}・猶予{y(p['yuuyo_m'])}")
        mirai = p["mirai"] if p.get("mirai") is not None else [(kk if kk is not None else p["age"], 59, "harau")]
        out.append(f"・{lab}から60歳まで：" + "／".join(
            f"{a}〜{b}歳 " + _MIRAI_TXT.get(k, k).format(hoken=G.HOKEN) for a, b, k in mirai))
    out.append(f"・月の支出：{_en(p['shishutsu'])}"
               + ("（" + "・".join(f"{a}歳から{_en(g)}" for a, g in p["kawaru"]) + "）" if p.get("kawaru") else ""))
    if kk is None:
        out.append(f"・貯める額：使われない（{lab}が空なので、いまの年から年金−月の支出で計算する）")
    else:
        out.append(f"・貯める額（{lab}の前の年まで）：" + ("／".join(_tameru_txt(t) for t in p["tameru"]) or "なし"))
    out.append("・一時費用：" + ("／".join(_ichiji_txt(r) for r in p["ichiji"]) or "なし") + "（ここに無いものは入っていない）")
    if p.get("aite"):
        out.append(f"・{p['aite'][0]}の歳：{lab}＋{p['aite'][1]:g}歳")
    isan = ([f"{_en(p['isan_kirikae'])}（{lab}の年）" if kk is not None else f"{_en(p['isan_kirikae'])}（{lab}が空なので入らない）"]
            if p.get("isan_kirikae") else []) + [f"{_en(g)}（{a}歳）" for a, g in p.get("isan", [])]
    out.append("・遺産：" + ("／".join(isan) or "なし"))
    if kk is None:
        out.append("・しくみ：いまの年から、年金−月の支出で増え減りする。一時費用と遺産は、その年齢の年に引く・足す。")
    else:
        out.append(f"・しくみ：{lab}の前の年までは、貯める額だけ足す（年金と支出は見ない）。{lab}の年からは、"
                   "年金−月の支出で増え減りする。一時費用と遺産は、その年齢の年に引く・足す。")
    return out


def _seikai_form(p: dict) -> dict:
    """rougo_keisan.py の形にする（名前を外す・遺産の「切り替わる年齢と同じ年」と、これからの国民年金の既定を解く）。"""
    q = {k: v for k, v in p.items() if k not in _TOOL_ONLY}
    q["tameru"] = [tuple(t[:4]) for t in p["tameru"]]
    q["ichiji"] = [tuple(r[1:]) for r in p["ichiji"]]
    isan = list(p["isan"])
    if p.get("isan_kirikae") and p.get("kirikae") is not None:
        isan.append((p["kirikae"], p["isan_kirikae"]))
    q["isan"] = isan
    if p.get("nenkin_direct") is None:
        k = p["kirikae"] if p.get("kirikae") is not None else p["age"]
        q["mirai"] = p["mirai"] if p.get("mirai") is not None else [(k, 59, "harau")]
        q.pop("nenkin_direct", None)
    return q


def _aite(p: dict, k) -> str:
    """「（親80歳）」。一緒に住む人の年の差が無ければ空。"""
    a = p.get("aite")
    return f"（{a[0]}{k + a[1]:g}歳）" if a and k is not None else ""


def _ga(p: dict) -> str:
    return {"一人暮らしになる年齢": "一人暮らしになるのが", "辞める年齢": "辞めるのが"}.get(p.get("kirikae_label"), "切り替わるのが")


def _sa(q0: dict) -> int:
    """いまの見込みの支出（年齢で変わる分も含む）に、毎月いくら足しても目標の年齢まで足りるか（100円きざみ・
    マイナスなら、その額を減らすと足りる）。支出を0円にしても足りないときは呼ばない（tsuki_ikura が None）。"""
    def ok(d):
        return G.tsukiru(dict(q0, shishutsu=q0["shishutsu"] + d, kawaru=[(a, g + d) for a, g in q0.get("kawaru", [])])) is None
    lo, hi = -1000000, 1000000
    if ok(hi):
        return hi
    if not ok(lo):               # 呼ぶ前に「支出0円なら足りる」を確かめてあるので、ふつうは来ない
        return lo
    while hi - lo > 100:
        mid = (lo + hi) // 2 // 100 * 100
        if mid == lo:
            mid += 100
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _sakaime(p: dict) -> str:
    """境目：切り替わる年齢が何歳からなら、お金が増えなくても目標の年齢まで足りるか（2026-10-14）。
    UX レビュー＝いちばん知りたいのは「一緒に住む家族が何歳まで元気なら足りるか」。前は年齢ごとに並べるだけで、
    極が手で詰めて年齢がぶれた。"""
    if p.get("kirikae_label") in (None, "切り替わる年齢"):
        return ""
    lo, mk = int(p["age"]) + 1, int(p["mokuhyo"])
    if lo >= mk:
        return ""
    ok = {k: G.tsukiru(dict(_seikai_form(dict(p, kirikae=k)), r=0.0)) is None for k in range(lo, mk)}
    ks = [k for k in range(lo, mk) if all(ok[j] for j in range(k, mk))]
    ga = _ga(p)
    if ks and ks[0] == lo:
        return f"境目：{ga}何歳でも、お金が増えなくても{mk}歳まで足ります。"
    if ks:
        k = ks[0]
        return f"境目：{ga}{k}歳{_aite(p, k)}からなら、お金が増えなくても{mk}歳まで足ります。それより早いと足りません。"
    return f"境目：{ga}何歳でも、お金が増えないと{mk}歳までは足りません。"


def _one(p: dict) -> list:
    q = _seikai_form(p)
    q0 = dict(q, r=0.0)
    lab = p.get("kirikae_label") or "切り替わる年齢"
    has_nisa = q.get("nisa", 0) > 0 or any(t[3] and t[2] > 0 for t in q["tameru"])
    r = q["r"] if has_nisa else 0
    rows0, rowsr = G.zandaka(q0), G.zandaka(q)
    t0, tr = G.tsukiru(q0), G.tsukiru(q)
    mk = q["mokuhyo"]
    kk = p.get("kirikae")
    nen = G.nenkin(q)
    # 1行目は「ひとことで」＝大丈夫か・だめか（UX レビュー：数字が苦手な人は、結論が先に無いと読めない）
    head = f"{_ga(p)}{kk}歳{_aite(p, kk)}なら、" if kk is not None else ""
    if t0 is None:
        one = f"ひとことで：{head}お金が増えなくても{mk}歳まで足ります（残り{_man(rows0[-1][3])}）。"
    elif r and tr is None:
        one = f"ひとことで：{head}お金が増えないと{t0}歳でお金がなくなります。NISA が年{r * 100:g}%で増えれば{mk}歳まで足ります。"
    elif r:
        one = f"ひとことで：{head}お金が増えないと{t0}歳で、NISA が年{r * 100:g}%で増えても{tr}歳でお金がなくなります。"
    else:
        one = f"ひとことで：{head}{t0}歳でお金がなくなります。"
    # 「NISA なし」は NISA が本当に無いときだけ（2026-10-14：rimawari 0 で NISA に積んでいても「NISA なし」と出ていた）
    out = [one, f"【{p.get('name') or '計算'}】{lab}{'' if kk is None else f' {kk}歳'}{_aite(p, kk)}・目標{mk}歳"
           + (f"・利回り{round(r * 100, 2)}%" if r else ("・利回り0%" if has_nisa else "・利回り0%（NISA なし）"))]
    kekka = lambda t, rows: f"{mk}歳まで持つ（残り{_man(rows[-1][3])}）" if t is None else f"{t}歳の年に尽きる"
    out.append(f"何歳まで持つか：利回り0%なら {kekka(t0, rows0)}" + (f"／利回り{round(r * 100, 2)}%なら {kekka(tr, rowsr)}" if r else ""))
    # 「暮らしは月いくらまで」は「今月いくら使っていい？」と取り違えやすいので、いつの生活費かと、いまの見込みとの差で言う
    t_ik = G.tsuki_ikura(q0)
    ato = (p.get("kirikae_ato") or "切り替わってから") if kk is not None else "いまから"
    if t_ik is None:
        s = "支出0円でも持たない"
    elif t_ik == 0:
        s = "生活費に回せるお金がありません（支出を0円にして、やっと足りる）"
    else:
        # いまの見込み（65歳から変わる分も含めて）に毎月いくら足せるか。「ずっと同じ額の上限」とくらべると、
        # 65歳から上がる分を見落として「多く使える」のに尽きる、と食い違うため（2026-10-14）
        d = _sa(q0)
        mie = f"いまの見込み 月{round(q0['shishutsu']):,}円" + "".join(f"・{a}歳から月{round(g):,}円" for a, g in q0.get("kawaru", []))
        s = (f"{mie}で、ちょうど足ります" if d == 0 else f"{mie}より、月100万円以上多く使えます" if d >= 1000000
             else f"{mie}より、月{d:,}円多く使えます" if d > 0 else f"{mie}より、月{-d:,}円減らすと足ります") \
            + f"（ずっと同じ額なら月{t_ik:,}円まで）"
    out.append(f"{ato}の生活費（お金が増えない場合）：{s}")
    a0 = G.ato_ikura(q0)
    out.append("あといくら（いまの預金に足せば持つ額・利回り0%）：" + ("1億円でも足りない" if a0 is None else ("足りている" if a0 == 0 else _man(a0))))
    if kk is not None and kk > q["age"]:
        ts0 = G.tsumitate_ikura(q0)
        tsr = G.tsumitate_ikura(q) if r else None
        say = lambda t: "月100万円でも足りない" if t is None else ("積み立てなくても持つ" if t == 0 else f"月{t:,}円")
        over = (f"（いまの年金 月{round(q['nenkin_direct']):,}円より多いので、積み立てだけでは届かない）"
                if "nenkin_direct" in q and ts0 and ts0 > q["nenkin_direct"] else "")
        out.append(f"積立は月いくら（{lab}の前の年まで毎月同じ額）：利回り0%なら {say(ts0)}{over}"
                   + (f"／利回り{round(r * 100, 2)}%なら {say(tsr)}" if r else ""))
    kaishi = q.get("kaishi")
    out.append(f"年金：月{round(nen):,}円（{kaishi}歳から" + ("・直接入力）" if "nenkin_direct" in q else "・自動計算）")
               + "".join(f"→{a}歳から月{round(g):,}円" for a, g in q.get("nenkin_kawaru", [])))
    marks = sorted({a for a in ([kk - 1] if kk else []) + [60, 70, 80, mk - 1] if q["age"] <= a < mk})
    z0 = {a: g for a, y, n, g in rows0}
    zr = {a: (y, n, g) for a, y, n, g in rowsr}
    line = []
    for a in marks:
        s = f"{a}歳の年の終わり {_man(z0[a])}"
        if r:
            y, n, g = zr[a]
            s += f"（利回りあり {_man(g)}＝預金{_man(y)}＋NISA{_man(n)}）"
        line.append(s)
    out.append("残高の節目（利回り0%）：" + "／".join(line))
    return out


def keisan(text: str) -> str:
    """道具の本体（同期・0円）。text＝下の説明どおりの JSON。"""
    if G is None:
        return "計算のファイル（docs/rougo_keisan.py）が読めませんでした。"
    try:
        d = json.loads(text) if isinstance(text, str) else dict(text or {})
    except Exception as e:
        return f"条件の JSON が読めませんでした（{e}）。説明の形で入れ直す。"
    if not isinstance(d, dict):
        return "条件は {…} の形（JSON のオブジェクト）で入れる。"
    try:
        base = copy.deepcopy(PRESETS.get(str(d.get("preset") or ""), _BLANK))
        p = _apply(base, d)
        if p.get("age") is None:
            return "いまの年齢（age）を入れる。"
        if not (isinstance(p.get("mokuhyo"), (int, float)) and p["mokuhyo"] > p["age"]):
            return "目標の年齢（mokuhyo）は、いまの年齢より上にする。"
        hikaku = d.get("hikaku")
        if p.get("kirikae") is None and not hikaku and p.get("kirikae_label") != "切り替わる年齢":
            # 切り替わる年齢が空なら、年齢ごとの見通しを並べる（画面のほぉほぉと同じ）
            cand = [k for k in (45, 47, 50, 51, 55, 60, 65) if p["age"] < k < p["mokuhyo"]]
            hikaku = [{"kirikae": k} for k in cand]
        # 並び＝ひとことで → 境目 → 入れた条件 → くわしい数字（結論を先に。くわしい数字は聞かれたときに使う）
        if hikaku:
            qs = [_apply(p, h) for h in hikaku if isinstance(h, dict)]
            js = [_jouken(q) for q in qs]
            common = [x for x in js[0] if all(x in j for j in js)] if js else _jouken(p)
            sks = [_sakaime(q) for q in qs]
            same_sk = len(set(sks)) <= 1
            lines = ([sks[0]] if same_sk and sks and sks[0] else []) \
                + ["", "入れた条件（下の全部に共通。ここに無いものは計算に入っていない）：", *common, ""]
            for q, j, sk in zip(qs, js, sks):
                diff = [x for x in j if x not in common]
                lines += _one(q) + ([sk] if sk and not same_sk else []) + (["この行で変えた条件：", *diff] if diff else []) + [""]
            return "\n".join(lines).strip()
        one = _one(p)
        sk = _sakaime(p)
        return "\n".join([one[0]] + ([sk] if sk else []) + ["", "入れた条件（ここに無いものは計算に入っていない）：", *_jouken(p), ""] + one[1:])
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"計算できませんでした（{type(e).__name__}: {e}）。入れた数字を見直す。"


def build_server():
    """老後シミュレーターの道具を、Agent SDK に渡せる形で返す。使えなければ None。"""
    if not AVAILABLE:
        return None

    @tool("rougo_keisan",
          "老後シミュレーター（画面と同じ計算）で、年金・遺産・貯金・支出から"
          "「大丈夫か（ひとことで）」「境目（一緒に住む家族が何歳まで元気なら足りるか）」「何歳まで持つか」「生活費は月いくらまで」"
          "「あといくら」「積立は月いくら」と残高の節目を出す。0円。"
          "★お金の見通しを聞かれたら、頭で計算せずにこれを呼ぶ。条件を変えて何度呼んでもよい。「計算します」だけで返事を終えず、"
          "同じ返事の中で呼んで答えまで出す。"
          "★答え方（数字が苦手でも1回で分かる形）：1行目は答えの「ひとことで」か「境目」をそのまま言う。"
          "数字は1行に1つ、全部で3つまで。入れた条件・残高の節目・"
          "あといくら・積立は、聞かれたときだけ言う。訂正は、正しい答えを先に1〜3行で言い、最後に『前の◯◯は私の間違いでした』を"
          "1行だけ置く（間違えた数字は書き直さない）。言い換え：一人暮らしになる年齢55歳→親が83歳まで元気なら／"
          "尽きる→お金がなくなる／持つ→足ります／利回り0%→お金が増えなくても（ふだんは言わない）／"
          "一時費用・ならす→家の修理や家電の買い替え（毎年少しずつ取っておく分）／単身→一人暮らし。"
          "★答えの「入れた条件」に書いてあるものだけが、計算に入っている。そこに無いものを『入っている』と言わない。"
          "何が入っているかを聞かれたら、聞かれた物が入っているかを1つずつ答える。条件の全文は読み上げない。"
          "★2つの案を比べるときは、同じ数え方で入れる（片方だけ貯金の行を抜く、をしない）。"
          "jouken＝JSON の文字。金額はすべて円（月額は円／月）。年齢は歳。"
          "preset＝\"mihon_a\"（見本A・架空：45歳・親と同居・年金7万円を65歳から・NISA 月2万円と預金 月1万円・支出7.9万円→65歳から8.1万円・"
          "家電30万円 50歳から10年ごとに3回・給湯器15万円 60歳・家の修理200万円 46〜89歳にならす・遺産200万円は一人暮らしの年に入る・親は28歳上）"
          "か \"mihon_b\"（見本B・架空：35歳・国民年金5年＋厚生年金10年・年収350万・辞めるまで月2万円貯める・支出12万）。"
          "preset から変える所だけ書けばよい。何も無ければ空から入れる。"
          "欄：kirikae（切り替わる年齢＝一人暮らしになる年齢・辞める年齢など。この前の年までは"
          "tameru だけ貯まり、この年から『年金−支出』で減る）、age、mokuhyo（目標の年齢・既定90）、"
          "yokin・nisa（いまの残高）、rimawari（NISA の年利%・既定3）、shishutsu（月の支出）、"
          "kawaru（[[年齢, 新しい月の支出], …]）、nenkin_direct（年金の月額・直接）、nenkin_kaishi、"
          "nenkin_kawaru（[[年齢, 新しい年金の月額], …]＝その年齢の年から年金が変わる。"
          "例：月10万円が50歳から月7万円に変わる＝nenkin_direct:100000 と nenkin_kawaru:[[50,70000]]。"
          "画面の年金の枠の「○歳から変わる」と同じ）、"
          "自動計算なら nenkin_jidou:true と noufu_nen・kousei_nen・menjo_nen・yuuyo_nen（いままでの年数）・nenshu（平均の年収）・"
          "mirai（辞めてから60歳まで [[から, まで, \"harau|menjo|yuuyo|minou\"], …]・猶予は50歳まで）、"
          "tameru（置き換え。[[から, まで(null=ずっと), 月額, \"nisa|yokin\", \"名前\"(なくてよい)], …]）、"
          "isan（[[年齢, 金額], …]）、isan_kirikae（切り替わる年齢と同じ年に入る遺産の額）、"
          "ichiji（置き換え）／ichiji_tasu（足す）＝[[\"名前\", 金額, 年齢], [\"名前\", 金額, \"くり返し\", はじめ, 何年ごと, 何回], "
          "[\"名前\", 金額, \"ならす\", から, まで], …]、aite（[呼び名, 年の差]・null で出さない）。"
          "hikaku＝[{…}, {…}] を渡すと、条件を1つずつ変えた結果を並べる（例：[{\"kirikae\":50},{\"kirikae\":60}]）。"
          "kirikae を書かないと、45〜65歳の年齢ごとの見通しを並べる。"
          "★答えるときは、お金が増えない（利回り0%）見通しを基本にし、NISA が増えた場合は『増えれば』と添える。"
          "どの割合で NISA にするか・何に使うかは、本人と家族が決めること（すすめない・決めない）。",
          {"jouken": str})
    async def _keisan(args):
        return _txt(keisan(args.get("jouken", "")))

    return create_sdk_mcp_server(name=SERVER_NAME, tools=[_keisan])
