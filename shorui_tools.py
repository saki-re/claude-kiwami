# -*- coding: utf-8 -*-
"""クロード極 — 書類を読む道具（2026-09-07 新設）

★道具の中身は `shorui/reader.py` の1か所だけです。ここは、それを
  Agent SDK が呼べる形に包んでいるだけ。別のアプリ側の包みも
  同じ中身を呼ぶので、**どちらに頼んでも同じ答えが返ります**。

★何のためにあるか
  `WebFetch` は PDF を**取ってはくる**のに、圧縮されたまま渡すので中身が読めません。
  2026-09-07、ある市の冊子の PDF で実際に詰まりました。ここはその受け皿です。

★つまみは増やしません。**ウェブ検索のつまみと同じ**に載せてあります
  （探す → 開く → 読む、が一続きになるので、持ち主が新しいつまみを覚えずに済みます）。
"""
import asyncio
import sys
from pathlib import Path

# shorui/ を読めるようにする。極は claude/ から起動するので、隣を指す。
_SHORUI = Path(__file__).resolve().parent / "shorui"
if str(_SHORUI) not in sys.path:
    sys.path.insert(0, str(_SHORUI))

AVAILABLE = False
try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    import reader                                   # shorui/reader.py
    AVAILABLE = True
except Exception as _e:                             # 部品が無くても会話は止めない
    reader = None
    print(f"[shorui] 書類の道具は使えません: {type(_e).__name__}: {_e}")


SERVER_NAME = "shorui"

TOOL_NAMES = [f"mcp__{SERVER_NAME}__shorui_read"]

TOOL_LABELS = {f"mcp__{SERVER_NAME}__shorui_read": "書類を読んでいます"}
TOOL_FIELDS = {f"mcp__{SERVER_NAME}__shorui_read": "url"}
KEEP_IN_CHAT = frozenset()                          # 読むだけなので、会話には残しません


def build_server():
    """書類の道具を、Agent SDK に渡せる形で返す。使えなければ None。"""
    if not AVAILABLE:
        return None

    @tool(reader.TOOL_NAME, reader.TOOL_DESCRIPTION, reader.TOOL_SCHEMA)
    async def _read(args):
        url, page = reader.tool_args(args)
        try:
            # ★別の糸で待つ＝取ってくるあいだ、極ぜんたいを止めない。
            text = await asyncio.to_thread(reader.read_document, url, page)
        except Exception as e:
            text = f"その書類は読めませんでした（{type(e).__name__}）。"
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name=SERVER_NAME, tools=[_read])
