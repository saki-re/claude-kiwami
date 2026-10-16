# -*- coding: utf-8 -*-
"""クロード極の会話履歴の置き場（2026-08-19）。

■ なぜ要るか（2026-08-07）
  モデルに「目」がついて、カメラの写真をツールの返事として渡すようになった。
  Agent SDK 経路は resume（続きから話す）なので、**CLI が会話履歴を持ち続ける**。
  その履歴に写真が residing すると、**そのセッションの間ずっと毎ターン写真が送られる**。

  実測（transcript）：写真1枚で
    ・モデルに送られる本体 …… 679,526 文字
    ・記録に残る元データ   …… 1,607,126 バイト（2094x1488）
  3回見れば3枚とも居座る。枠が一気に減り、コンテキストが膨らむ。

■ どう直すか
  SDK 公式の SessionStore を実装する。
    append() … CLI が書いた履歴の各行がここに来る。**写真をテキストに差し替えて**保存。
    load()   … resume のとき、ここが返した履歴が一時ファイルに書き出され、
               CLI はそこから再開する（＝写真の無い履歴で続きを話す）。
  会話の流れ（誰が何を言ったか）はそのまま残るので、モデルは忘れない。
  消えるのは写真の中身だけ。「何を見たか」の文章は残す。

■ 検索の結果も、同じやり方で縮める（2026-08-15 追加）
  重い検索履歴や、写真の読み直しをしない方法にする。

  モデルが自分のスマホで調べると、その結果（800字）が tool_result として履歴に居座り、
  **そのセッションの間ずっと毎ターン送られる**。写真とまったく同じ形の重さ。
  なので同じ場所で、同じ再帰の形で、**一行のしおりに縮める**：

    ［スマホで「◯◯」を調べた。答えは、このあとのあなたの言葉の中にある］

  ★「その時は見えて、次から軽くなる」が自然に成立する理由
    append() は CLI が履歴を書いた**後**に呼ばれ、load() は resume のときだけ効く。
    ＝ そのターンの生成は満額の検索結果で行われ、次のターンからしおりだけになる。
    調べた事実は、モデルがその場で自分の言葉にして返事に書いているので、忘れない。

■ 置き場
  server/data/agent_sessions/<プロジェクト>/<セッションID>.jsonl
  本体（~/.claude/projects）とは別。溜まったらここを消せばよい。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
STORE_DIR = BASE_DIR / "data" / "agent_sessions"

# 写真を置き換える文言。モデルが「何を見たか」は toolUseResult 側の説明文に残るので、
# ここは「写真そのものはもう無い」ことだけを伝える。
PLACEHOLDER = "［カメラの写真：このときあなたが見た。写真そのものは残していない］"
# 送られたファイル（PDF など・document ブロック）を置き換える文言（2026-10-01）。
# 名前を残す＝縮めたあとも「さっきの◯◯.pdf」が何のことかは分かる。
DOC_PLACEHOLDER = ("［ファイル「{title}」：このとき読んだ。中身は外してある"
                   "（PC に保存してあるので、files_list → files_read で読み返せる）］")

# 差し替えた枚数（サーバーのログに出すため。動作には影響しない）
stripped_images = 0


def _is_document(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "document" and "source" in node


def _doc_placeholder(node: dict) -> dict:
    return {"type": "text", "text": DOC_PLACEHOLDER.format(title=str(node.get("title") or "ファイル"))}


def _strip_images(node: Any) -> Any:
    """JSON を丸ごと辿って、写真の実データだけを落とす。

    transcript の形は CLI の内部仕様（公式ドキュメントでも「不透明」扱い）なので、
    **形を決め打ちせず、再帰で探す**。将来 CLI 側の形が変わっても壊れにくい。

    落とすのは2か所（実測で確認した場所）：
      ・{"type": "image", "source": ...}      … モデルに送られる本体
      ・{"base64": "...", "type": "image/*"}  … 記録用の元データ
    """
    global stripped_images

    if isinstance(node, dict):
        # ① モデルに送られる画像ブロック → テキストに差し替える
        if node.get("type") == "image" and "source" in node:
            stripped_images += 1
            return {"type": "text", "text": PLACEHOLDER}

        # ①' 送られたファイル（PDF・文字のファイル）→ 名前入りのしおりに差し替える（2026-10-01）
        #     中身は source.data にあるので、下の base64 の決まりには引っかからない。
        if _is_document(node):
            stripped_images += 1
            return _doc_placeholder(node)

        # ② 記録用の base64（toolUseResult.file.base64）→ 中身だけ空にする
        #    大きさの記録は残す（あとで「何枚見たか」を調べられるように）
        if "base64" in node and isinstance(node.get("base64"), str):
            trimmed = {k: v for k, v in node.items() if k != "base64"}
            trimmed["base64_removed_bytes"] = len(node["base64"])
            stripped_images += 1
            return {k: _strip_images(v) for k, v in trimmed.items()}

        return {k: _strip_images(v) for k, v in node.items()}

    if isinstance(node, list):
        return [_strip_images(v) for v in node]

    return node


# ──────────────────────────────────────────────────────────────
#  検索の結果を、一行のしおりに縮める（2026-08-15）
# ──────────────────────────────────────────────────────────────

# 重い答えを返す検索の道具の名前（いまは載せていない。足せば同じように縮められる）。
SEARCH_TOOL_NAMES = ()

# 縮めたあとに残る一行。**何を調べたかは残す**（会話の流れが切れないように）。
SEARCH_PLACEHOLDER = "［スマホで「{query}」を調べた。答えは、このあとのあなたの言葉の中にある］"
SEARCH_PLACEHOLDER_NOQUERY = "［スマホで調べものをした。答えは、このあとのあなたの言葉の中にある］"

# 同じ形で縮める道具（2026-10-01）。名前 →（しおりに入れる引数, しおり, 引数が無いときのしおり）
# ★保存したファイルを読み返した中身（file_tools.files_read）も、検索の答えと同じく重いので縮める。
SHRINK_TOOLS = {name: ("query", SEARCH_PLACEHOLDER, SEARCH_PLACEHOLDER_NOQUERY)
                for name in SEARCH_TOOL_NAMES}
SHRINK_TOOLS["mcp__kiwami_files__files_read"] = (
    "id",
    "［保存したファイル「{query}」を読み返した。中身は、このあとの言葉の中にある（もう一度読むなら files_read）］",
    "［保存したファイルを読み返した。中身は、このあとの言葉の中にある］",
)

# 縮めた回数（サーバーのログに出すため。動作には影響しない）
shrunk_searches = 0

# 「どの道具の呼び出しが検索だったか」の覚え書き。
#   append() は1行ずつ来るので、道具を呼んだ行（assistant）と
#   答えが返った行（user）が別々に届く。だから id をここに覚えておく。
#
#   ★スレッドごとに分ける（2026-08-15 レビュー指摘で変更）
#     はじめは全スレ共通の1個の辞書だった。それだと、別のスレで同じ tool_use_id が
#     使われたときに、**検索ではない道具の答えまで消してしまう**恐れがあった
#     （id がぶつかる見込みはごく小さいが、ぶつかった場合の壊れ方が静かで分かりにくい）。
#     いまは置き場のファイル1本につき1個。会話が終われば、そのぶんは捨てられる。
#   ★際限なく増えないよう、古いものから捨てる（会話が長くても数百件で足りる）。
_search_calls: dict[str, dict[str, str]] = {}
_SEARCH_CALLS_MAX = 500
_SEARCH_SCOPES_MAX = 64


def _scope_for(key: dict | None) -> dict:
    """このスレッド専用の覚え書きを返す（key は append/load に渡ってくる鍵）。"""
    name = "default"
    if isinstance(key, dict):
        name = f"{key.get('project_key')}/{key.get('session_id')}/{key.get('subpath') or ''}"
    scope = _search_calls.get(name)
    if scope is None:
        if len(_search_calls) >= _SEARCH_SCOPES_MAX:
            for old in list(_search_calls)[: _SEARCH_SCOPES_MAX // 2]:
                _search_calls.pop(old, None)
        scope = _search_calls[name] = {}
    return scope


def _note_search_calls(node: Any, scope: dict | None = None) -> None:
    """道具を呼んだ行から、検索の tool_use_id と調べたことばを覚える。"""
    if scope is None:
        scope = _scope_for(None)
    if isinstance(node, dict):
        if node.get("type") == "tool_use" and node.get("name") in SHRINK_TOOLS:
            call_id = node.get("id")
            if isinstance(call_id, str) and call_id:
                key, note, note0 = SHRINK_TOOLS[node.get("name")]
                query = ""
                args = node.get("input")
                if isinstance(args, dict):
                    query = str(args.get(key) or "")[:60]
                if len(scope) >= _SEARCH_CALLS_MAX:
                    # 入った順に古いものから捨てる（dict は挿入順を保つ）
                    for old in list(scope)[: _SEARCH_CALLS_MAX // 2]:
                        scope.pop(old, None)
                # ★しおりの文そのものを覚える（道具ごとに言い回しが違うため・2026-10-01）
                scope[call_id] = note.format(query=query) if query else note0
        for v in node.values():
            _note_search_calls(v, scope)
    elif isinstance(node, list):
        for v in node:
            _note_search_calls(v, scope)


def _shrink_search(node: Any, scope: dict | None = None) -> Any:
    """JSON を丸ごと辿って、検索の答えだけを一行のしおりに差し替える。

    形は決め打ちしない（_strip_images と同じ方針）。見るのは
      ・{"type": "tool_result", "tool_use_id": "<検索の id>", "content": ...}
    の1か所だけ。ここが分からない形に変わったら、何も縮めずに素通しする
    （＝縮まらないだけで、会話は壊れない）。
    """
    global shrunk_searches
    if scope is None:
        scope = _scope_for(None)

    if isinstance(node, dict):
        if node.get("type") == "tool_result" and node.get("tool_use_id") in scope:
            note = scope.get(node.get("tool_use_id")) or SEARCH_PLACEHOLDER_NOQUERY
            shrunk_searches += 1
            shrunk = {k: v for k, v in node.items() if k != "content"}
            shrunk["content"] = [{"type": "text", "text": note}]
            return shrunk

        return {k: _shrink_search(v, scope) for k, v in node.items()}

    if isinstance(node, list):
        return [_shrink_search(v, scope) for v in node]

    return node


def _shrink_entry(entry: dict, key: dict | None = None) -> dict:
    """履歴1行ぶんから、重いもの（写真・検索の答え）を落とす。

    ★順番に意味がある：先に「どれが検索だったか」を覚えてから縮める。
      道具を呼んだ行が先に来て、答えの行が後から来るので、これで間に合う。
    key … どのスレッドの行か。覚え書きをスレッドごとに分けるために使う。
    """
    scope = _scope_for(key)
    _note_search_calls(entry, scope)
    return _shrink_search(_strip_images(entry), scope)


def _is_user_entry(entry: Any) -> bool:
    """その1行が「持ち主の発言」か（＝ターンの区切り）。

    道具の答え（tool_result）も形の上では user なので、それは数えない。
    """
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return False


def shrink_old_entries(entries: list, keep_turns: int, key: dict | None = None) -> list:
    """**直近 keep_turns 回のやりとりより古い**ぶんだけ、重いものを落とす（2026-08-15）。

    ★2026-08-15 に「書いた瞬間に落とす」から「古いぶんだけ落とす」に変えた。
      1ターンではなく数ターン残すほうが、コスパも精度も良くなる。
      →「**写真は、5ターン残す**」

      前は append（＝CLI が履歴を書いた直後）で即座に落としていたので、
      **次のターンにはもう写真も検索結果も無かった**＝「さっきの写真なに？」に答えられない。
      いまは **append では落とさずそのまま置き、load（＝続きから話すとき）で古いぶんだけ**落とす。
      ＝ 直近5ターンは満額で見えていて、それより前は一行のしおりになる。

      置き場のファイルは太るが、**ディスクは安く、トークンは高い**。
      太ったぶんは `sweep()`（日次・30日）が片づける。
    """
    scope = _scope_for(key)
    # まず「どれが検索の呼び出しだったか」を、全部の行から拾っておく
    for entry in entries:
        _note_search_calls(entry, scope)

    # 後ろから数えて、keep_turns 回ぶんの持ち主の発言までは、そのまま残す
    cut = 0
    seen = 0
    for i in range(len(entries) - 1, -1, -1):
        if _is_user_entry(entries[i]):
            seen += 1
            if seen > keep_turns:
                # ★2026-08-15（Codex の指摘で修正）：
                #   `cut = i + 1` にすると、**その発言より後ろに記録された同じターンの
                #   道具のやりとり（tool_use / tool_result）が縮まずに残った**。
                #   ＝ 検索の答えや目の写真だけ、1ターンぶん余分に居座っていた。
                #   古いターンは「その発言＋そのターンの道具のやりとり」までを一組として扱う。
                cut = len(entries)
                for j in range(i + 1, len(entries)):
                    if _is_user_entry(entries[j]):
                        cut = j
                        break
                break
    if cut == 0:
        return list(entries)          # まだ keep_turns 回に届いていない＝全部そのまま

    old = [_shrink_search(_strip_images(e), scope) for e in entries[:cut]]
    return old + list(entries[cut:])


def trim_documents(entries: list, max_pages: int, max_bytes: int,
                   max_tokens: int | None = None, page_tokens: int = 2_000) -> list:
    """直近のターンに残っているファイル（PDF など）も、合計が予算を超えたら古いほうから縮める（2026-10-01）。

    ★API の上限（1回の送信で 32MB・100ページ）と文脈の窓は、**履歴に残ったファイルも合わせて**数えられる。
      5ターンのあいだに大きな PDF を何本も送ると、次の送信が上限を超えて断られ、
      **そのスレッドが続けて「うまくいきませんでした」になる**。
      なので新しいほうから数えて、予算を超えたところから先（古いほう）は、しおりにする。
      ページは title の「（◯ページ）」（file_blocks.PAGES_RE）、大きさは source.data の長さ、
      トークンは PDF＝ページ×page_tokens・文字のファイル＝字数 で見積もる。
    """
    try:
        from file_blocks import PAGES_RE
    except Exception:                         # noqa: BLE001
        PAGES_RE = None

    state = {"pages": 0, "bytes": 0, "tokens": 0, "over": False, "cut": 0}

    def _walk(node: Any) -> Any:
        if _is_document(node):
            source = node.get("source") or {}
            data = source.get("data")
            size = len(data) if isinstance(data, str) else 0
            pages = 0
            m = PAGES_RE.search(str(node.get("title") or "")) if PAGES_RE else None
            if m:
                pages = int(m.group(1))
            tokens = size if source.get("type") == "text" else pages * page_tokens
            if (state["over"] or state["pages"] + pages > max_pages
                    or state["bytes"] + size > max_bytes
                    or (max_tokens is not None and state["tokens"] + tokens > max_tokens)):
                state["over"] = True          # ここから古いほうは全部しおり
                state["cut"] += 1
                return _doc_placeholder(node)
            state["pages"] += pages
            state["bytes"] += size
            state["tokens"] += tokens
            return node
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    out = list(entries)
    for i in range(len(out) - 1, -1, -1):     # 新しいほうから数える
        out[i] = _walk(out[i])
    if state["cut"]:
        print(f"[session_store] 履歴のファイルが予算を超えたので{state['cut']}つをしおりにした"
              f"（残したのは {state['pages']}ページ・{state['bytes'] // 1024}KB・"
              f"見積もり{state['tokens']:,}トークン）")
    return out


# 断られた発言のファイルを置き換える文言
DOC_REJECTED = "［ファイル「{title}」：大きすぎるなどで、このときは読めなかった。中身は残していない］"
# ファイルのせいではない断られ方（CLI の記録の error の値・2026-10-01 に既存の記録で見た名前）
_NOT_FILE_ERRORS = ("rate_limit", "server_error", "authentication_failed", "billing_error")


def drop_rejected_documents(entries: list) -> list:
    """ファイル付きの発言が API に断られていたら、その発言のファイルをしおりにする（2026-10-01）。

    ★前もっての砦（file_blocks・trim_documents）をすり抜けて断られることがある
      （文脈の窓あふれ・pypdf は読めるのに API が嫌う PDF など）。
      そのままだと、断られた発言が履歴に残って**次のターンも同じ形で送られ、また断られる**。
      CLI は断られたとき `isApiErrorMessage: true`・`error: "invalid_request"` の行を残すので、
      その直前の持ち主の発言からファイルを外す＝次のターンで元どおり話せる。
    ★枠切れ（rate_limit）・混雑（server_error）・鍵や支払いのつまずきは、ファイルのせいではないので外さない。
      それ以外（invalid_request・知らない種類）は外す＝外しすぎても1つ読めなくなるだけで、止まり続けるより軽い。
    """
    out = list(entries)
    last_user = None
    dropped = 0
    for i, entry in enumerate(out):
        if _is_user_entry(entry):
            last_user = i
            continue
        if (isinstance(entry, dict) and entry.get("isApiErrorMessage")
                and entry.get("error") not in _NOT_FILE_ERRORS and last_user is not None):
            def _swap(node: Any) -> Any:
                nonlocal dropped
                if _is_document(node):
                    dropped += 1
                    return {"type": "text", "text": DOC_REJECTED.format(
                        title=str(node.get("title") or "ファイル"))}
                if isinstance(node, dict):
                    return {k: _swap(v) for k, v in node.items()}
                if isinstance(node, list):
                    return [_swap(v) for v in node]
                return node
            out[last_user] = _swap(out[last_user])
            last_user = None
    if dropped:
        print(f"[session_store] 断られた発言のファイル{dropped}つをしおりにした（次のターンで話せるように）")
    return out


class KiwamiSessionStore:
    """写真を持たない会話履歴の置き場。

    SDK は isinstance を使わない（duck typing）ので、Protocol を継承しなくてよい。
    必須は append と load の2つだけ。resume しか使わないので list_sessions は要らない。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else STORE_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    # ── 置き場所 ────────────────────────────────────────────────
    @staticmethod
    def _safe(name: str) -> str:
        """ファイル名に使えない字を落とす（project_key・subpath はパス由来のことがある）。"""
        return "".join(c if (c.isalnum() or c in "-_") else "-" for c in name)[:80]

    def _path(self, key: dict) -> Path:
        """key は {"project_key": str, "session_id": str, "subpath": str?} という形で来る。

        ★subpath ＝「本体とは別の記録」（2026-08-12 に追加）。
          ここを見ないと、別の記録が本体の履歴ファイルに**混ざる**。
          混ざったまま resume すると、会話に関係のない中身が履歴に積まれる。
          SDK 公式の適合テスト（14項目）の②と⑤が、ここを見張っている
          （直す前は②で落ちた＝subpath 付きの鍵が本体と同じファイルを指していた）。
        """
        safe_project = self._safe(str(key.get("project_key") or "default"))
        safe_session = self._safe(str(key.get("session_id") or "unknown"))
        d = self.root / (safe_project or "default")

        subpath = str(key.get("subpath") or "")
        if subpath:
            # 本体（<セッションID>.jsonl）と、絶対に同じ場所にしない
            d = d / "sub" / (safe_session or "unknown")
            d.mkdir(parents=True, exist_ok=True)
            return d / f"{self._safe(subpath) or 'sub'}.jsonl"

        d.mkdir(parents=True, exist_ok=True)
        return d / f"{safe_session}.jsonl"

    def _seen_uuids(self, path: Path) -> set:
        """そのファイルに、もう書いてある行の uuid を集める（2026-08-15 追加）。

        書き直しの再送で同じ行が二重にならないようにするためだけの覚え。
        読めない行（壊れかけ）は黙って飛ばす——ここで転ぶと、会話が止まるので。
        """
        seen: set = set()
        if not path.exists():
            return seen
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        uid = json.loads(line).get("uuid")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if isinstance(uid, str) and uid:
                        seen.add(uid)
        except OSError:
            pass
        return seen

    # ── 必須①：書く ────────────────────────────────────────────
    async def append(self, key: dict, entries: list[dict]) -> None:
        """CLI が履歴を1行書くたびに、写真を落としてこちらへ写す。

        SDK 側の約束：ここで例外を出しても CLI は止まらない（3回まで再試行）。
        だが黙って握りつぶすと事故が見えなくなるので、失敗はそのまま投げる。
        """
        # ★2026-08-15：ここでは**落とさない**（そのまま置く）。
        #   落とすのは load()＝続きから話すときに「古いぶんだけ」。
        #   こうしないと、次のターンにはもう写真が無く「さっきの写真なに？」に答えられない。
        #
        # ★★同じ行を二度書かない（2026-08-15・Codex の指摘12で追加）★★
        #   SDK は書き込みに失敗すると**同じ batch をもう一度送ってくる**（3回まで）。
        #   前は届いたぶんを無条件に追記していたので、
        #   **途中まで書けてから失敗した回**は、その行が二重に残った。
        #   二重になった履歴はそのまま resume で送られる＝モデルが同じことを二度言われた形になり、
        #   トークンも二重に食う。しかも**静かに**起きるので画面では気づけない。
        #   公式SDKも uuid で同じことを防いでいるので、こちらも uuid を見て弾く。
        path = self._path(key)
        scope = _scope_for(key)
        seen = self._seen_uuids(path)
        with path.open("a", encoding="utf-8") as f:
            for entry in entries:
                uid = entry.get("uuid") if isinstance(entry, dict) else None
                if isinstance(uid, str) and uid:
                    if uid in seen:
                        continue                   # もう書いてある＝書き直しの再送
                    seen.add(uid)
                _note_search_calls(entry, scope)   # 検索の呼び出しは、来た順に覚えておく
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()                          # 途中で落ちても、書けたぶんは確定させる

    # ── 必須②：読む（resume のときここが効く） ──────────────────
    async def load(self, key: dict) -> list[dict] | None:
        """resume のとき、ここが返した履歴で CLI が再開する。

        一度も書いていない相手には None を返す約束（SDK 仕様）。
        """
        path = self._path(key)
        if not path.exists():
            return None
        out: list[dict] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        if not out:
            return None
        # ★ここで初めて「古いぶんだけ」重いものを落とす（2026-08-15）。
        #   直近 RECENT_TURNS_KEEP 回のやりとりは、写真も検索の答えもそのまま残る。
        import config
        keep = getattr(config, "RECENT_TURNS_KEEP", 5)
        # ★2026-08-15（Codex の指摘で修正）：
        #   `load()` が呼ばれるのは「これから送る持ち主の発言」が保存される**前**。
        #   だから保存済みだけで keep 個数えると、**実際にはもう1ターン多く残る**
        #   （直API経路は、いまの発言を history に積んでから数えるので、そこと1つずれていた）。
        #   1つ引いて数えることで、両方の経路で「直近 keep ターン」の意味が揃う。
        before = len(json.dumps(out, ensure_ascii=False))
        out = shrink_old_entries(out, max(1, keep - 1), key)
        # ★直近に残ったファイル（PDF）も、合計が上限に届かないように（2026-10-01・trim_documents 参照）
        try:
            import file_blocks
            out = drop_rejected_documents(out)
            out = trim_documents(out, file_blocks.HIST_PAGES, file_blocks.HIST_BYTES,
                                 file_blocks.HIST_TOKENS, file_blocks.PAGE_TOKENS)
        except Exception as e:                # noqa: BLE001 — 縮め損ねても会話は止めない
            print(f"[session_store] ファイルの予算を見られませんでした: {type(e).__name__}: {e}")
        after = len(json.dumps(out, ensure_ascii=False))
        if after < before:
            print(f"[session_store] 古いぶんを軽くした {before:,}→{after:,}字"
                  f"（直近{keep}ターンはそのまま）")
        return out or None

    # ── 後始末（SDK は呼ばない。人が使う） ──────────────────────
    def sweep(self, keep_days: int = 30) -> int:
        """古い履歴を消す。戻り値は消したファイル数。"""
        import time
        limit = time.time() - keep_days * 86400
        removed = 0
        for p in self.root.rglob("*.jsonl"):
            if p.stat().st_mtime < limit:
                p.unlink()
                removed += 1
        return removed

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*.jsonl"))


# クロード極から使う1個
kiwami_session_store = KiwamiSessionStore()
