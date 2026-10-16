# -*- coding: utf-8 -*-
"""クロード極に送ったファイル（PDF・文字のファイル）を、モデルに渡す document ブロックにする（2026-10-01）。

ファイルを添付して、PDF も読めるようにする。

■ 流れ
  画面（index.html）が data URL で送る → ここで document ブロックにする →
  app.py が写真の image ブロックと並べて、その1回の発言に乗せる（agent_bridge._prompt_with_photos）。
  SDK→CLI の道を document ブロックがそのまま通ることは、2026-10-01 に Haiku 1回で確かめた。

■ 重さ（写真と同じ考え方）
  ・アプリの履歴（store）には目印の1行だけ。中身は入れない。
  ・CLI の履歴に残ったぶんは session_store が「直近5ターン」だけ残し、
    それより前は1行のしおり（ファイル名入り）に縮める＝「さっきのPDF」には答えられる。
  ・PDF はページごとに文字＋絵で読む（1ページ 約1,500〜3,000トークン）。

■ 断られないための砦（★ここが要）
  モデルに断られる大きさ・形のまま渡すと、CLI の履歴にその発言が残り、
  **そのスレッドが数ターン続けて「うまくいきませんでした」になる**。だから渡す前にここで見る。
    ・API の上限は1回の送信で 32MB・100ページ（200k の型）／600ページ（1M の型）。
      **前のターンの PDF も毎回いっしょに送られる**ので、1回ぶんは VISUAL_* まで、
      履歴に残すぶんは HIST_*（session_store.trim_documents）までにして、足しても上限に届かないようにしてある。
    ・それでも断られたとき（文脈の窓あふれ・API だけが嫌う PDF）は、session_store.load が
      「断られた発言のファイル」をしおりにする＝次のターンで元に戻る（_drop_rejected_documents）。
    ・それを超える PDF は、文字だけ取り出して渡す（TEXT_MAX_CHARS まで）。
    ・鍵（パスワード）のかかった PDF は、空の鍵で開けるものだけ文字で渡す。開けないものは渡さない。
  渡さなかったものは、目印の文（mark_for）に理由を書く＝アシスタントも「読めていない」と分かる。
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
from datetime import datetime
from io import BytesIO
from pathlib import Path

# 送られたファイルの置き場（2026-10-01）。
# claude/data/ は .gitignore 済み＝GitHub には出ない。消す仕掛けは置かない（写真の置き場と同じ）。
FILES_DIR = Path(__file__).resolve().parent / "data" / "files"

# 1つあたりの受け取り上限（縮める前）。画面（index.html の FILE_MAX_BYTES）でも同じ値で止める。
MAX_BYTES = 30 * 1024 * 1024
# 1回の送信で、絵ごと読ませる PDF の合計（ページ・大きさ）。超えたぶんは文字だけにする。
VISUAL_PAGES = 45
VISUAL_BYTES = 9 * 1024 * 1024
# 履歴に残しておくファイルの合計（session_store.trim_documents が使う）。
# ★1回ぶんと同じ大きさにそろえてある（HIST_BYTES は base64 の長さ＝9MB×4/3）。
#   そろえないと、送ったターンは読めたのに、次のターンで即しおりになって「さっきのPDF」に答えられない。
#   1回ぶん＋履歴で 90ページ・24MB ＝ API の上限（100ページ・32MB）に届かない。
HIST_PAGES = 45
HIST_BYTES = 12 * 1024 * 1024
# 文脈の窓（トークン）の目安。PDF は1ページ PAGE_TOKENS、文字は1字1トークンで見積もる。
# 履歴に残すのは HIST_TOKENS まで（45ページの PDF 1本、または10万字の文字1本が残る大きさ）。
PAGE_TOKENS = 2_000
HIST_TOKENS = 100_000
# 文字で渡すときの上限（1回の送信の合計）。日本語はおおむね1字1トークン前後。
TEXT_MAX_CHARS = 100_000
# pypdf が無い端末では、ページを数えられない。小さいものだけ絵ごと渡す。
NO_PYPDF_MAX_BYTES = 5 * 1024 * 1024

# 文字のファイルとして読む拡張子（type が空で届くことがあるので、名前でも見る）
TEXT_EXTS = (".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".html", ".htm",
             ".yaml", ".yml", ".ini", ".py", ".js", ".css")

# title の後ろに付けるページ数。session_store が履歴の PDF のページを数えるときにも読む。
PAGES_RE = re.compile(r"（(\d+)ページ）$")

try:                      # 無い端末でも極は起動する（写真の Pillow と同じ作法）
    from pypdf import PdfReader
    PYPDF_OK = True
except Exception:         # noqa: BLE001
    PdfReader = None      # type: ignore[assignment]
    PYPDF_OK = False


def _decode(data_url) -> bytes | None:
    """画面から来た data URL を、ただのバイト列にする。

    ★iPhone は .md や .csv を `data:;base64,` で送ってくることがある（種類が空）。
      なので種類では選ばず、`;base64` だけを見る。何のファイルかは名前と中身で決める。
    """
    if not isinstance(data_url, str):
        return None
    head, sep, body = data_url.partition(",")
    if not sep or not head.startswith("data:") or ";base64" not in head:
        return None
    if len(body) > MAX_BYTES * 4 // 3 + 1024:      # base64 は約4/3に膨らむ
        return None
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return None


def clean_name(name) -> str:
    """ファイル名を、目印の文に入れられる形にする（フォルダ・「」・見えない字を落とす）。"""
    s = str(name or "").replace("\\", "/").split("/")[-1]
    # ★画面の cleanFileName と同じ字だけ落とす（制御文字と「」）。全角スペースなどは残す
    #   ＝送った直後の吹き出しと、開き直したときの名前が同じになる。
    s = "".join(c for c in s if c not in "「」" and not (ord(c) < 0x20 or ord(c) == 0x7f)).strip()
    return s[:80] or "ファイル"


def _kind(name: str, mime: str, raw: bytes) -> str:
    """'pdf' / 'text' / '' を返す。"""
    low = name.lower()
    # ★名前・種類を先に見る。「%PDF-」と本文に書いてあるメモ（.md・.txt）を PDF と取り違えないため。
    # PDF と名乗っていても、中身の頭1KBに %PDF- が無ければ PDF ではない（渡さない）。
    if low.endswith(".pdf") or mime == "application/pdf":
        return "pdf" if b"%PDF-" in raw[:1024] else ""
    if low.endswith(TEXT_EXTS) or mime.startswith("text/") or mime == "application/json":
        return "text"
    if raw.lstrip()[:5] == b"%PDF-":                # 名前に拡張子が無い PDF
        return "pdf"
    return ""


def _to_text(raw: bytes) -> str:
    """文字のファイルを読む。日本の CSV は Shift_JIS（cp932）のことが多いので、2番目に試す。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _open_pdf(raw: bytes):
    """(reader, ページ数, 鍵がかかっていたか) を返す。開けなければ reader は None。"""
    reader = PdfReader(BytesIO(raw))
    locked = bool(reader.is_encrypted)
    if locked:
        try:
            ok = reader.decrypt("")                 # 空の鍵で開けるもの（印刷だけ禁止など）
        except Exception:                           # noqa: BLE001
            ok = 0
        if not ok:
            return None, 0, True
    return reader, len(reader.pages), locked


def _pdf_text(reader, limit: int) -> tuple[str, bool]:
    """PDF から文字だけを取り出す。(文字, 途中で切ったか)。"""
    parts: list = []
    total = 0
    for i, page in enumerate(reader.pages, 1):
        try:
            t = (page.extract_text() or "").strip()
        except Exception:                           # noqa: BLE001
            t = ""
        if not t:
            continue
        parts.append(f"--- {i}ページ ---\n{t}")
        total += len(parts[-1]) + 2
        if total >= limit:
            break
    text = "\n\n".join(parts)
    return text[:limit], total > limit


def _text_block(title: str, text: str) -> dict:
    return {"type": "document", "title": title,
            "source": {"type": "text", "media_type": "text/plain", "data": text}}


def make_blocks(files: list) -> tuple[list, list, list]:
    """画面から来たファイルを document ブロックにする。

    files … [{"name": "a.pdf", "type": "application/pdf", "data": "data:...;base64,..."}, ...]
    返すのは (ブロック, 渡せたファイル名, ことわり書き)。
    ★ここから例外を外へ出さない。ファイルがうまくいかなくても、極は返事をする。
    """
    blocks: list = []
    names: list = []
    notes: list = []
    visual_pages = 0
    visual_bytes = 0
    text_left = TEXT_MAX_CHARS

    for f in list(files or []):
        if not isinstance(f, dict):
            continue
        name = clean_name(f.get("name"))
        try:
            data = f.get("data")
            if isinstance(data, str) and len(data) > MAX_BYTES * 4 // 3 + 1024:
                notes.append(f"「{name}」は大きすぎて読めなかった")
                continue
            raw = _decode(data)
            if raw and len(raw) > MAX_BYTES:
                notes.append(f"「{name}」は大きすぎて読めなかった")
                continue
            if not raw:
                notes.append(f"「{name}」は開けなかった")
                continue
            kind = _kind(name, str(f.get("type") or ""), raw)

            if kind == "pdf":
                if not PYPDF_OK:
                    if visual_bytes + len(raw) > NO_PYPDF_MAX_BYTES:
                        notes.append(f"「{name}」は大きすぎて読めなかった")
                        continue
                    blocks.append({"type": "document", "title": name,
                                   "source": {"type": "base64", "media_type": "application/pdf",
                                              "data": base64.b64encode(raw).decode("ascii")}})
                    visual_bytes += len(raw)
                    names.append(name)
                    continue

                try:
                    reader, pages, locked = _open_pdf(raw)
                except Exception as exc:            # noqa: BLE001
                    print(f"[file] PDF を開けませんでした: {name}: {type(exc).__name__}: {exc}")
                    notes.append(f"「{name}」は PDF として開けなかった")
                    continue
                if reader is None:
                    notes.append(f"「{name}」は鍵（パスワード）がかかっていて開けなかった")
                    continue
                if pages < 1:
                    notes.append(f"「{name}」はページが無かった")
                    continue

                fits = (not locked
                        and visual_pages + pages <= VISUAL_PAGES
                        and visual_bytes + len(raw) <= VISUAL_BYTES)
                if fits:
                    # 絵ごと読ませる（表・図・手書きも見える）
                    blocks.append({"type": "document", "title": f"{name}（{pages}ページ）",
                                   "source": {"type": "base64", "media_type": "application/pdf",
                                              "data": base64.b64encode(raw).decode("ascii")}})
                    visual_pages += pages
                    visual_bytes += len(raw)
                    names.append(name)
                    continue

                # 大きい・鍵つき → 文字だけ
                if text_left <= 0:
                    notes.append(f"「{name}」は、ほかのファイルで読める量がいっぱいで読めなかった")
                    continue
                text, cut = _pdf_text(reader, text_left)
                if not text.strip():
                    notes.append(f"「{name}」は文字を取り出せなかった（写真のような PDF）")
                    continue
                blocks.append(_text_block(f"{name}（文字だけ）", text))
                text_left -= len(text)
                names.append(name)
                why = "鍵がかかっているので" if locked else f"{pages}ページと大きいので"
                notes.append(f"「{name}」は{why}、文字だけ読んだ"
                             + ("（はじめのほうだけ）" if cut else ""))
                continue

            if kind == "text":
                if text_left <= 0:
                    notes.append(f"「{name}」は、ほかのファイルで読める量がいっぱいで読めなかった")
                    continue
                text = _to_text(raw)
                if not text.strip():
                    notes.append(f"「{name}」は空だった")
                    continue
                cut = len(text) > text_left
                text = text[:text_left]
                blocks.append(_text_block(name, text))
                text_left -= len(text)
                names.append(name)
                if cut:
                    notes.append(f"「{name}」は長いので、はじめのほうだけ読んだ")
                continue

            notes.append(f"「{name}」は読めない種類のファイルだった（PDF・文字のファイルは読める）")
        except Exception as exc:                    # noqa: BLE001 — ファイル1つで会話を転ばせない
            print(f"[file] 読めませんでした: {name}: {type(exc).__name__}: {exc}")
            notes.append(f"「{name}」は開けなかった")

    if blocks:
        print(f"[file] {len(blocks)}つあずかりました（{'／'.join(names)}・"
              f"絵ごと{visual_pages}ページ {visual_bytes // 1024}KB・"
              f"文字 {TEXT_MAX_CHARS - text_left}字）")
    return blocks, names, notes


_WIN_BAD = '\\/:*?"<>|'


def save_files(files: list) -> list:
    """送られたファイルを、そのまま PC に置く（2026-10-01）。返すのは [{"path", "name", "bytes"}]。

    置き場は FILES_DIR/日付/「時刻-乱数_名前」。読めない種類のファイルも置く（あとで人が開けるように）。
    ★ここから例外を外へ出さない。置けなくても、極は返事をする。
    """
    saved: list = []
    now = datetime.now()
    for f in list(files or []):
        if not isinstance(f, dict):
            continue
        name = clean_name(f.get("name"))
        try:
            raw = _decode(f.get("data"))
            if not raw or len(raw) > MAX_BYTES:
                continue
            safe = "".join("_" if c in _WIN_BAD else c for c in name).strip(" .") or "ファイル"
            folder = FILES_DIR / f"{now:%Y-%m-%d}"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{now:%H%M%S}-{secrets.token_hex(2)}_{safe}"
            path.write_bytes(raw)
            saved.append({"path": str(path), "name": name, "bytes": len(raw)})
        except Exception as exc:                    # noqa: BLE001
            print(f"[file] PC に置けませんでした: {name}: {type(exc).__name__}: {exc}")
    if saved:
        print(f"[file] PC に置きました: " + "／".join(
            f"{s['name']} {s['bytes'] // 1024}KB" for s in saved))
    return saved


def mark_for(names: list, notes: list, seen: bool = True) -> str:
    """履歴に残す目印の文。★画面（index.html の fileMark）と同じ形にする（読み直しても同じに見える）。"""
    parts: list = []
    if names:
        if len(names) == 1:
            s = f"ファイル「{names[0]}」を送ったよ"
        else:
            s = f"ファイルを{len(names)}つ送ったよ：" + "".join(f"「{n}」" for n in names)
        if not seen:
            s += "。でも、いまのアシスタントには見えていないみたい"
        parts.append(f"（{s}）")
    parts += [f"（{n}）" for n in notes]
    return "".join(parts)
