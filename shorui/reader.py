# -*- coding: utf-8 -*-
"""書類を読む係（2026-09-07 新設）── PDF・ワード・エクセル・ふつうの文字。

★中身はここ1か所だけです。包みが2つあります ──
    `claude/shorui_tools.py`  … クロード極の実務用
    もう一つの別のアプリ側の包み
  どちらに頼んでも**同じ答えが返ります**。

★何のためにあるか
  `WebFetch` は PDF を**取ってはくる**のですが、圧縮されたまま渡すので中身が読めません。
  2026-09-07、ある市の冊子の PDF を開いたとき、返ってきたのはこれでした ──
    「提供いただいたWebページコンテンツはPDF形式の圧縮データであり、
      テキストとして正常に解析できません」
  実務は外の変換サービス（r.jina.ai）まで試して、それでも読めませんでした。
  ここは、その受け皿です。

★読めるもの ── PDF ／ ワード(.docx) ／ エクセル(.xlsx) ／ ふつうの文字（テキスト・CSV・HTML）。
  **中身の頭を見て決めます。** 拡張子は当てになりません
  （その PDF の住所は `.../xxxx/1` で、`.pdf` が付いていませんでした）。

★読むだけです。書き込みも、このPCのファイル開きもしません（URLだけを受け取ります）。
"""
import io
import re
import sys
import zipfile
import urllib.error
import urllib.request

try:
    import pypdf
    PDF_OK = True
except Exception as _e:                       # 部品が無くても会話は止めない
    pypdf = None
    PDF_OK = False
    print(f"[shorui] PDF は読めません（pypdf が入っていません）: {type(_e).__name__}: {_e}")


# 1回に取ってくる大きさの上限（市の書類は大きくても数MB）。
MAX_BYTES = 40 * 1024 * 1024
# 1回に返す文字数の上限。長い書類は page でめくってもらう。
MAX_CHARS = 15000
# 名乗り。役所のサイトは、名乗らないと断ることがある。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _fetch(url: str, timeout: int = 60) -> tuple[bytes, str]:
    """URLの中身を取ってくる。返りは (中身, 困りごとの言葉)。"""
    if not re.match(r"^https?://", url or "", re.I):
        return b"", "http:// か https:// で始まる住所を渡してください。"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = res.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return b"", "書類が大きすぎて読めませんでした（40MBまで）。"
        return data, ""
    except urllib.error.HTTPError as e:
        return b"", f"そのページは開けませんでした（HTTP {e.code}）。"
    except (urllib.error.URLError, TimeoutError):
        return b"", "そのページにつながりませんでした。住所を確かめる。"
    except Exception as e:
        return b"", f"そのページを取ってこられませんでした（{type(e).__name__}）。"


def _kind(data: bytes) -> str:
    """中身の頭を見て、何の書類かを決める（拡張子は当てにしない）。"""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
            if "word/document.xml" in names:
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
        except Exception:
            pass
        return "zip"
    return "text"


def _decode(data: bytes) -> str:
    """ふつうの文字として読む。日本語のページは utf-8 か cp932 のどちらか。"""
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _strip_tags(xml: str) -> str:
    """XML の札を外して、中の文字だけにする。"""
    xml = re.sub(r"<w:p[ >]", "\n<w:p ", xml)          # 段落の切れ目を改行に
    xml = re.sub(r"<w:br\s*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    return text


def _read_pdf(data: bytes, page: int) -> str:
    if not PDF_OK:
        return ("PDFを読む部品（pypdf）が、このパソコンに入っていません。"
                "持ち主に一声かけて `python -m pip install pypdf` を入れてもらう。")
    try:
        doc = pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:
        return f"PDFとして開けませんでした（{type(e).__name__}）。"

    total = len(doc.pages)
    start = max(1, int(page or 1))
    if start > total:
        return f"このPDFは全{total}ページなので、{start}ページ目はありません。"

    out, i = [], start
    while i <= total and sum(len(x) for x in out) < MAX_CHARS:
        try:
            body = doc.pages[i - 1].extract_text() or ""
        except Exception:
            body = ""
        out.append(f"── {i}ページ ──\n{body.strip()}")
        i += 1

    head = f"PDF・全{total}ページ／いま {start}〜{i - 1}ページ目"
    if i <= total:
        head += f"（続きは page={i} で読める）"
    body = "\n\n".join(out)
    if not body.replace("──", "").replace("ページ", "").strip():
        return (f"{head}\n\n文字が1つも取り出せませんでした。"
                "紙をそのまま写した（画像だけの）PDFかもしれません。"
                "そのときは、持ち主に画面の写真を送っていただくほうが早い。")
    return f"{head}\n\n{body[:MAX_CHARS]}"


def _read_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as e:
        return f"ワードの書類として開けませんでした（{type(e).__name__}）。"
    lines = [ln.strip() for ln in _strip_tags(xml).splitlines()]
    return "ワードの書類\n\n" + "\n".join(ln for ln in lines if ln)[:MAX_CHARS]


def _read_xlsx(data: bytes) -> str:
    """エクセルを、1行ずつの文字にする（見た目や式ではなく、入っている値だけ）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                shared = [_strip_tags(m).strip()
                          for m in re.findall(r"<si>(.*?)</si>", sx, re.S)]
            sheets = [n for n in names
                      if n.startswith("xl/worksheets/") and n.endswith(".xml")]
            out = []
            for sheet in sorted(sheets)[:5]:
                sx = z.read(sheet).decode("utf-8", "replace")
                out.append(f"── {sheet.split('/')[-1]} ──")
                for row in re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S):
                    cells = []
                    for cell in re.findall(r"<c\b[^>]*>.*?</c>|<c\b[^>]*/>", row, re.S):
                        val = re.search(r"<v>(.*?)</v>", cell, re.S)
                        if not val:
                            inline = re.search(r"<is>(.*?)</is>", cell, re.S)
                            cells.append(_strip_tags(inline.group(1)).strip() if inline else "")
                            continue
                        raw = val.group(1)
                        if 't="s"' in cell:      # 共有された文字の番号
                            try:
                                raw = shared[int(raw)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(raw.strip())
                    if any(cells):
                        out.append(" | ".join(cells))
    except Exception as e:
        return f"エクセルの書類として開けませんでした（{type(e).__name__}）。"
    return "エクセルの書類\n\n" + "\n".join(out)[:MAX_CHARS]


def _read_text(data: bytes) -> str:
    text = _decode(data)
    if re.search(r"<\s*(html|body|div|p)\b", text[:4000], re.I):
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = _strip_tags(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())[:MAX_CHARS]


def read_document(url: str, page: int = 1) -> str:
    """URLの書類を、読める文字にして返す。★ここが中身の1か所。"""
    data, err = _fetch(url)
    if err:
        return err
    if not data:
        return "そのページは空でした。"

    kind = _kind(data)
    if kind == "pdf":
        return _read_pdf(data, page)
    if kind == "docx":
        return _read_docx(data)
    if kind == "xlsx":
        return _read_xlsx(data)
    if kind == "zip":
        return "圧縮された入れ物（zip）でした。ワードでもエクセルでもないので、中は読めません。"
    return _read_text(data)


# ── 包みが共通で使うもの（呼び出し側が違っても、同じ言葉・同じ名前にするため）──
TOOL_NAME = "shorui_read"

TOOL_DESCRIPTION = (
    "URLの書類を開いて、中の文字を読む。PDF・ワード(.docx)・エクセル(.xlsx)・"
    "ふつうの文字（テキスト・CSV・HTML）に対応。"
    "★WebFetch がPDFを開くと『圧縮データで読めません』と返ってくる。そのときはこれを使う。"
    "拡張子が .pdf でなくても中身を見て判るので、役所の書類の住所をそのまま渡してよい。"
    "page＝PDFの何ページ目から読むか（既定は1）。長い書類は、返ってきた案内のとおり "
    "page をめくって続きを読む。"
    "★読めなかったときは、そう返ってくる。読めないまま話を進めないで、"
    "『この書類は機械では読めませんでした』と正直に書く。"
)

TOOL_SCHEMA = {"url": str, "page": int}


def tool_args(args: dict) -> tuple[str, int]:
    """道具に渡された引数を、read_document が使える形にそろえる。"""
    url = " ".join(str((args or {}).get("url") or "").split())
    try:
        page = int((args or {}).get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    return url, page


if __name__ == "__main__":                    # 確かめ用：python reader.py <URL> [ページ]
    sys.stdout.reconfigure(encoding="utf-8")
    print(read_document(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1))
