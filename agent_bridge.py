# -*- coding: utf-8 -*-
"""クロード極 — SDK を呼ぶところ

引き継いだ考え方：
  - tools=[] / setting_sources=[] で標準ツール（約22,000トークン）を積まない
  - SDK2 は env の差し替えだけでアカウントを切り替える（プロセス全体の認証は汚さない）
  - resume で追記式にしてキャッシュを効かせる（種まきは最初の1回だけ）
  - 思考の深さはモデルの世代で効かせ方が違う（下の agent_thinking のコメント参照）
"""
import asyncio
import contextlib
import os
from datetime import datetime
from typing import AsyncIterator, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    create_sdk_mcp_server,
    query as agent_query,
    tool,
)

import config
import memory
import projects
import remind
import taicho
from session_store import kiwami_session_store

# 手帳＝話したことを勝手に覚えていく入れ物（2026-09-02）。
# ★無くても極は今までどおり動く。モデルの実体が無い環境（server/models/ は
#   .gitignore）では、「読めなければ静かに休む」が普通の状態。
try:
    import techo
except Exception as _e:                        # 部品が無くても会話は止めない
    techo = None
    print(f"[techo] 手帳は使えません: {type(_e).__name__}: {_e}")

# 別画面の秘書の道具（2026-09-04）。**スイッチが入のときだけ載る**ので、
# 切のあいだは今までと1トークンも変わらない。読めなくても極はそのまま動く。
try:
    import winpost_tools
except Exception as _e:
    winpost_tools = None
    print(f"[winpost] 別画面の道具は使えません: {type(_e).__name__}: {_e}")

# 図鑑の道具（2026-09-06）。別画面の道具とまったく同じ形で、
# **スイッチが入のときだけ載る**。図鑑が入っていないパソコンでは静かに休む。
try:
    import pokedex_tools
except Exception as _e:
    pokedex_tools = None
    print(f"[pokedex] 図鑑の道具は使えません: {type(_e).__name__}: {_e}")

# Amazonの道具（2026-09-07）。図鑑などとまったく同じ形で、
# **スイッチが入のときだけ載る**。中身はお買い物係に頼むだけ
# （ブラウザは1つのまま）。買う道具は1本も持たせていない。
try:
    import amazon_tools
except Exception as _e:
    amazon_tools = None
    print(f"[amazon] Amazonの道具は使えません: {type(_e).__name__}: {_e}")

# シミュレーターの道具（2026-09-29）。図鑑・Amazon とまったく同じ形で、
# **スイッチが入のときだけ載る**。計算は docs/ の正解スクリプト（画面と1円まで同じ）。
try:
    import rougo_tools
except Exception as _e:
    rougo_tools = None
    print(f"[rougo] シミュレーターの道具は使えません: {type(_e).__name__}: {_e}")

# ランキングの道具（2026-10-01）。シミュレーターとまったく同じ形で、
# **スイッチが入のときだけ載る**。数字は別のアプリに GET で聞くだけ（127.0.0.1 だけ・POST なし・0円）。
try:
    import toushin_tools
except Exception as _e:
    toushin_tools = None
    print(f"[toushin] ランキングの道具は使えません: {type(_e).__name__}: {_e}")

# 書類を読む道具（2026-09-07）。PDF・ワード・エクセルを、中の文字にして読む。
# ★つまみは増やさず、**ウェブ検索と同じつまみ**に載せます（探す→開く→読む が一続き）。
try:
    import shorui_tools
except Exception as _e:
    shorui_tools = None
    print(f"[shorui] 書類の道具は使えません: {type(_e).__name__}: {_e}")

# 送ってもらったファイルを読み返す道具（2026-10-01・file_tools.py）。
# ★つまみは付けず、いつも載せる（2本だけで軽い・持ち主が覚えることを増やさない）。
try:
    import file_tools
except Exception as _e:
    file_tools = None
    print(f"[file_tools] 読み返す道具は使えません: {type(_e).__name__}: {_e}")

# 事実の見張り（2026-09-24）。
# ★ふだんの返事（stream_reply）にだけフックを渡す。話題の回以外は
#   1文字も足さない・判定も呼ばない。道具は1本も止めない（別画面の門番とは別物）。
#   読めなくても極はそのまま動く（フック無しで今までどおり）。
try:
    import jijitsu_guard
except Exception as _e:
    jijitsu_guard = None
    print(f"[jijitsu] 事実の見張りは使えません: {type(_e).__name__}: {_e}")

# 見張りが止めたとき、書き直しの続きの前に入れる線（画面は同じ吹き出しに追記・保存も1発言）
GUARD_LINE = "\n\n――見張りが止めました――\n\n"


# 端末の SDK が「写真を履歴に残さない仕掛け」に対応しているか。
# 一度調べたら覚えておく（毎回 import するとその分だけ遅い）。
_session_store_ok: Optional[bool] = None


def supports_session_store() -> bool:
    """この端末の Agent SDK で、写真を履歴から落とせるか。

    落とせない端末では、1回見ただけで、そのあと毎ターン写真が送られ続ける。
    ＝「見えるけれど枠を食いつぶす」より「見えない」ほうがまし、と判断する。
    """
    global _session_store_ok
    if _session_store_ok is not None:
        return _session_store_ok

    try:
        import dataclasses
        from claude_agent_sdk import ClaudeAgentOptions

        _session_store_ok = any(
            f.name == "session_store"
            for f in dataclasses.fields(ClaudeAgentOptions)
        )
    except Exception as exc:
        print(
            "[kiwami] 写真を落とす仕掛けの判定に失敗（使わない側に倒す）: "
            f"{type(exc).__name__}: {exc}"
        )
        _session_store_ok = False

    if not _session_store_ok:
        print(
            "[kiwami] この端末の Agent SDK は写真を履歴から落とせません。"
            "写真は渡しません（SDK を新しくすると使えます）"
        )
    return _session_store_ok


# ── メモリに書く道具 ──────
# Agent SDK がツールのループを内蔵しているので、インプロセスの MCP サーバーとして
# 1つだけ登録する。**メモリを使う設定のプロジェクトのときだけ**繋ぐ。
_SAVE_DESC = (
    "持ち主について知ったこと・約束・決まりごとを1件おぼえる。"
    "「覚えておいて」と頼まれたとき、または次からも知っておくべきことが分かったときに使う。"
    "1回につき1件、短い一文で書く。"
    "★同じことがらを前に覚えているなら、これを使ってはいけない。"
    "先に recall_memories で探し、update_memory で書き換える。"
    "古い記憶を残したまま新しい記憶を足すと、数字が二重になって計算が狂う。"
)

_RECALL_DESC = (
    "いま自分がおぼえていることを見る。番号（id）つきで返る。"
    "keyword を渡すとその言葉を含むものだけ、省くと全件。"
    "記憶を書き換える前・忘れる前に、まずこれを呼んで番号を自分で調べる。"
    "★持ち主に番号を尋ねてはいけない。番号を知っているのは自分だけで、"
    "持ち主は番号を覚えていない。"
    "計算を頼まれたとき、同じことがらの記憶が複数あると気づいたら、"
    "どれが最新かを自分で見きわめ、古いほうを forget_memory で捨ててから答える。"
)

_UPDATE_DESC = (
    "おぼえていることを1件、新しい中身に書き換える。"
    "数字・日付・事実が変わったときは、新しく覚えるのではなくこれで書き換える。"
    "which には recall_memories で得た番号（id）を渡す。"
    "番号が分からないときは、その記憶の文の一部をそのまま渡してもよい（1件に絞れたときだけ書き換わる）。"
)

_FORGET_DESC = (
    "おぼえていることを1件わすれる。"
    "誤り・重複・古くなって用済みになった記憶を捨てるときに使う。"
    "which には recall_memories で得た番号（id）か、その記憶の文の一部を渡す。"
    "捨てたものは持ち主が戻せる場所へ移るだけなので、迷ったら捨ててよい。"
)

_SAVE_SHARED_DESC = (
    "**みんな共通の袋**へ1件おぼえる（2026-09-02 新設）。"
    "この袋は実務・クロードなど**どのプロジェクトからも読めて、どこからでも書ける**。"
    "持ち主自身のこと・家族・住まい・持ち物・体のこと・お金の決めごと・"
    "呼び方や言葉づかいの好みなど、**どの話をしているときでも当てはまること**は、"
    "自分の袋ではなくこちらへ入れる。"
    "いま話しているこの件にしか効かないことは、こちらへ入れない（自分の袋の save_memory を使う）。"
    "共通の袋の中身は、次の会話から全プロジェクトの目の前に並ぶ。"
)

# 手帳は「おぼえていること」と役割が違う。取り違えないよう、説明でそこを分ける。
#   おぼえていること … 自分で選んで書いた要点。毎回ぜんぶ目の前にある
#   手帳             … 話したことを、話題ごとの粒（段落）にしたもの。目の前には無く、探すか、向こうから出てくる
_SEARCH_TECHO_DESC = (
    "手帳を探す。手帳には、持ち主と話したことが、話題ごとの覚え書き（何を聞かれ、何と答え、何が決まったか）として貯まっている。"
    "「前に話した◯◯」「いつ△△と言ったか」のように、過去のやりとりを確かめたいときに使う。"
    "keyword には探したい言葉を渡す。空白で区切ると、全部の言葉を含む覚え書きが先に並ぶ（例：「電気代 650」）。"
    "★おぼえていることに無くても、手帳には残っていることがある。"
    "思い出せないとき・数字や日付を確かめたいときは、答える前にここを探す。"
    "★「覚えていない」「分からない」「記録にない」と答える前に、言葉を変えて（金額の数字・品名・言い換え）もう一度ここを探すこと。"
    "★持ち主に「前に言いましたか」と尋ねる前に、まず自分でここを見ること。"
)

# search_techo が一度に出す件数。1件は平均50字ほど（長くて150字）なので、30件でも約1,500字。
# ★2026-10-14 までは8件で、文字の合う粒が多いと目当ての粒が枠の外に落ちていた。
SEARCH_TECHO_MAX = 30


# ── 自分のシスプロを書く道具（2026-09-24）──────
# ★書けるのは「外側」（config.sys_file＝スマホの編集欄と同じファイル）だけ。
#   表に出ないシスプロ（system_prompt_core.txt）には届かない。
# ★丸ごと書き直す道具は渡さない。外側は長いもので約3万7千字あり、全文を出し直させると
#   行が抜け落ちる。「この文をこの文に」の置き換えだけにする。
# ★書くたびに前の中身を data/sysprompt_history/<袋>/ に写す＝ゴミ箱と同じで、
#   AI が誤って消しても戻せる。
_READ_SYS_DESC = (
    "自分のシスプロのうち、自分で書き換えられる部分を読む。"
    "keyword を渡すとその言葉を含む段落だけ、空なら全文。"
    "edit_system_prompt の前に、今の文を1文字も違えずに確かめるときに使う。"
)

_EDIT_SYS_DESC = (
    "自分のシスプロ（書き換えられる部分）を直す。"
    "持ち主に「シスプロに書いて／直して」と頼まれたとき、"
    "またはふるまい・口調・決まりごとを今後ずっと変えるときに使う（事実や出来事は save_memory へ）。"
    "old の文を new に置き換える。old は今の中身から1文字も違えずに写し、1か所だけに当たるようにする。"
    "old を空にすると new を末尾に足す。new を空にすると old を消す。"
    "次の発言から効く。前の中身は写しが残るので、持ち主が戻せる。"
)

SYS_HISTORY_DIR = config.DATA_DIR / "sysprompt_history"


def read_own_system_prompt(pid: str, keyword: str = "") -> str:
    """外側を読む。keyword があれば、その言葉を含む段落（空行で区切ったまとまり）だけ。"""
    nl = chr(10)
    body = _read(config.sys_file(pid))
    if not body:
        return "自分のシスプロ（書き換えられる部分）は、まだ空です。"
    kw = (keyword or "").strip()
    if not kw:
        return f"自分のシスプロ（書き換えられる部分・{len(body)}字）：{nl}{nl}{body}"
    paras = [b for b in body.split(nl + nl) if kw in b]
    if not paras:
        return f"自分のシスプロに「{kw}」はありません。"
    return f"「{kw}」を含む段落 {len(paras)} か所：{nl}{nl}" + f"{nl}{nl}――{nl}{nl}".join(paras)


def edit_own_system_prompt(pid: str, old: str, new: str) -> str:
    """外側を「old → new」で直す。返すのは AI に見せる一言。"""
    nl = chr(10)
    path = config.sys_file(pid)
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        body = ""
    old = old or ""
    new = new or ""
    if not old.strip() and not new.strip():
        return "old も new も空なので、何も変えませんでした。"
    if old.strip():
        n = body.count(old)
        if n == 0 and old.strip() in body:        # 前後の空白だけ違うときは、中身で当てる
            old = old.strip()
            n = body.count(old)
        if n == 0:
            return ("その文は、自分のシスプロ（書き換えられる部分）に見つかりませんでした。"
                    "read_system_prompt で今の文を確かめ、1文字も違えずに old へ写してください。"
                    "（表に出ないシスプロと、おぼえていることの並びは、ここでは変えられません）")
        if n > 1:
            return (f"その文は {n} か所にあるので、どこを直すか決められません。"
                    "前後の文を足して1か所に絞ってください。")
        after = body.replace(old, new, 1)
    else:
        after = (body.rstrip() + nl + nl if body.strip() else "") + new.strip()
    after = after.strip()
    if not after:
        return "書き換えると空になるので、止めました。"
    if after == body.strip():
        return "中身が同じなので、何も変えませんでした。"
    # 前の中身を写す（書く前に。写せなければ例外で止まり、書かない）
    hist = SYS_HISTORY_DIR / str(pid)
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    (hist / f"{stamp}.txt").write_text(body, encoding="utf-8")
    path.write_text(after + nl, encoding="utf-8")
    print(f"[sysprompt] 袋{pid} のシスプロを書き換え {len(body.strip())}字 → {len(after)}字（写し {stamp}）")
    return (f"シスプロを書き換えました（{len(body.strip())}字 → {len(after)}字）。"
            "次の発言から効きます。前の中身は写しに残してあります。")

def _taicho_on(pid: str, own: bool) -> bool:
    """体調の記録の道具を載せるか。サーバーに載せる側と、許可を出す側で同じ判定を使う。"""
    return bool(own) and str(pid) in config.TAICHO_PROJECTS


def pick_memory(pid: str, which: str):
    """番号でも、文の一部でも引ける。返すのは (記憶, 迷った件数, どの袋か)。

    ★自分の袋 → みんな共通の袋 の順に探す（2026-09-02）。
      共通の袋の記憶も、いまある update_memory / forget_memory でそのまま
      直せる・忘れられるようにするため。**道具を増やさない**＝説明文のぶん
      入力が太らない。実務の 265〜310 トークンを崩さないための形。
    ★文の一部で引くときは、両方の袋の当たりを合わせて1件のときだけ採る（2026-10-14）。
      たたんだ記憶の頭（文の頭）は、もう片方の袋の写しにも含まれやすい。自分の袋で1件に
      絞れた時点で決めると、共通の袋の記憶を直すつもりで自分の袋の記憶を書き換えてしまう。
    """
    which = (which or "").strip()
    if not which:
        return None, 0, pid
    bags = (pid, memory.SHARED)
    for bag in bags:
        for m in memory.listing(bag):
            if m["id"] == which:
                return m, 1, bag
    hits = [(m, bag) for bag in bags for m in memory.find(bag, which)]
    if len(hits) == 1:
        return hits[0][0], 1, hits[0][1]
    return None, len(hits), (hits[0][1] if hits else pid)


def _memory_server_for(pid: str, own: bool = True):
    """そのプロジェクトの袋を、自分で出し入れできる道具一式にして渡す。

    own=False … そのプロジェクトが「おぼえない」設定のとき。
      自分の袋の道具は1つも渡さず、**みんな共通の袋へ書く道具だけ**を渡す
      （2026-09-02）。
      共通の袋を読むほうは道具が要らない＝シスプロに並んでいるので目の前にある。

    ★呼ばれるたびに作り直す。モジュールに1つだけ置くと、どのプロジェクトの
      記憶なのかツール側から分からず、袋をまたいで混ざる（2026-08-02 プロジェクト化）。

    ★2026-08-28、覚える1本だけだったのを4本にした。
      覚えるしかできないと、訂正のたびに記憶が二重になる。実際に同じ項目が
      複数並び、どれを使うかで計算がぶれていた。
      公式クロードと同じで、番号は持ち主に見せない。AI が自分で調べて自分で直す。
    """
    def _pick(which: str):
        return pick_memory(pid, which)

    @tool("save_memory", _SAVE_DESC, {"text": str})
    async def _save(args):
        text = (args.get("text") or "").strip()
        if not text:
            return {"content": [{"type": "text", "text": "中身が空なので覚えませんでした。"}]}
        saved = memory.add(pid, text, source="ai")
        msg = f"覚えました：{text}" if saved else f"それはもう覚えています：{text}"
        return {"content": [{"type": "text", "text": msg}]}

    @tool("recall_memories", _RECALL_DESC, {"keyword": str})
    async def _recall(args):
        kw = (args.get("keyword") or "").strip()
        items = memory.find(pid, kw) if kw else memory.listing(pid)
        # みんな共通の袋も一緒に返す（2026-09-02）。番号が分かれば、共通の袋のものも
        # update_memory / forget_memory でそのまま直せる（_pick が両方を見るため）。
        shared = memory.find(memory.SHARED, kw) if kw else memory.listing(memory.SHARED)
        if not items and not shared:
            body = "覚えていることはありません。" if not kw else f"「{kw}」を含む記憶はありません。"
            return {"content": [{"type": "text", "text": body}]}
        nl = chr(10)
        out = []
        if items:
            out.append(f"自分の袋 {len(items)} 件（左が番号）：")
            out += [f"{m['id']}  {m['text']}" for m in items]
        if shared:
            if out:
                out.append("")
            out.append(f"みんな共通の袋 {len(shared)} 件（左が番号）：")
            out += [f"{m['id']}  {m['text']}" for m in shared]
        return {"content": [{"type": "text", "text": nl.join(out)}]}

    @tool("update_memory", _UPDATE_DESC, {"which": str, "text": str})
    async def _update(args):
        text = (args.get("text") or "").strip()
        if not text:
            return {"content": [{"type": "text", "text": "新しい中身が空なので書き換えませんでした。"}]}
        m, n, bag = _pick(args.get("which"))
        if not m:
            hint = ("見つかりませんでした。recall_memories で番号を調べてください。"
                    if n == 0 else
                    f"{n} 件に当てはまるので絞れません。recall_memories で番号を調べてください。")
            return {"content": [{"type": "text", "text": hint}]}
        before = m["text"]
        memory.update(bag, m["id"], text)
        nl = chr(10)
        where = "みんな共通の袋の" if bag == memory.SHARED else ""
        return {"content": [{"type": "text",
                             "text": f"{where}記憶を書き換えました。{nl}前：{before}{nl}後：{text}"}]}

    @tool("forget_memory", _FORGET_DESC, {"which": str})
    async def _forget(args):
        m, n, bag = _pick(args.get("which"))
        if not m:
            hint = ("見つかりませんでした。recall_memories で番号を調べてください。"
                    if n == 0 else
                    f"{n} 件に当てはまるので絞れません。recall_memories で番号を調べてください。")
            return {"content": [{"type": "text", "text": hint}]}
        memory.delete(bag, m["id"])
        where = "みんな共通の袋から" if bag == memory.SHARED else ""
        return {"content": [{"type": "text", "text": f"{where}忘れました：{m['text']}"}]}

    @tool("search_techo", _SEARCH_TECHO_DESC, {"keyword": str})
    async def _search_techo(args):
        """手帳を自分で引く道具（2026-09-02）。**読むだけ**なので、何度使ってもよい。

        ★手帳は毎ターン、話に関係する分だけが向こうから出てくる（recall）。
          この道具は、それでも足りないときに自分から探しに行くためのもの。
        """
        kw = (args.get("keyword") or "").strip()
        if techo is None:
            return {"content": [{"type": "text", "text": "いまは手帳を開けません。"}]}
        if not kw:
            return {"content": [{"type": "text", "text": "探したい言葉を keyword に渡してください。"}]}
        try:
            found = await techo.search_all(pid, kw)
        except Exception as e:                 # 手帳の失敗で会話を止めない
            print(f"[techo] 手帳を探せませんでした: {type(e).__name__}: {e}")
            found = []
        if not found:
            return {"content": [{"type": "text",
                                 "text": f"手帳に「{kw}」は見当たりませんでした。"
                                         "別の言葉（金額の数字・品名など）でも引いてみてください。"}]}
        # ★残す順（文字が合うものが先）で切ってから、新しい順に並べる。
        #   切ったぶんは数で知らせる。黙って切ると、極が「記録にない」と言い切る（2026-10-14）。
        hits = sorted(found[:SEARCH_TECHO_MAX], key=lambda h: h.get("ts", ""), reverse=True)
        nl = chr(10)
        lines = [f"（{h.get('date', '')}）{h.get('text', '')}" for h in hits]
        rest = len(found) - len(hits)
        head = (f"手帳から {len(hits)} 件："
                if not rest else
                f"手帳から {len(found)} 件当たりました。{len(hits)} 件を出します"
                f"（残り {rest} 件は、言葉を空白で区切って足すと、全部を含むものが先に並びます）：")
        return {"content": [{"type": "text", "text": nl.join([head] + lines)}]}

    @tool("save_shared_memory", _SAVE_SHARED_DESC, {"text": str})
    async def _save_shared(args):
        """みんな共通の袋へ書く（2026-09-02）。どのプロジェクトからも同じ袋に入る。"""
        text = (args.get("text") or "").strip()
        if not text:
            return {"content": [{"type": "text", "text": "中身が空なので覚えませんでした。"}]}
        # 誰が書いたかを残す。共通の袋は書き手が混ざるので、名前が無いと
        # 画面の一覧で出どころが分からなくなる（memory.add の by）。
        saved = memory.add(memory.SHARED, text, source="ai", by=projects.name_of(pid))
        msg = (f"みんな共通の袋におぼえました：{text}" if saved
               else f"それはもう共通の袋にあります：{text}")
        return {"content": [{"type": "text", "text": msg}]}

    @tool("read_system_prompt", _READ_SYS_DESC, {"keyword": str})
    async def _read_sys(args):
        text = read_own_system_prompt(pid, args.get("keyword") or "")
        return {"content": [{"type": "text", "text": text}]}

    @tool("edit_system_prompt", _EDIT_SYS_DESC, {"old": str, "new": str})
    async def _edit_sys(args):
        text = edit_own_system_prompt(pid, args.get("old") or "", args.get("new") or "")
        return {"content": [{"type": "text", "text": text}]}

    # own=False（「おぼえない」プロジェクト）へは、共通の袋へ書く1本だけを渡す。
    own_tools = [_save, _recall, _update, _forget, _search_techo] if own else []
    # 記録の3本（書く・読む・数える）は、一部のプロジェクトにだけ載せる（2026-09-19・taicho.py）
    if _taicho_on(pid, own):
        own_tools += taicho.tools()
    # リマインダーの札（2026-09-19・remind.py）は、どのプロジェクトにも渡す（「おぼえない」設定でも）
    return create_sdk_mcp_server(
        name="kiwami_memory",
        # 自分のシスプロを書く2本（2026-09-24）も、どのプロジェクトにも渡す（「おぼえない」設定でも）
        tools=own_tools + [_save_shared, _read_sys, _edit_sys] + remind.tools(pid),
    )


# みんな共通の袋へ書く1本（2026-09-02）。**どのプロジェクトにも必ず渡す**＝
# 「おぼえない」設定のプロジェクトでも、共通の袋にだけは書き足せる。
MEMORY_SHARED_TOOL_NAMES = ["mcp__kiwami_memory__save_shared_memory"]

# 自分のシスプロを読む・直す2本（2026-09-24）。**どのプロジェクトにも必ず渡す**。
SYSPROMPT_TOOL_NAMES = ["mcp__kiwami_memory__read_system_prompt",
                        "mcp__kiwami_memory__edit_system_prompt"]

MEMORY_TOOL_NAMES = [
    "mcp__kiwami_memory__save_memory",
    "mcp__kiwami_memory__recall_memories",
    "mcp__kiwami_memory__update_memory",
    "mcp__kiwami_memory__forget_memory",
    "mcp__kiwami_memory__search_techo",
] + MEMORY_SHARED_TOOL_NAMES

# app_sid -> {"sdk_sid": str, "synced": int} をアカウントごとに分けて持つ
_sessions: dict[str, dict[str, dict]] = {"sdk1": {}, "sdk2": {}}


def _acct(account: str) -> str:
    """画面から来た枠の名前を "sdk1"/"sdk2" に正す。

    ★画面は極を作った日（2026-08-01）から "agent"/"claude2"（旧い呼び名）を
      送っていて、ここの判定 "sdk2" と食い違っていた＝**SDK2 を選んでも SDK1 で
      叩いていた**（2026-08-13 に発覚。SDK1 の鍵が腐っていたので両方 401 に見えた）。
      画面の値は sdk1/sdk2 に直したが、古い画面・端末に保存ずみの設定でも通るよう、
      ここでは旧名も受ける。
    """
    return "sdk2" if str(account) in ("sdk2", "claude2") else "sdk1"


# ── シスプロ ────────────────────────────────────────────
def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


# 曜日の1文字（datetime.weekday() は 月=0）
_WEEK = "月火水木金土日"


def now_line(turns: int = 0) -> str:
    """持ち主の発言の頭に添える目印（2026-09-01）。

    ★なぜシスプロに入れないか
      シスプロは resume のあいだキャッシュに乗っている。分きざみの時刻を入れると
      **毎ターン作り直し**になって、そのぶん高くつく。日付だけがシスプロに残り、
      分きざみの「いま」は発言のほうへ添える＝キャッシュは無傷のまま、時刻は毎回正しい。
    ★保存する本文には足さない（app.py が store へ入れたあと、SDK へ渡す分にだけ付く）。
    """
    n = datetime.now()
    stamp = f"{n.year}年{n.month:02d}月{n.day:02d}日 {_WEEK[n.weekday()]}曜日 {n.strftime('%H:%M')}"
    return f"［いま {stamp}／このスレ {turns}往復目］" if turns else f"［いま {stamp}］"


def _system_prompt_arg(text: str):
    """シスプロを CLI へ渡す形。長いときはファイルにして場所だけ渡す（2026-09-25）。

    ★SDK は str のシスプロを `--system-prompt <本文>` でコマンドラインに載せる。
      Windows のコマンドラインは 32,767 字まで。超えると WinError 206 が FileNotFoundError になり、
      SDK は「CLINotFoundError: Claude Code not found」と出す（claude.exe は在るのに）。
      袋3が 31,887 字になった夜に実際に起きた（2026-09-25 23:42・Fable 5.1）。
    ★ファイルで渡すのは SDK 公式の口（SystemPromptFile → --system-prompt-file）。届く中身は同じ。
      名前は中身の指紋なので、同じシスプロなら同じファイル・同時に何本走っても取り合わない。"""
    if len(text) < 8000:
        return text
    import hashlib
    d = config.DATA_DIR / "sysprompt_files"
    d.mkdir(exist_ok=True)
    p = d / (hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + ".txt")
    if not p.exists():
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    return {"type": "file", "path": str(p)}


def build_system_prompt(pid: str) -> str:
    """プロジェクトの設定どおりに組み立てる（2026-08-02 プロジェクト化）。

      日付 ＋ 表に出ないシスプロ（core のときだけ）＋ 外側 ＋ おぼえていること（memory のときだけ）

    ★実務（"1"）とクロード（"2"）の出力は、プロジェクト化の前と1文字も変わらない。
      1＝日付＋表に出ないシスプロ＋外側＋記憶 ／ 2＝日付＋外側だけ。
    """
    n = datetime.now()
    today = f"{n.year}年{n.month:02d}月{n.day:02d}日 {_WEEK[n.weekday()]}曜日"
    # ★ここは1日1回しか変わらない＝キャッシュを壊さない。
    #   分きざみの時刻は now_line() で、発言のほうへ添える。
    parts = [f"今日は {today} です。",
             "持ち主の発言の頭に付く ［いま …］ は、システムが足した目印です"
             "（いまの日時と、このスレで話しかけられた回数）。持ち主が書いたものではないので、"
             "聞かれないかぎり、返事でそのまま繰り返さなくてよいです。"]

    proj = projects.get(pid)
    if proj.get("core"):
        parts.append(_read(config.SYS_CORE_FILE))
    parts.append(_read(config.sys_file(pid)))     # 外側（スマホから書くところ）
    if proj.get("memory"):
        parts.append(memory.block(pid))           # 1件も無ければ空＝何も積まない

    # みんな共通の袋（2026-09-02）。★プロジェクトの「おぼえない」設定に関わらず積む。
    #   みんなそれぞれ書きこめて見れる＝袋の壁を越えるための
    #   ものなので、ここで proj.memory を見てしまうと作った意味が無くなる。
    #   1件も無いうちは空文字が返る＝**いままでの入力の重さは1トークンも変わらない**。
    # ★「おぼえない」設定のプロジェクトには recall_memories が無い＝たたんだ記憶を開けないので、
    #   共通の袋を全文で積む（2026-10-14・memory.block_shared の full）。
    parts.append(memory.block_shared(full=not proj.get("memory")))

    return "\n\n".join(p for p in parts if p)


# ── 思考の深さ ──────────────────────────────────────────
# 実測（claude CLI 2.1.220 / claude_agent_sdk 0.2.101）に基づく。
#   - Opus 5 / Fable 5 … adaptive ＋ effort。display を明示しないと思考テキストが返らない
#   - Opus 5 は disabled にするとツール呼び出しが本文に漏れる実害があるので使わない
#   - Fable 5 / Fable 5.1 は思考を切れない（400 になる）＝「オフ」は effort low に寄せる
#   - Opus 4.6 … CLI が adaptive の世代として扱う＝予算は使われず、effort で深さが決まる（2026-09-10 実測）
#   - Opus 4.5 … effort max は CLI が high に戻す。深さは budget_tokens で表す
#   - Opus 4.7 / 4.8 … 5 と同じ adaptive 世代（budget_tokens は 400 になる）
ADAPTIVE_MODELS = frozenset({"claude-opus-4-7", "claude-opus-4-8",
                             "claude-opus-5", "claude-opus-5-5"})

# ★「オフ」で thinking を切ってはいけないモデル。
#   Opus 5 は disabled にするとツール呼び出しが本文に漏れて save_memory が動かない実害がある。
#   4.7 / 4.8 はここに入れない＝オフはちゃんと切る。
NO_DISABLE_MODELS = frozenset({"claude-opus-5", "claude-opus-5-5"})
FABLE_MODELS = frozenset({"claude-fable-5", "claude-fable-5-1"})
# ★CLI が adaptive で送るのに、上の ADAPTIVE_MODELS に入っていない世代（2026-09-10）。
#   予算を渡しても CLI は adaptive＋effort high で送っていた＝「最大」が「おまかせ」と同じ中身だった。
ADAPTIVE_BY_CLI = frozenset({"claude-opus-4-6"})


def agent_thinking(model: str, effort: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """(thinking, effort) を返す。ClaudeAgentOptions にそのまま渡せる形。"""
    if effort in (None, "", "keep"):
        # 深さは選ばれていない（「おまかせ」や、深さ欄を持たないモデルのとき）。
        # ★深さは CLI の既定に任せるが、**思考テキストの出し方だけは明示する**（2026-09-02）。
        #   adaptive 世代は display を書かないと既定が "omitted" ＝思考が1文字も返らず、
        #   画面の「考えていたこと」が空のままになる。手当て。
        #   世代の分からない「おまかせ」（model が空）には出さない＝400 を踏まないため。
        if model in ADAPTIVE_MODELS or model in FABLE_MODELS:
            return {"type": "adaptive", "display": "summarized"}, None
        return None, None                      # それ以外は CLI の既定に任せる

    if effort == "off":
        if model in FABLE_MODELS or model in NO_DISABLE_MODELS:
            # 切らずに、いちばん浅い effort へ寄せる（disabled は上のコメントの実害がある）
            return {"type": "adaptive", "display": "summarized"}, "low"
        return {"type": "disabled"}, None

    if model in ADAPTIVE_MODELS or model in FABLE_MODELS:
        return {"type": "adaptive", "display": "summarized"}, effort

    # Opus 4.6：深さは effort で渡す（画面の「最大」＝ max がそのまま API に届く）
    if model in ADAPTIVE_BY_CLI:
        return {"type": "adaptive", "display": "summarized"}, effort

    # ★low / medium は effort が効く世代だけの段階。ここへ来たのは budget_tokens 世代
    #   （Opus 4.5 以前）なので、今までどおり「深さを指定しない」に落とす。
    #   Opus 4.5 には触らない（2026-09-20）＝ 4.5 の中身は一字も変えない。
    if effort in ("low", "medium"):
        return None, None

    # Opus 4.5 以前：off 以外はいちばん大きい budget に寄せる（63999・出力の上限は _options が上げる）
    # display は 2026-09-10 から明示する（この日から API が 4.5/4.6 でも "omitted" を守るようになった）。
    budget = config.THINKING_BUDGETS.get("max")
    return ({"type": "enabled", "budget_tokens": budget, "display": "summarized"} if budget else None), None


# ── 道具を使ったことを、画面へ知らせる ──────────────────────
# ★それまでは道具の**名前だけ**を流していた（画面には
#   「mcp__kiwami_memory__save_memory を使っています」と生の名前が出るだけ）。
#   （2026-09-01）
#   名前のほかに「日本語の見出し」と「中身」を添えて、画面が会話の中に出せるようにする。
_TOOL_LABEL = {
    "WebSearch": "調べています",
    "WebFetch": "ページを開いています",
    "mcp__kiwami_memory__save_memory": "おぼえました",
    "mcp__kiwami_memory__save_shared_memory": "みんな共通におぼえました",
    "mcp__kiwami_memory__update_memory": "おぼえ直しました",
    "mcp__kiwami_memory__forget_memory": "わすれました",
    "mcp__kiwami_memory__recall_memories": "思い出しています",
    "mcp__kiwami_memory__search_techo": "手帳を見ています",
    taicho.KAKU: "体調を書きました",
    taicho.YOMU: "体調の記録を見ています",
    taicho.KAZOERU: "悪い日を数えています",
    remind.TOUROKU: "リマインダーに登録しました",
}
# 中身のどこを見せるか（道具ごとに引数の名前が違う）
_TOOL_FIELD = {
    "mcp__kiwami_memory__save_memory": "text",
    "mcp__kiwami_memory__save_shared_memory": "text",
    "mcp__kiwami_memory__update_memory": "text",
    "mcp__kiwami_memory__forget_memory": "which",
    "mcp__kiwami_memory__recall_memories": "keyword",
    "mcp__kiwami_memory__search_techo": "keyword",
    "WebSearch": "query",
    "WebFetch": "url",
    taicho.YOMU: "kara",
    taicho.KAZOERU: "kara",
}
# 引数1つでは中身が見えない道具は、ここで1行に組む（体調を書いた中身＝日付・悪い日か・やったこと・代償）
_TOOL_TEXT = {
    taicho.KAKU: taicho.event_text,
}
# 会話の中に残す道具（記憶が動いたときだけ。調べものは流れて消えてよい）
# ★記録を書いたぶんも残す＝書いた中身を、持ち主がその場で確かめられる（2026-09-19）
MEMORY_MOVED = frozenset({
    "mcp__kiwami_memory__save_memory",
    "mcp__kiwami_memory__save_shared_memory",
    "mcp__kiwami_memory__update_memory",
    "mcp__kiwami_memory__forget_memory",
    taicho.KAKU,
})

# 別画面の道具の見出しも、同じ表へ入れておく（無いと画面に生の英語名が出る）
if winpost_tools is not None:
    _TOOL_LABEL.update(winpost_tools.TOOL_LABELS)
    _TOOL_FIELD.update(winpost_tools.TOOL_FIELDS)
    MEMORY_MOVED = MEMORY_MOVED | winpost_tools.KEEP_IN_CHAT

# 図鑑の見出しも、同じ表へ（2026-09-06）
if pokedex_tools is not None:
    _TOOL_LABEL.update(pokedex_tools.TOOL_LABELS)
    _TOOL_FIELD.update(pokedex_tools.TOOL_FIELDS)
    MEMORY_MOVED = MEMORY_MOVED | pokedex_tools.KEEP_IN_CHAT

# Amazonの見出しも、同じ表へ（2026-09-07）。
# ★カゴが動いたぶんは会話の中に残す（KEEP_IN_CHAT）＝あとで「何を入れたか」を見返せる。
if amazon_tools is not None:
    _TOOL_LABEL.update(amazon_tools.TOOL_LABELS)
    _TOOL_FIELD.update(amazon_tools.TOOL_FIELDS)
    MEMORY_MOVED = MEMORY_MOVED | amazon_tools.KEEP_IN_CHAT

# シミュレーターの見出しも、同じ表へ（2026-09-29）
if rougo_tools is not None:
    _TOOL_LABEL.update(rougo_tools.TOOL_LABELS)
    _TOOL_FIELD.update(rougo_tools.TOOL_FIELDS)

# ランキングの見出しも、同じ表へ（2026-10-01）
if toushin_tools is not None:
    _TOOL_LABEL.update(toushin_tools.TOOL_LABELS)
    _TOOL_FIELD.update(toushin_tools.TOOL_FIELDS)

# 書類を読む道具の見出しも、同じ表へ（2026-09-07）
if shorui_tools is not None:
    _TOOL_LABEL.update(shorui_tools.TOOL_LABELS)
    _TOOL_FIELD.update(shorui_tools.TOOL_FIELDS)
    MEMORY_MOVED = MEMORY_MOVED | shorui_tools.KEEP_IN_CHAT

# 保存したファイルを読み返す道具の見出しも、同じ表へ（2026-10-01）
if file_tools is not None:
    _TOOL_LABEL.update(file_tools.TOOL_LABELS)
    _TOOL_FIELD.update(file_tools.TOOL_FIELDS)


def tool_event(block) -> dict:
    """道具ブロック1つを、画面が出せる形にする。"""
    name = getattr(block, "name", type(block).__name__)
    args = getattr(block, "input", None) or {}
    field = _TOOL_FIELD.get(name)
    text = str(args.get(field) or "") if (field and isinstance(args, dict)) else ""
    if name in _TOOL_TEXT and isinstance(args, dict):
        text = _TOOL_TEXT[name](args)
    ev = {
        "name": name,
        "label": _TOOL_LABEL.get(name, name),
        "text": text[:300],
        "keep": name in MEMORY_MOVED,     # True＝会話の中に残す
    }
    # リマインダーの札（2026-09-19）。読めた引数のときだけ札の中身を添える＝画面が会話の中に残す。
    # ★読めない引数（過ぎた日時など）は道具のほうが断るので、札もボタンも出さない。
    #   app._reply_worker が "remind" を集めて、返事の発言と一緒に保存する（開き直しても押せる）。
    if name == remind.TOUROKU and isinstance(args, dict):
        r = remind.normalize(args)
        if isinstance(r, dict):
            ev["text"] = remind.event_text(r)
            ev["keep"] = True
            ev["remind"] = {k: r[k] for k in ("title", "due", "notes", "label")}
    return ev


# ── 道具を何回まで呼んでよいか ──────────────────────────
# ★ここが足りないと、SDK は**返事を書かないまま**終わります（error_max_turns）。
#   画面には何も出ないので、持ち主には「止まった」に見えます。
#   2026-09-07：ウェブ検索が3回止まった件の芯がここでした。検索は
#   「探す（WebSearch）→ 開く（WebFetch）→ もう1つ開く → 答える」と進むので、
#   6回はすぐ使い切ります。別画面の道具だけが 14 に増やされていて、検索は 6 のままでした。
_TURNS_PLAIN = 6          # 道具を1本も持たないとき（今までどおり）
_TURNS_TOOLS = 14         # 自分の家の道具（別画面・図鑑・Amazon）
_TURNS_WEB = 20           # ウェブ検索（外のページは、何ページも開くことがある）


def _max_turns(web_search: bool, *servers) -> int:
    turns = _TURNS_PLAIN
    if any(s is not None for s in servers):
        turns = max(turns, _TURNS_TOOLS)
    if web_search:
        turns = max(turns, _TURNS_WEB)
    return turns


def _ended_early_note(msg) -> str:
    """「ふつうに書き終えた」以外で終わったとき、その訳を日本語で1行にする。

    ★これが無いと、途中で打ち切られたときに**何も言い残さないまま**終わります。
      持ち主には「止まった」に見えるので、必ずひとこと残します（2026-09-07）。
    ★戻す文は「返事の続き」として保存されます＝画面を開き直しても消えません。
      これが大事なところで、それまでの英語の断り文は保存されていませんでした。
    """
    subtype = str(getattr(msg, "subtype", "") or "")
    if subtype in ("", "success") and not getattr(msg, "is_error", False):
        return ""                                   # ふつうに書き終えた

    if subtype == "error_max_turns":
        turns = getattr(msg, "num_turns", None)
        return ("（調べものの手数をここで使い切ったので、いったん切れました"
                f"{f'＝{turns}回' if turns else ''}。"
                "続きが要るときは「続けて」とおっしゃってください）")
    if subtype == "error_during_execution":
        return "（調べている途中で止まってしまいました。もう一度お声がけください）"

    # それ以外（API が混んでいた等）。SDK と同じ順で、いちばん中身のある言葉を選ぶ。
    errors = [str(e) for e in (getattr(msg, "errors", None) or []) if str(e).strip()]
    result = str(getattr(msg, "result", "") or "").strip()
    status = getattr(msg, "api_error_status", None)
    detail = "／".join(errors[:2]) or result or (f"HTTP {status}" if status else subtype)
    return f"（途中で止まりました：{detail[:200]}。もう一度お声がけください）"


# ── SDK を呼ぶ ──────────────────────────────────────────
def _options(persona: str, account: str, model: str, effort: Optional[str],
             web_search: bool, resume: Optional[str],
             winpost: bool = False, pokedex: bool = False,
             amazon: bool = False,
             hooks: Optional[dict] = None, rougo: bool = False,
             toushin: bool = False) -> ClaudeAgentOptions:
    thinking_cfg, effort_level = agent_thinking(model, effort)

    # 検索オフなら tools=[]（標準ツール約22,000トークンを1つも積まない）。
    # オンのときだけ、探す道具と読む道具を2つ積む（2026-09-01）。
    #   WebSearch … 探して、見出しと URL の一覧を持ってくる
    #   WebFetch  … その URL を開いて、中身を読む
    # ★WebFetch が無いと、一覧は出るのに中身が読めない＝「使えない」になる。
    # 重さの実測（sonnet・下地だけ・1回ずつ）：1,294 → 1,786 トークン＝WebFetch は +492。
    # 許可は allowed_tools に "WebFetch" と書けば、どのサイトでも通る（サイトごとの許可は要らない）。
    tools = ["WebSearch", "WebFetch"] if web_search else []

    # 自分の袋を使う設定のプロジェクトにだけ、自分の袋の道具を渡す＝混ざらない。
    # ★みんな共通の袋へ書く1本は、どのプロジェクトにも渡す（2026-09-02）。
    #   「おぼえない」設定のプロジェクトでも、共通の袋にだけは書き足せるようにするため。
    use_memory = bool(projects.get(persona).get("memory"))
    allowed = tools + (MEMORY_TOOL_NAMES if use_memory else MEMORY_SHARED_TOOL_NAMES)
    if _taicho_on(persona, use_memory):       # 記録の道具（一部のプロジェクトだけ・2026-09-19）
        allowed = allowed + taicho.TOOL_NAMES
    allowed = allowed + remind.TOOL_NAMES     # リマインダーの札（全プロジェクト・2026-09-19）
    allowed = allowed + SYSPROMPT_TOOL_NAMES  # 自分のシスプロを書く（全プロジェクト・2026-09-24）
    servers = {"kiwami_memory": _memory_server_for(persona, own=use_memory)}

    # 別画面の道具は、スイッチが入のときだけ載せる（ウェブ検索と同じ形）。
    # ★切のときは1本も載らない＝ふだんの会話の重さは今までどおり。
    wp = None
    if winpost and winpost_tools is not None:
        wp = winpost_tools.build_server()
    if wp is not None:
        servers[winpost_tools.SERVER_NAME] = wp
        allowed = allowed + winpost_tools.TOOL_NAMES

    # 図鑑の道具も、同じくスイッチが入のときだけ（2026-09-06）。
    pd = None
    if pokedex and pokedex_tools is not None:
        pd = pokedex_tools.build_server()
    if pd is not None:
        servers[pokedex_tools.SERVER_NAME] = pd
        allowed = allowed + pokedex_tools.TOOL_NAMES

    # Amazonの道具も、同じくスイッチが入のときだけ（2026-09-07）。
    # ★載るのは「探す・見る・読む・カゴに入れる」の4本だけ。買う道具は繋いでいない。
    az = None
    if amazon and amazon_tools is not None:
        az = amazon_tools.build_server()
    if az is not None:
        servers[amazon_tools.SERVER_NAME] = az
        allowed = allowed + amazon_tools.TOOL_NAMES

    # シミュレーターの道具も、同じくスイッチが入のときだけ（2026-09-29）。
    rg = None
    if rougo and rougo_tools is not None:
        rg = rougo_tools.build_server()
    if rg is not None:
        servers[rougo_tools.SERVER_NAME] = rg
        allowed = allowed + rougo_tools.TOOL_NAMES

    # ランキングの道具も、同じくスイッチが入のときだけ（2026-10-01）。
    # ★載るのは4本だけ。別のアプリに GET で聞くだけで、書き込む道具は無い。
    ts = None
    if toushin and toushin_tools is not None:
        ts = toushin_tools.build_server()
    if ts is not None:
        servers[toushin_tools.SERVER_NAME] = ts
        allowed = allowed + toushin_tools.TOOL_NAMES

    # 書類を読む道具は、**ウェブ検索と同じつまみ**に載せる（2026-09-07）。
    # ★WebFetch は PDF を取ってはくるが、圧縮されたまま渡すので中身が読めない
    #   （ある市の冊子で実際に詰まった。「PDF形式の圧縮データであり、テキストとして
    #   正常に解析できません」が返っていた）。その受け皿なので、検索と一続きが自然。
    #   つまみを増やさないぶん、持ち主が覚えることも増えない。
    sr = None
    if web_search and shorui_tools is not None:
        sr = shorui_tools.build_server()
    if sr is not None:
        servers[shorui_tools.SERVER_NAME] = sr
        allowed = allowed + shorui_tools.TOOL_NAMES

    # 送ってもらったファイルを読み返す道具（2026-10-01）。つまみは無く、いつも載せる。
    # ★「探す→読む→続きを読む」と数回呼ぶので、道具の手数（max_turns）も道具ありの数にする。
    ft = None
    if file_tools is not None:
        ft = file_tools.build_server()
    if ft is not None:
        servers[file_tools.SERVER_NAME] = ft
        allowed = allowed + file_tools.TOOL_NAMES

    common = dict(
        system_prompt=_system_prompt_arg(build_system_prompt(persona)),
        tools=tools,                 # 組み込みツールの集合。MCP はここには入らない
        allowed_tools=allowed,       # 許可（MCP のツール名はこちらに入れる）
        mcp_servers=servers,
        setting_sources=[],          # CLAUDE.md 等を読み込ませない
        # 道具を何回まで呼べるか。★6回は「道具を1本も持たない、ふつうの会話」のための数です。
        #   道具を持たせると「探す→開く→読む→答える」と何度も呼ぶので、6回では足りません。
        #   ★足りないと、SDK はそこで**返事を書かないまま**終わります。
        #     画面には何も出ないので、持ち主には「止まった」に見えます
        #     （2026-09-07・ウェブ検索が3回止まった件。下の「打ち切られた合図」もそのときの手当て）。
        max_turns=_max_turns(web_search, wp, pd, az, ft),
        # ★書けたそばから届けてもらう（2026-09-02）。
        #   これが無いと、考えたぶんも返事も**できあがってから丸ごと**届くので、
        #   長い返事ほど画面が止まって見える。実測：
        #   本文の1粒目 2.72秒／まとまって届くのは 3.41秒（短い返事でも 0.7秒ぶん早い）。
        #   枠の減りは変わらない（同じ言葉を、細かく分けて受け取るだけ）。
        include_partial_messages=True,
        # ★CLI から受け取る1行の上限（2026-09-21）。
        #   SDK の既定は 1MB。写真つきの発言は CLI がそのまま1行で返すので、写真2枚（base64 で約1.3MB）で
        #   「JSON message exceeded maximum buffer size」になって落ちていた。
        #   枚数の上限は置かず、受け取る側を広げる。64MB は API が1回に受け付ける量（32MB）より上。
        max_buffer_size=64 * 1024 * 1024,
    )
    if model:
        common["model"] = model
    if thinking_cfg:
        common["thinking"] = thinking_cfg
    if effort_level:
        common["effort"] = effort_level
    # ★思考の中身を返してもらう（2026-09-10）。
    #   CLI は SDK から呼ばれると、指定が無ければ思考を display="omitted"（中身を返さない）で頼む。
    #   この日から API が Opus 4.5/4.6 でもそれを守るようになった（Sonnet 5 は前から0字）。
    #   thinking が空のとき＝「おまかせ」「いままで通り」・深さ欄の無いモデル・声かけ、だけここで足す。
    #   thinking に display が入っているときは SDK が付けるので、2回は付けない。
    #   「おまかせ」（model が空）にも付けてよい ── CLI はもともと display を毎回送っているので、
    #   値が omitted → summarized に変わるだけで 400 は踏まない。お金も変わらない。
    if thinking_cfg is None:
        common["extra_args"] = {"thinking-display": "summarized"}

    # 鍵は**毎回 .env から読み直して**サブプロセスにだけ注入する
    # （os.environ を土台に CLAUDE_CODE_OAUTH_TOKEN だけ差し替え＝PATH 等はそのまま）。
    # ★起動時の写しでは腐る：極は再起動されないので、鍵を入れ替えた日から 401 になる
    #   （2026-08-13・config.sdk_tokens() のコメント参照）。
    tok1, tok2 = config.sdk_tokens()
    token = tok2 if (_acct(account) == "sdk2" and tok2) else tok1
    extra_env = {}
    if token:
        extra_env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # メールアドレスを会話に添えさせない（2026-09-13・config.CLI_HOME のコメント参照）。
        # 鍵があるときだけ＝鍵が空で ~/.claude のログインに戻るときは、いままでどおり。
        extra_env["CLAUDE_CONFIG_DIR"] = str(config.CLI_HOME)
    # ★道具1本にかけてよい時間（ミリ秒）。Amazonの「探す」はお買い物係を
    #   最長240秒待つので、CLI 側の「1回あたりの固い上限」に引っかかると、
    #   お買い物係がまだ働いているのに道具だけ失敗します（2026-09-07）。
    #   別画面・図鑑・記憶は一瞬で終わるので、広げても害はありません。
    if az is not None:
        extra_env["MCP_TOOL_TIMEOUT"] = "300000"      # 5分
    # ★「最大」の予算を API まで届かせる（2026-09-10）。CLI は予算を「出力の上限 − 1」に
    #   切り詰めるので、上限が既定の 32000 のままだと 63999 を渡しても 31999 になる。
    #   予算が 32000 以上のときだけ上限を上げる。上げても、使ったぶんしか数えられない。
    if thinking_cfg and thinking_cfg.get("type") == "enabled" and (thinking_cfg.get("budget_tokens") or 0) >= 32000:
        extra_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(thinking_cfg["budget_tokens"] + 1)
    if extra_env:
        common["env"] = {**os.environ, **extra_env}

    if supports_session_store():
        common["session_store"] = kiwami_session_store   # 写真を履歴に居座らせない

    if resume:
        common["resume"] = resume
    # ★Opus 5.5 のときだけ、同梱より新しいクロードコードを使う（2026-09-23・config.MODEL_MIN_CLI）。
    #   ほかのモデルは None ＝ SDK が今までどおり同梱の CLI を選ぶ（送る中身は1文字も変わらない）。
    common["cli_path"] = config.agent_cli_path(model)
    # ★事実の見張りのフック（2026-09-24）。stream_reply だけが渡す。引き継ぎメモ・声かけは渡さない。
    if hooks:
        common["hooks"] = hooks
    return ClaudeAgentOptions(**common)


def _prompt_with_photos(text: str, photo_blocks: list):
    """写真つきの発言を、SDK に渡せる形（ストリーム）にする（2026-08-15）。

    ★SDK は文字列を渡しても、内部でこれと同じ dict を1個 stdin へ書いているだけ
      （claude_agent_sdk/_internal/client.py の `isinstance(prompt, str)` 分岐）。
      違うのは content が文字列かリストか、それだけ。だから resume とも共存する
      （resume は CLI の引数で、入力の形とは無関係）。
    ★写真は**この1回の発言**にしか乗らない。CLI 側の履歴に残るぶんは
      session_store（_strip_images）が落とす＝次のターンからは軽くなる。
    """
    async def _stream():
        yield {
            "type": "user",
            "session_id": "",
            "message": {
                "role": "user",
                "content": [*photo_blocks, {"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
        }
    return _stream()


async def _techo_block(pid: str, user_text: str) -> str:
    """手帳から、いまの話に関係する分だけを引く（2026-09-02）。

    ★**失敗しても空文字を返すだけ。** 手帳の不調で、極が黙ることは無い。
    ★メモリを使わない設定の袋（クロード＝"2"）では引かない＝今までどおりの軽さのまま。
    ★生成AIは呼ばない。このPCの中の計算だけ＝**0円**なので、毎ターン引いてよい。
    """
    if techo is None:
        return ""
    try:
        if not projects.get(pid).get("memory"):
            return ""
        return await techo.recall(pid, user_text)
    except Exception as e:
        print(f"[techo] 思い出せませんでした: {type(e).__name__}: {e}")
        return ""


async def stream_reply(app_sid: str, user_text: str, *, persona: str = "1",
                       account: str = "sdk1", model: str = "", effort: Optional[str] = None,
                       web_search: bool = False, seed_text: str = "",
                       photo_blocks: Optional[list] = None,
                       turns: int = 0,
                       winpost: bool = False,
                       pokedex: bool = False,
                       amazon: bool = False,
                       guard: bool = True,
                       rougo: bool = False,
                       toushin: bool = False) -> AsyncIterator[tuple[str, object]]:
    """1往復を流す。("thinking"|"text"|"tool"|"result", 中身) を順に返す。

    seed_text … CLI セッションがまだ無いときに送る「これまでの流れ」。
    2回目以降は resume で最新の発言だけ送る＝過去ぶんは CLI のキャッシュに任せる。
    """
    store = _sessions[_acct(account)]
    sess = store.get(app_sid)
    resume = sess["sdk_sid"] if sess else None

    # 手帳から思い出す（0円）。
    # ★**シスプロには入れない。** シスプロは resume のあいだキャッシュに乗っているので、
    #   毎ターン変わるものを入れると作り直しになって高くつく（now_line と同じ理由）。
    #   この1回の発言のほうへ添える＝キャッシュは無傷のまま、思い出はいまの話に合う。
    recalled = await _techo_block(persona, user_text)

    # いまの日時と往復数を、この1回の発言にだけ添える（保存ずみの本文は汚さない）
    said = f"{now_line(turns)}\n{user_text}"
    if recalled:
        said = f"{recalled}\n\n{said}"          # 思い出 → 目印 → 持ち主の言葉、の順

    # ★別画面の帳面を、この1回の発言に添える（スイッチが入のときだけ・2026-09-04）。
    #   セッションをまたいで、話の内容を覚えておけるようにするため。
    #   ★シスプロには入れません（上と同じ理由。帳面は道具で変わるため）。
    #   ★切のあいだは1文字も通りません。帳面そのものは残ります＝セーブデータと同じ扱い。
    if winpost and winpost_tools is not None:
        wp_block = winpost_tools.briefing()
        if wp_block:
            said = f"{wp_block}\n\n{said}"      # 帳面 → 思い出 → 目印 → 持ち主の言葉

    # ★図鑑の一枚も、同じ場所に（スイッチが入のときだけ・2026-09-06）。
    #   「手元に図鑑がある」と先に分かっていると、覚えている数字で答えずに引いてくれます。
    #   ★切のあいだは1文字も通りません。
    if pokedex and pokedex_tools is not None:
        pd_block = pokedex_tools.briefing()
        if pd_block:
            said = f"{pd_block}\n\n{said}"

    # ★お買い物の見張り（カゴと「あとで買う」の値下がり・大きいセール）も、同じ場所に（2026-09-23）。
    #   ★つまみは付けない（普段オフだとセールに気付けないため）。知らせが無い日は1文字も通りません。
    #   控えを書くのは別のアプリの夜の見回り（sale_watch.run_daily）のまま。
    if amazon_tools is not None:
        sale_block = amazon_tools.sale_briefing()
        if sale_block:
            said = f"{sale_block}\n\n{said}"
    prompt = said if resume else (f"{seed_text}\n\n{said}".strip() if seed_text else said)
    if photo_blocks:
        prompt = _prompt_with_photos(prompt, photo_blocks)
    # ★事実の見張り（2026-09-24・jijitsu_guard.py の頭のコメント参照）。
    #   guard … フックと _pump が分け合う入れ物（この回の道具の記録・返事の粒・止めたか）。
    #   つまずいたらフック無しで今までどおり（会話を止めない）。
    guard_state, hooks = None, None
    # ★つまみ（2026-09-24）。切＝フックを1本も渡さない
    if jijitsu_guard is not None and guard:
        try:
            guard_state = {"tools": [], "text": [], "blocked": False, "opened_by": "", "model": model}
            hooks = jijitsu_guard.make_hooks(app_sid, persona, account, user_text, guard_state)
        except Exception as e:                   # noqa: BLE001
            print(f"[jijitsu] 見張りを付けられませんでした（今までどおり続けます）: {type(e).__name__}: {e}")
            guard_state, hooks = None, None
    options = _options(persona, account, model, effort, web_search, resume,
                       winpost, pokedex, amazon, hooks=hooks, rougo=rougo, toushin=toushin)

    # 書き終えたら、このスレに CLI セッションを結ぶ（次から resume で続きを話す）
    pump = _pump(prompt, options, record=(store, app_sid), state=guard_state)
    try:
        async for item in pump:
            yield item
    finally:
        # ★外（_reply_worker の aclose・■）でやめられても、中を必ず閉じる。
        #   `async for` は途中で抜けてもイテレータを閉じないので、閉じないと _pump の
        #   finally が走らず claude.exe が残る（_pump の頭のコメントと同じ理由）。
        with contextlib.suppress(BaseException):
            await asyncio.shield(pump.aclose())


# ── スレ引き継ぎ（2026-09-17）──────────────────────────────
# ★書くのは、そのスレで話していた相手（同じプロジェクト）。自分の言葉で書く。
#   モデルは画面のピッカーで選んでいるもの（2026-09-19・app.hikitsugi のコメント参照）。
# ★長さの上限は書かない。以前「1〜3行」を外し、上限なしと決めた。**短くする向きの言葉を、あとから足さないこと。**
HIKITSUGI_PROMPT = (
    "［スレ引き継ぎのお願い］\n"
    "持ち主が、このスレをここで区切って、新しいスレへ移ります。"
    "新しいスレのあなたは、このスレの会話を読めません。"
    "読めるのは、いまあなたが書く引き継ぎメモと、「おぼえていること」「手帳」だけです。\n"
    "新しいスレのあなたが、このスレの続きからそのまま話せるように、引き継ぎメモを書いてください。\n"
    "\n"
    "書くこと：\n"
    "・何の話をしてきたか（話題ごとに）\n"
    "・決まったことと、そう決めた理由\n"
    "・まだ決まっていないこと、途中の話、次にやること、持ち主と約束したこと\n"
    "・出てきた名前・数字・日付・金額・手続きや書類の名前は、そのままの形で\n"
    "・いま話がどこまで進んでいて、最後に何を話していたか\n"
    "・持ち主の気持ちや調子、話し方の好みのうち、続きを話すときに要ること\n"
    "\n"
    "書き方：\n"
    "・中身が分かるように具体的に書く。中身を削って縮めない。ぼかしたり言い換えたりしない\n"
    "・このスレで話していないことは足さない\n"
    "・持ち主への返事ではなく、新しいスレの自分に宛てたメモとして書く。前置きやあいさつは要らない\n"
    "・道具は使わない。この返事の本文が、そのまま新しいスレの頭に入る"
)

# つなぎが切れているとき、スレの全文を読ませる量の手すり（2026-09-17 実測でいちばん長いスレが約6.8万字）。
# 超えたら古いほうから落とす。**メモの長さの上限ではない。**
HIKITSUGI_MAX_CHARS = 150_000


async def stream_hikitsugi(from_sid: str, full_text: str, *, persona: str = "1",
                           account: str = "sdk1", model: str = "",
                           effort: Optional[str] = None, web_search: bool = False,
                           winpost: bool = False, pokedex: bool = False,
                           amazon: bool = False, rougo: bool = False,
                           toushin: bool = False) -> AsyncIterator[tuple[str, object]]:
    """from_sid のスレの相手に、引き継ぎメモを書かせる。流すものは stream_reply と同じ形。

    ・つなぎ（CLI セッション）があるとき … resume ＋ fork_session。元のスレのつなぎは汚さず、
      枝分かれした先で書かせる。これまでの会話はキャッシュから読むので軽い。
    ・つなぎが無いとき（サーバーを起こし直したあと・アカウントを替えたとき）… full_text（スレの全文）を読ませる。

    ★書き終えても、どのスレにもつなぎを結ばない（record=None）。
      新しいスレに枝分かれのつなぎを結ぶと、新しいスレが古い会話まるごとを抱えたまま＝重いままになる。
      新しいスレの最初の送信は、引き継ぎメモを頭にした「これまでの流れ」から始まる（store.history_text）。
    """
    sess = _sessions[_acct(account)].get(from_sid)
    resume = sess["sdk_sid"] if sess else None
    prompt = HIKITSUGI_PROMPT if resume else f"{full_text}\n\n{HIKITSUGI_PROMPT}".strip()
    # 道具のつまみは、いまの画面の値をそのまま渡す（前の送信と道具の並びが同じなら、キャッシュがそのまま効く）
    options = _options(persona, account, model, effort, web_search, resume,
                       winpost, pokedex, amazon, rougo=rougo, toushin=toushin)
    if resume:
        options.fork_session = True
    pump = _pump(prompt, options, record=None)
    try:
        async for item in pump:
            yield item
    finally:
        with contextlib.suppress(BaseException):      # stream_reply と同じ（中を必ず閉じる）
            await asyncio.shield(pump.aclose())


async def _pump(prompt, options: ClaudeAgentOptions,
                record: Optional[tuple[dict, str]],
                state: Optional[dict] = None) -> AsyncIterator[tuple[str, object]]:
    """SDK を1回呼んで、届いたものを ("thinking"|"text"|"tool"|"result"…, 中身) で流す。

    record … (アカウントのつなぎ表, app_sid)。書き終えたら、その app_sid に CLI セッションを結ぶ。
             None＝どこにも結ばない（引き継ぎメモ）。
    state  … 事実の見張りと分け合う入れ物（2026-09-24・stream_reply だけが渡す）。
             返事の粒と道具の記録をここへ足し、見張りが止めたら書き直しの前に線を1回だけ流す。
             None＝今までと1文字も同じ動き（引き継ぎメモ）。
    ★2026-09-17 に stream_reply の中から切り出した。中身は1行も変えていない（結ぶ先を引数にしただけ）。
    """
    # ★口を変数で持つ（2026-09-01）。`async for` は途中で抜けても**イテレータを閉じない**
    #   （PEP 533 は見送られた。SDK の _internal/client.py に同じ注意書きがある）。
    #   閉じそこねると CLI のサブプロセス（claude.exe）が残り、CPU を握ったまま
    #   何日でも回り続ける。実際 2026-08-30 の2本が 49時間ぶん・39時間ぶん回し、
    #   ファンが鳴りっぱなしになった（このPCは2コアなので2本で半分埋まる）。
    agen = agent_query(prompt=prompt, options=options)
    # 粒（delta）が1つでも届いたか。届いているなら、あとから来る「まとまった塊」は
    # 同じ中身の二度目なので捨てる（そのまま流すと、返事が2回並ぶ）。
    got_think_bits = got_text_bits = False
    said_think = False                       # 考えたぶんを1文字でも流したか
    said_text = False                        # 返事を1文字でも流したか（塊で来た道の分）
    told_why = False                         # 打ち切りの訳を、もう日本語で伝えたか
    drew_line = False                        # 見張りの線を、もう流したか（1回だけ）
    try:
        async for msg in agen:
            if isinstance(msg, StreamEvent):
                # ★書けたそばから届く粒（2026-09-02）。中身は Anthropic の生の
                #   ストリームイベント。ここでは3つだけ見る：
                #     content_block_start … いま「考えはじめた／書きはじめた」の合図
                #     thinking_delta      … 考えたぶんの、ひとかけら
                #     text_delta          … 返事の、ひとかけら
                #   ※ 子（サブエージェント）のぶんは混ぜない。
                if msg.parent_tool_use_id:
                    continue
                ev = msg.event or {}
                kind = ev.get("type")
                if kind == "content_block_start":
                    t = (ev.get("content_block") or {}).get("type")
                    if t == "thinking":
                        yield "phase", "thinking"
                    elif t == "text":
                        yield "phase", "writing"
                        # ★見張りに止められたあとの書き直しの頭（2026-09-24）。線は state["text"] に入れない
                        if state is not None and state.get("blocked") and not drew_line:
                            drew_line = True
                            yield "text", GUARD_LINE
                    continue
                if kind != "content_block_delta":
                    continue
                d = ev.get("delta") or {}
                if d.get("type") == "thinking_delta" and d.get("thinking"):
                    got_think_bits = True
                    said_think = True
                    yield "thinking", d["thinking"]
                elif d.get("type") == "text_delta" and d.get("text"):
                    got_text_bits = True
                    if state is not None:
                        state["text"].append(d["text"])
                    yield "text", d["text"]
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ThinkingBlock):
                        if got_think_bits:
                            continue          # 粒で流しずみ
                        # 粒が来ない世代のための道。塊どうしは改行で分ける。
                        yield "thinking", (("\n" if said_think else "") + block.thinking)
                        said_think = True
                    elif isinstance(block, TextBlock):
                        if got_text_bits:
                            continue          # 粒で流しずみ
                        said_text = True
                        if state is not None:
                            if state.get("blocked") and not drew_line:
                                drew_line = True
                                yield "text", GUARD_LINE
                            state["text"].append(block.text)
                        yield "text", block.text
                    else:
                        ev = tool_event(block)
                        if state is not None:     # 見張りに「この回に実際に使った道具」を渡す
                            state["tools"].append({"name": ev.get("name"), "text": ev.get("text")})
                        yield "tool", ev
            elif isinstance(msg, ResultMessage):
                # ★「途中で打ち切られた」合図を、ここで拾って画面へ出す（2026-09-07）。
                #   それまでは金額とトークン数しか見ていなかったので、道具の回数を
                #   使い切って終わったときに**画面が空のまま**になっていました。
                #   ウェブ検索が3回止まった件の、もう半分の手当てです。
                note = _ended_early_note(msg)
                if note:
                    told_why = True
                    yield "text", (("\n\n" + note) if (got_text_bits or said_text) else note)
                u = msg.usage or {}
                if record is not None:
                    record[0][record[1]] = {"sdk_sid": msg.session_id}
                yield "result", {
                    "cost_usd": msg.total_cost_usd,
                    "session_id": msg.session_id,
                    "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens"),
                    "cache_read": u.get("cache_read_input_tokens"),
                    "cache_write": u.get("cache_creation_input_tokens"),
                }
    except Exception as e:                       # 落ちても画面を黙らせない
        # ★打ち切りのときは、SDK が「知らせ」を出したあとに**もう一度**例外を投げます
        #   （ResultError。中身は英語で「exit code: 1」）。すぐ上で日本語にして
        #   お伝えずみなので、ここでは重ねて出しません（2026-09-07）。
        if not told_why:
            yield "error", f"{type(e).__name__}: {e}"
    finally:
        # スマホが途中で切れても、ここで必ず claude.exe を終わらせる。
        # ★shield で包む：切られている最中は次の await がすぐ打ち切られるので、
        #   裸で書くと**片づけ自体が中断されて**サブプロセスが残る。
        # ★片づけで新しい例外は出さない（もう画面へは返せないので握りつぶす）。
        with contextlib.suppress(BaseException):
            await asyncio.shield(agen.aclose())


def needs_seed(app_sid: str, account: str = "sdk1") -> bool:
    """CLI セッションがまだ無い＝これまでの流れを添えて送りなおす必要があるか。"""
    return app_sid not in _sessions[_acct(account)]


def forget_session(app_sid: str) -> None:
    """スレッドを作り直したときに CLI セッションの対応も捨てる。"""
    for store in _sessions.values():
        store.pop(app_sid, None)
