# -*- coding: utf-8 -*-
"""クロード極 — 送ってもらったファイルを読み返す道具（2026-10-01 新設）

送られたファイルをPCに保存し、道具であとから読み返せるようにする。

■ 流れ
  送られたファイルは、届いたときに file_blocks.save_files が claude/data/files/日付/ に置く。
  そのターンは document ブロックで丸ごと読み、5ターンたつと履歴から中身が外れる。
  ★それより後（別のスレッドでも）は、この道具で置き場から読み返す。
    files_list … 置いてあるファイルの一覧（新しい順・名前で絞れる）
    files_read … 1つ読む。PDF はページを指定して文字で読む。文字の無いページ（スキャンした PDF）は、
                 ページに入っている絵をそのまま見せる。文字のファイルは、続きの字から読める。

■ つまみは付けない（いつも載せる）
  2本だけで軽い。持ち主が「つまみを入れてから頼む」を覚えなくて済む。

■ 重さ
  1回に返すのは READ_CHARS 字・絵 READ_IMAGES 枚まで（足りなければ「続きは◯ページから」と添える）。
  読んだ中身は直近5ターンだけ履歴に残り、それより前は session_store が1行のしおりに縮める
  （session_store.SHRINK_TOOLS に files_read を登録してある）。
"""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path

import file_blocks

AVAILABLE = False
try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    AVAILABLE = True
except Exception as _e:                             # 部品が無くても会話は止めない
    print(f"[file_tools] 読み返す道具は使えません: {type(_e).__name__}: {_e}")

try:
    from PIL import Image
    PIL_OK = True
except Exception:                                   # noqa: BLE001
    Image = None                                    # type: ignore[assignment]
    PIL_OK = False

SERVER_NAME = "kiwami_files"
LIST_NAME = "files_list"
READ_NAME = "files_read"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{LIST_NAME}", f"mcp__{SERVER_NAME}__{READ_NAME}"]
TOOL_LABELS = {
    f"mcp__{SERVER_NAME}__{LIST_NAME}": "保存したファイルを探しています",
    f"mcp__{SERVER_NAME}__{READ_NAME}": "保存したファイルを読んでいます",
}
TOOL_FIELDS = {
    f"mcp__{SERVER_NAME}__{LIST_NAME}": "namae",
    f"mcp__{SERVER_NAME}__{READ_NAME}": "id",
}

READ_CHARS = 30_000         # 1回に返す字の上限
READ_IMAGES = 4             # 1回に見せる絵の上限（スキャンした PDF のページ）
LIST_MAX = 50               # 一覧に出す件数の上限
IMAGE_EDGE = 1568           # 絵の長辺（写真と同じ）

LIST_DESCRIPTION = (
    "持ち主がこれまでに送ってくれたファイル（PDF・文字のファイルなど）の一覧を、新しい順に返す。"
    "「前に送った◯◯」「あの書類」と言われたら、まずこれで探して、files_read で読む。"
    "namae を渡すと、名前にその言葉を含むものだけに絞る。"
)
READ_DESCRIPTION = (
    "保存してあるファイルを1つ読む。id は files_list が返した id をそのまま渡す。"
    "PDF は page（何ページ目から・1始まり）と pages（何ページぶん・既定10）で範囲を選べる。"
    "文字の無いページ（スキャンした PDF）は、ページの絵を見せる。"
    "文字のファイルは kara（何字目から・0始まり）で続きを読める。"
    "返しの最後に続きの読み方が書いてあれば、それに従って続きを読める。"
)
LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "namae": {"type": "string", "description": "名前の一部（省略すると全部）"},
    },
}
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "files_list が返した id（例：2026-10-01/141630-ab12_見積書.pdf）"},
        "page": {"type": "integer", "description": "PDF の何ページ目から（1始まり・既定1）"},
        "pages": {"type": "integer", "description": "PDF を何ページぶん（既定10）"},
        "kara": {"type": "integer", "description": "文字のファイルの何字目から（既定0）"},
    },
    "required": ["id"],
}


def _files_dir() -> Path:
    return Path(file_blocks.FILES_DIR)


def _display_name(path: Path) -> str:
    """「141630-ab12_見積書.pdf」→「見積書.pdf」"""
    return path.name.split("_", 1)[1] if "_" in path.name else path.name


def list_files(namae: str = "") -> str:
    """置き場の一覧を、極が読める文にする。"""
    root = _files_dir()
    if not root.exists():
        return "保存してあるファイルは、まだありません。"
    found = []
    for day in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
        for p in sorted((p for p in day.iterdir() if p.is_file()), reverse=True):
            name = _display_name(p)
            if namae and namae.lower() not in name.lower():
                continue
            found.append((day.name, p))
            if len(found) >= LIST_MAX:
                break
        if len(found) >= LIST_MAX:
            break
    if not found:
        return f"「{namae}」を名前に含むファイルは見つかりませんでした。" if namae \
            else "保存してあるファイルは、まだありません。"
    lines = []
    for day, p in found:
        t = p.name[:6]
        when = f"{day} {t[0:2]}:{t[2:4]}" if t.isdigit() else day
        size = p.stat().st_size
        extra = ""
        if p.suffix.lower() == ".pdf" and file_blocks.PYPDF_OK:
            try:
                _, pages, locked = file_blocks._open_pdf(p.read_bytes())
                extra = f"・{pages}ページ" if pages else ("・鍵つき" if locked else "")
            except Exception:                       # noqa: BLE001
                extra = ""
        lines.append(f"- {_display_name(p)}（{when}・{max(1, size // 1024)}KB{extra}）　id: {day}/{p.name}")
    head = f"保存してあるファイル（新しい順・{len(found)}件{'・上限まで' if len(found) >= LIST_MAX else ''}）："
    return head + "\n" + "\n".join(lines)


def _resolve(file_id: str) -> Path | None:
    """id（日付/ファイル名）を置き場の中のパスにする。置き場の外は指させない。"""
    if not isinstance(file_id, str) or not file_id.strip():
        return None
    root = _files_dir().resolve()
    try:
        path = (root / file_id.strip().replace("\\", "/")).resolve()
    except Exception:                               # noqa: BLE001
        return None
    if root not in path.parents or not path.is_file():
        return None
    return path


def _shrink_image(raw: bytes) -> tuple[str, str] | None:
    """ページに入っていた絵を、見せられる形（base64・種類）にする。"""
    if not PIL_OK:
        return None
    try:
        with Image.open(BytesIO(raw)) as im:
            w, h = im.size
            if w * h > 40_000_000:
                return None
            im.load()
            im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
            out = BytesIO()
            im.convert("RGB").save(out, format="JPEG", quality=90)
            return base64.b64encode(out.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:                               # noqa: BLE001
        return None


def read_file(file_id: str, page: int = 1, pages: int = 10, kara: int = 0) -> list:
    """1つ読む。返すのは MCP の content（text と image の並び）。"""
    path = _resolve(file_id)
    if path is None:
        return [{"type": "text", "text": f"「{file_id}」は見つかりませんでした。files_list で id を確かめてください。"}]
    name = _display_name(path)
    raw = path.read_bytes()
    kind = file_blocks._kind(name, "", raw)

    if kind == "text":
        text = file_blocks._to_text(raw)
        kara = max(0, int(kara or 0))
        part = text[kara:kara + READ_CHARS]
        tail = ""
        if kara + READ_CHARS < len(text):
            tail = f"\n\n（ここまで {kara + len(part)}字／全{len(text)}字。続きは kara={kara + len(part)} で読めます）"
        return [{"type": "text", "text": f"【{name}】\n{part}{tail}"}]

    if kind != "pdf":
        return [{"type": "text", "text": f"「{name}」は読めない種類のファイルです（PDF・文字のファイルは読めます）。"
                                         f"PC には {path} に置いてあります。"}]
    if not file_blocks.PYPDF_OK:
        return [{"type": "text", "text": "この PC には PDF を読む部品（pypdf）がありません。"}]
    try:
        reader, total, locked = file_blocks._open_pdf(raw)
    except Exception as exc:                        # noqa: BLE001
        return [{"type": "text", "text": f"「{name}」は PDF として開けませんでした（{type(exc).__name__}）。"}]
    if reader is None:
        return [{"type": "text", "text": f"「{name}」は鍵（パスワード）がかかっていて開けませんでした。"}]

    start = min(max(1, int(page or 1)), max(1, total))
    count = min(max(1, int(pages or 10)), 50)
    content: list = []
    texts: list = []
    used = 0
    shown_images = 0
    last = start - 1
    for i in range(start, min(total, start + count - 1) + 1):
        pg = reader.pages[i - 1]
        try:
            t = (pg.extract_text() or "").strip()
        except Exception:                           # noqa: BLE001
            t = ""
        if t:
            if used + len(t) > READ_CHARS and texts:
                break
            texts.append(f"--- {i}ページ ---\n{t[:READ_CHARS]}")
            used += len(texts[-1])
        else:
            # 文字の無いページ＝スキャンした PDF。ページの絵をそのまま見せる
            imgs = []
            try:
                imgs = list(pg.images)
            except Exception:                       # noqa: BLE001
                imgs = []
            got = None
            for im in imgs[:1]:
                got = _shrink_image(im.data)
            if got and shown_images < READ_IMAGES:
                texts.append(f"--- {i}ページ（文字が無いので絵で見せます）---")
                content.append({"type": "text", "text": "\n\n".join(texts)})
                texts = []
                content.append({"type": "image", "data": got[0], "mimeType": got[1]})
                shown_images += 1
            elif got:
                break                               # 絵の上限。続きは次の呼び出しで
            else:
                texts.append(f"--- {i}ページ ---\n（文字も絵も取り出せませんでした）")
        last = i
    if texts:
        content.append({"type": "text", "text": "\n\n".join(texts)})
    head = f"【{name}】全{total}ページ" + ("（鍵つき・文字だけ）" if locked else "") + f"・{start}〜{last}ページ"
    content.insert(0, {"type": "text", "text": head})
    if last < total:
        content.append({"type": "text", "text": f"（続きは page={last + 1} で読めます）"})
    return content


def build_server():
    """読み返す道具を、Agent SDK に渡せる形で返す。使えなければ None。"""
    if not AVAILABLE:
        return None

    @tool(LIST_NAME, LIST_DESCRIPTION, LIST_SCHEMA)
    async def _list(args):
        namae = str((args or {}).get("namae") or "").strip()
        try:
            text = await asyncio.to_thread(list_files, namae)
        except Exception as e:                      # noqa: BLE001
            text = f"一覧を出せませんでした（{type(e).__name__}）。"
        return {"content": [{"type": "text", "text": text}]}

    @tool(READ_NAME, READ_DESCRIPTION, READ_SCHEMA)
    async def _read(args):
        args = args or {}
        try:
            content = await asyncio.to_thread(
                read_file, str(args.get("id") or ""), args.get("page") or 1,
                args.get("pages") or 10, args.get("kara") or 0)
        except Exception as e:                      # noqa: BLE001
            content = [{"type": "text", "text": f"読めませんでした（{type(e).__name__}）。"}]
        return {"content": content}

    return create_sdk_mcp_server(name=SERVER_NAME, tools=[_list, _read])
