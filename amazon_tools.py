# -*- coding: utf-8 -*-
"""クロード極 — Amazonの道具（2026-09-07 新設）

イメージのわかない品物も、カゴを通じて理解できる。
実務が、話に出た品物を**実物として**お見せするための道です。
言葉で説明するより、Amazonのカゴに入れてしまったほうが早い、という考えです。

★中身は1か所だけです。**お買い物のブラウザを持っている別のアプリ（shopping/）** に頼みます。
  ほかの呼び手も、まったく同じ口に頼んでいます。
  つまり **Amazonを開くブラウザは1つのまま** ＝ 取り合いになりません。
  その中では1本の糸（_ShopWorker）が順番に捌くので、同時に頼まれても並ぶだけです。
  ★shopping/ を直に import してはいけません（ブラウザの写しを2つ開いて壊れます）。

★スイッチが入のときだけ、この道具一式が載ります（ほかの切り替え式の道具と同じ形）。
  切のあいだは1本も載らないので、ふだんの会話の重さは1トークンも変わりません。

★**買う道具は1本も持たせていません。** ここにあるのは
    探す ／ 商品を見る ／ 口コミを読む ／ カゴに入れる
  の4本だけです。お会計へ進む口（/api/shop/review）・確定を押す口（/api/shop/confirm）・
  取り消す口（/api/shop/cancel）は **繋いでいません**。
  理由は2つ ──
    ①お金が動くところは、持ち主がご自分のAmazonでなさることだから
    ②その3本は「いま誰の会計か」を見分けないので、極から触ると
      **ほかの呼び手がお会計待ちの品を横取りして買ってしまう**ことがある（2026-09-07 に確かめました）
  カゴに入れるだけなら、その穴を踏みません（お買い物の係が、会計待ちのときは断ってくれます）。

★枠のこと：「探す」の1回だけ、お買い物係（Opus5・エクストラハイ）が2回動きます。
  そのぶんは、このパソコンのクロードの枠から引かれます（ほかの呼び手が探すときと同じ道・同じ枠）。
  「商品を見る」「口コミを読む」「カゴに入れる」は、ページを読むだけなので0円です。

★カゴに入れるときだけ、このパソコンの画面にAmazonの窓が開きます（Bot よけのため。
  ほかの呼び手が入れるときと同じ動きなので、新しく起きることではありません）。
"""
import asyncio
import json
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    AVAILABLE = True
except Exception as _e:                       # 部品が無くても会話は止めない
    AVAILABLE = False
    print(f"[amazon] Amazonの道具は使えません: {type(_e).__name__}: {_e}")


# お買い物のブラウザを持っている係の住所。
# ★ここを変えるときは、同じ口に頼むほかの呼び手の BASE も一緒に。
BASE = "http://127.0.0.1:8010"

# セールの見回りの控え（2026-09-10）。
# 見回りそのものは別のアプリの夜の見回り（sale_watch.run_daily）が1日1回やって、ここに書く。
# 極は、この控えを読むだけ（ブラウザも生成AIも使わない＝0円・すぐ返る）。
_SALE_FILE = Path(__file__).resolve().parent / "data" / "sale_watch.json"   # 無ければ「控えがありません」と返す
_SALE_KEEP_DAYS = 3        # 見回り側の KEEP_DAYS と同じ（これより古い値下がりは、もう新しくない）

SERVER_NAME = "amazon"

# 道具の名前（allowed_tools に書く形）。MCP の道具は "mcp__<サーバー名>__<道具名>"。
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{n}" for n in (
    "amazon_search", "amazon_product", "amazon_kuchikomi", "amazon_kago", "amazon_sale",
)]

# 画面に出す日本語の見出し（無いと生の英語名が出ます）
TOOL_LABELS = {
    f"mcp__{SERVER_NAME}__amazon_search": "Amazonで探しています",
    f"mcp__{SERVER_NAME}__amazon_product": "商品のページを見ています",
    f"mcp__{SERVER_NAME}__amazon_kuchikomi": "買った人の声を読んでいます",
    # ★この札は「道具を呼んだ瞬間」に出ます（入り終わった合図ではありません）。
    #   入れられなかったときも出るので、言い切らない言葉にしてあります。
    f"mcp__{SERVER_NAME}__amazon_kago": "カゴに入れています",
    f"mcp__{SERVER_NAME}__amazon_sale": "セールの見回りを読んでいます",
}
# 中身のどこを画面に見せるか
TOOL_FIELDS = {
    f"mcp__{SERVER_NAME}__amazon_search": "text",
    f"mcp__{SERVER_NAME}__amazon_product": "asin",
    f"mcp__{SERVER_NAME}__amazon_kuchikomi": "asin",
    f"mcp__{SERVER_NAME}__amazon_kago": "asins",
}
# 会話の中に残す道具＝カゴが動いたときだけ（記憶が動いたときと同じ扱い）。
# ★探す・読むは流れて消えてよいのですが、**カゴの中身が変わったこと**は
#   あとから見返せないと、持ち主が「何が入ったのか分からない」になります。
KEEP_IN_CHAT = frozenset({f"mcp__{SERVER_NAME}__amazon_kago"})


def _txt(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _sale_state():
    """控えを読む。無い・壊れているときは None。"""
    try:
        state = json.loads(_SALE_FILE.read_text(encoding="utf-8-sig"))
        return state if isinstance(state, dict) else None
    except Exception:
        return None


def _sale_lines(state: dict) -> list:
    """新しい知らせ（値下がり・また買える・大きいセール）を行にする。無ければ空。"""
    limit = (date.today() - timedelta(days=_SALE_KEEP_DAYS)).isoformat()
    news = [n for n in (state.get("news") or [])
            if isinstance(n, dict) and str(n.get("date", "")) >= limit]
    events = state.get("events") or []
    if not news and not events:
        return []
    lines = ["【お買い物の見張り】"]
    if events:
        lines.append(f"いま Amazon でやっているセール：{'・'.join(events)}")
    for item in news[:6]:
        title = " ".join(str(item.get("title") or "").split())[:40]
        where = item.get("where") or "カゴ"
        if item.get("kind") == "modotta":
            lines.append(f"{where}の「{title}」が、また買えるようになった（¥{item.get('now')}）")
        else:
            lines.append(f"{where}の「{title}」が ¥{item.get('high')} → ¥{item.get('now')}"
                         f"（{item.get('percent')}%引き）")
    return lines


def sale_briefing() -> str:
    """会話の頭に置く1枚（2026-09-23）。知らせが無ければ**1文字も返しません**。

    会話の頭に置くのは、この極だけにする。
    ★事実だけを書く（「こう言って」は書かない＝server/sale_watch.briefing と同じ作法）。"""
    state = _sale_state()
    return "\n".join(_sale_lines(state)) if state else ""


def sale_text() -> str:
    """セールの見回りの控えを、読める1枚にする（server/sale_watch.summary_text と同じ中身）。

    ★server/sale_watch.py を import しないのは、あちらが別の config を読むため
      （極の config と名前がぶつかる）。控えのファイルを直に読むだけにしている。"""
    state = _sale_state()
    if state is None:
        return "セールの見回りの控えが、まだありません（夜の見回りが1回も済んでいないようです）。"
    checked = state.get("checked_at") or ""
    lines = _sale_lines(state)
    if not lines:
        watching = state.get("watching")
        if watching:
            return (f"いまは大きいセールも、値下がりもありません。"
                    f"カゴと「あとで買う」の{watching}件を見張っています（最後に見たのは {checked}）。")
        return "まだ見回りに行けていないようです。"
    lines.append(f"（最後に見たのは {checked}）")
    return "\n".join(lines)


def _one_line(x: object, limit: int = 0) -> str:
    """改行と続いた空白をつぶして1行にする（Amazonの文字列は改行だらけのため）。"""
    text = " ".join(str(x or "").split())
    if limit and len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def _post_sync(path: str, payload: dict, timeout: int):
    """お買い物の係に頼む（同期）。返りは (通じたか, 中身, 困りごとの言葉)。"""
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE}{path}", data=body, method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
        return True, json.loads(raw.decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            data = json.loads(e.read().decode("utf-8"))
            detail = _one_line(data.get("detail"))
        except Exception:
            detail = ""
        # 409＝いま別のお買い物が動いている（順番待ち・ほかの呼び手のお会計など）
        if e.code == 409:
            return False, None, (detail or "いま別のお買い物が動いています。"
                                 "少し置いてから、もう一度だけ試す。")
        return False, None, detail or f"お買い物の係が困っています（HTTP {e.code}）。"
    except (urllib.error.URLError, TimeoutError):
        return False, None, ("お買い物の係につながりませんでした。"
                             "動いていないかもしれないので、持ち主に一声かける。")
    except Exception as e:
        return False, None, f"お買い物の係に頼めませんでした（{type(e).__name__}）。"


async def _post(path: str, payload: dict, timeout: int):
    """お買い物の係に頼む。★待つあいだ、極ぜんたいを止めないように別の糸で待つ。"""
    try:
        return await asyncio.to_thread(_post_sync, path, payload, timeout)
    except Exception as e:
        return False, None, f"お買い物の係に頼めませんでした（{type(e).__name__}）。"


def build_server():
    """Amazonの道具一式を、Agent SDK に渡せる形で返す。使えなければ None。"""
    if not AVAILABLE:
        return None

    @tool("amazon_search",
          "Amazonで品物を探す。text＝持ち主の言葉のまま（「爪切り 高いやつ」でよい）。"
          "★持ち主が『それ何？』『イメージがわかない』と言われた品物は、"
          "言葉で説明するより、これで探して**実物をお見せする**ほうが早い。"
          "見つかったものには asin（Amazonの品番）が付いてくるので、"
          "そのあと amazon_product・amazon_kuchikomi・amazon_kago に渡せる。"
          "★ここだけ、お買い物係（Opus5）が動くので枠を使う。"
          "同じものを何度も探し直さない。",
          {"text": str})
    async def _search(args):
        want = _one_line(args.get("text", ""))
        if not want:
            return _txt("何を探すのかが分かりませんでした。持ち主に聞き直す。")

        ok, data, err = await _post(
            "/api/shop/search", {"text": want, "max_results": 6},
            # 内訳＝検索語づくり(最長90) ＋ Amazonを読む(約35) ＋ 選ぶ(最長90)。
            # 呼び手側の待ちと同じにしてあります。
            timeout=240,
        )
        if not ok or not isinstance(data, dict):
            return _txt(err or "うまく探せませんでした。")

        if data.get("needs_closeup"):
            return _txt(_one_line(data.get("message")) or "写真が読めませんでした。")

        results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
        if not results:
            return _txt("Amazonでは見つかりませんでした。言い方を変えて、もう一度だけ探す。")

        lines = [f"「{_one_line(data.get('query')) or want}」で探した結果："]
        for r in results:
            star = _one_line(r.get("rating"))
            mark = "・プライム" if r.get("prime") else ""
            lines.append(
                f"- {_one_line(r.get('title'), 70)}"
                f"／{_one_line(r.get('price_text')) or '値段の表示なし'}"
                f"{('／' + star) if star else ''}{mark}"
                f"／asin={_one_line(r.get('asin'))}"
            )

        pick = data.get("pick")
        if isinstance(pick, dict):
            main = pick.get("main")
            if isinstance(main, dict):
                lines.append("")
                lines.append(
                    f"お買い物係の本命：{_one_line(main.get('title'), 70)}"
                    f"（{_one_line(main.get('price_text'))}"
                    f"・asin={_one_line(main.get('asin'))}）"
                )
            reason = _one_line(pick.get("reason"))
            if reason:
                lines.append(f"選んだわけ：{reason}")
            for note in (pick.get("notes") or [])[:5]:
                if _one_line(note):
                    lines.append(f"気づいたこと：{_one_line(note)}")

        lines.append("")
        lines.append("（ここまでは調べただけ。カゴにはまだ何も入っていない）")
        return _txt("\n".join(lines))

    @tool("amazon_product",
          "商品のページを読んで、選べる色・サイズ・素材を出す（買わない・カゴにも触らない）。"
          "asin＝amazon_search が返した品番。"
          "★カゴに入れる前に、色やサイズが選べる品なら、ここで確かめてからにする。0円で読める。",
          {"asin": str})
    async def _product(args):
        asin = _one_line(args.get("asin", ""))
        if not asin:
            return _txt("どの商品か分かりませんでした（asin が要ります）。")

        ok, data, err = await _post("/api/shop/product", {"asin": asin}, timeout=90)
        if not ok or not isinstance(data, dict) or data.get("ok") is not True:
            return _txt(err or "その商品のページを読めませんでした。")

        lines = [_one_line(data.get("title"), 80) or asin]
        dims = data.get("dimensions") or {}
        for label, vals in dims.items():
            if vals:
                lines.append(f"{label}：{'／'.join(_one_line(v) for v in vals)}")
        material = _one_line(data.get("material"))
        if material:
            lines.append(f"素材：{material}")
        if not dims and not material:
            lines.append("色やサイズの選べる種類は無さそう（単品）")
        return _txt("\n".join(lines))

    @tool("amazon_kuchikomi",
          "買った人の口コミを読む（読むだけ・カゴにも触らない・0円）。asin＝品番。"
          "★持ち主が『使い心地は？』『よさそう？』と気にされたら、"
          "思い込みで答えずに、これを読んでから事実で答える。",
          {"asin": str})
    async def _kuchikomi(args):
        asin = _one_line(args.get("asin", ""))
        if not asin:
            return _txt("どの商品の口コミか分かりませんでした（asin が要ります）。")

        ok, data, err = await _post(
            "/api/shop/kuchikomi", {"asin": asin, "max_reviews": 8}, timeout=120)
        if not ok or not isinstance(data, dict) or data.get("ok") is not True:
            return _txt(err or "その商品の口コミを読めませんでした。")

        lines = [_one_line(data.get("title"), 60) or asin]
        star = _one_line(data.get("rating"))
        total = _one_line(data.get("total"))
        if star or total:
            lines.append(f"みんなの評価：{star} {total}".strip())
        highlights = _one_line(data.get("highlights"))
        if highlights:
            lines.append(f"よく言われていること：{highlights}")

        reviews = [r for r in (data.get("reviews") or []) if isinstance(r, dict)]
        if not reviews:
            lines.append("口コミの本文はまだ載っていない（星の数だけ）")
        for r in reviews:
            mark = "・買った人" if r.get("verified") else ""
            lines.append(
                f"── {_one_line(r.get('stars'))} {_one_line(r.get('title'))}{mark}".rstrip())
            lines.append(_one_line(r.get("body")))
        lines.append("（ここまでが、買った人の言葉そのまま）")
        return _txt("\n".join(lines))

    @tool("amazon_kago",
          "Amazonのカゴに入れておく（**買わない**・お会計にも進まない・0円）。"
          "asins＝品番。1つでも、読点やカンマで区切って何個でも。"
          "色やサイズを選ぶ品なら color／size も渡す（1つのときだけ効く）。"
          "★これが、この道具一式のいちばん大事な1本。"
          "言葉では伝わらない品物も、カゴに入れておけば、持ち主がAmazonのアプリを開いて"
          "**実物として**ご覧になれる。『どんなものか分からない』と言われたら、"
          "探して、カゴに入れて、『カゴに入れておきました』とお伝えする。"
          "★1円も動かない。買うのは持ち主が自分でする。アシスタントは買えないし、買わない。",
          {"asins": str, "color": str, "size": str})
    async def _kago(args):
        wanted = [a.strip() for a in re.split(r"[,、\s]+", args.get("asins") or "")
                  if a.strip()]
        if not wanted:
            return _txt("どれをカゴに入れるのか分かりませんでした（asin が要ります）。")

        items = [{"asin": a} for a in wanted]
        if len(items) == 1:
            color = _one_line(args.get("color", ""))
            size = _one_line(args.get("size", ""))
            if color:
                items[0]["color"] = color
            if size:
                items[0]["size"] = size

        ok, data, err = await _post("/api/shop/kago", {"items": items}, timeout=240)
        if not ok or not isinstance(data, dict):
            return _txt(err or "カゴに入れられませんでした。")

        if data.get("variant_not_found"):
            dims = data.get("dimensions") or {}
            opts = "／".join(
                f"{k}：{'・'.join(_one_line(x) for x in v)}" for k, v in dims.items() if v)
            if opts:
                return _txt(f"その色・サイズは見つかりませんでした。選べるのは（{opts}）。"
                            "持ち主に、どれにするか聞く。")
            return _txt("その色・サイズは見つかりませんでした。持ち主に聞く。")

        if data.get("ok") is not True:
            return _txt("カゴに入れられませんでした。")

        added = [x for x in (data.get("added") or []) if isinstance(x, dict)]
        names = [_one_line(x.get("title"), 50) for x in added if x.get("title")]
        lines = [f"カゴに入れました：{'、'.join(names)}" if names
                 else f"カゴに{len(added)}つ入れました"]

        count = data.get("cart_count")
        if isinstance(count, int):
            lines.append(f"いまカゴには{count}つ入っています")

        failed = [x for x in (data.get("failed") or []) if isinstance(x, dict)]
        if failed:
            lines.append(f"{len(failed)}つは入れられませんでした")

        lines.append("持ち主がAmazonのアプリでカゴを開くと、実物を見られます"
                     "（買うのは持ち主。ここでは1円も動いていません）")
        return _txt("\n".join(lines))

    @tool("amazon_sale",
          "セールの見回りの控えを読む（ブラウザも開かない・0円・すぐ返る）。"
          "いま Amazon で大きいセールをやっているか、持ち主のカゴや「あとで買う」の品が"
          "安くなっていないか、また買えるようになっていないかが分かる。"
          "見回りは毎晩1回、係が勝手にやっている。持ち主に「セールある？」「安くなってない？」と"
          "聞かれたとき、お買い物の話の途中で値段が気になったときに読む。",
          {})
    async def _sale(args):
        return _txt(sale_text())

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        tools=[_search, _product, _kuchikomi, _kago, _sale],
    )
