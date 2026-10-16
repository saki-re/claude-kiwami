# -*- coding: utf-8 -*-
"""クロード極に送った写真の置き場（2026-08-19）。

写真を送れるようにする。毎回写真を読み直す作りではなく、その時は見れて、
それ以降は写真の雰囲気の要約だけが残る、コスパのいい作りにする。

■ 考え方：**直近5ターンは残す。それより前は一行に縮める**（2026-08-15）
  はじめ「その1ターンだけ」にしていたが、それでは
  **「さっきの写真なんだった？」に答えられない**と分かって直した。
  1ターンではなく数ターン残すほうが、コスパも精度も良くなる。→「**写真は、5ターン残す**」

    ・直API経路 …… history に積み、送るときに `prompting._strip_old_photos` が古いぶんだけ外す
    ・SDK経路   …… そのまま置き、`session_store.load()` が古いぶんだけ縮める

  5ターンより前は、**モデルが写真を見て言ったことば**だけが残る。
  ＝「その時は見れて、しばらくは覚えていて、そのあとは雰囲気だけ」。
  要約のための生成は**1回も足していない**（モデルの返事そのものが要約になる）。

  実測の目安：写真1枚 約1,844トークン。
    1ターン … 1,844（安いが「さっきの」に答えられない）
    5ターン … 約9,200（答えられる）
    公式    … 20往復で約37,000（答えられるが、ずっと重い）

■ 枚数の上限は置いていない（2026-08-15）
  写真はコスパよくつくってあるので、上限は不要。
  写真は届いたターンにしか乗らず、次のターンには1枚も残らない作りなので、
  たくさん送っても後を引かない。だから枚数は絞らない。
  残してある砦は「1枚が壊れて巨大なとき」の MAX_BYTES だけ。

■ 実物はどこへ行くか
  server/data/photos/YYYY-MM-DD/<id>.jpg   … モデルに見せる版（長辺 1568px）
  server/data/photos/YYYY-MM-DD/<id>_t.jpg … 画面に出すサムネ（長辺 320px）
  `server/data/*` は .gitignore 済み＝**GitHub には出ない**（会話ログと同じ扱い）。
  ★**消していない。** `sweep()` は用意してあるが、どこからも呼んでいない
    （background の日次見回りは容量を見るだけ）。
    写真は会話に乗らないので、置いてあってもトークンは1つも増えない。
    2026-08-19、この置き場をシール帳の「思い出」として見返せるようにした（`list_days`）ので、
    **勝手に消す仕掛けを足さないこと。**

■ なぜ 1568px か
  Anthropic がそのまま受け取る長辺の上限で、これより大きく送っても料金と時間が増えるだけ
  （config.EYE_LONG_EDGE と同じ理由・同じ値）。目と同じ見え方になる。
  ブラウザ側でも同じ大きさに縮めてから送るので、ここは念のための二重の砦。

■ Pillow が無い端末では、写真を受け取らない（2026-08-15 レビュー指摘で変更）
  目（routes_eye.py）と同じ作法で、Pillow は中で囲って読む。
  ただし**無いときは写真をあずからない**（縮小せずに渡すと、PNG を「JPEGです」と
  偽って渡すことになり、モデルに拒まれて**そのターン返事が止まる**）。
  目のときと同じ判断で、「あやしいものは、見えないほうがまし」にしてある。
  写真が0枚のときは、ことばだけの、いつもの会話になる。
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import config

BASE_DIR = Path(__file__).resolve().parent
PHOTO_DIR = BASE_DIR / "data" / "photos"

# モデルに見せる版の長辺。目と同じ値（config.EYE_LONG_EDGE＝1568）。
LONG_EDGE = getattr(config, "EYE_LONG_EDGE", 1568)
# 画面に出すサムネの長辺。
# ★2026-08-15：320 → 480 に上げた。会話に出るサムネは 148px 幅だが、
#   iPhone の画面は3倍の細かさなので 148×3＝444px 必要。320 では粗く見えていた。
THUMB_EDGE = 480
# 1枚あたりの受け取り上限（縮小前）。
# ※これは枚数のケチではなく「1枚が壊れて巨大なときに転ばないため」の砦。
# ★2026-08-15：12MB → 32MB に上げた。iPhone の ProRAW や長時間の連写は
#   1枚10MBを超えることがあり、12MB では**ふつうの写真が弾かれうる**。
MAX_BYTES = 32 * 1024 * 1024
# 1枚あたりの画素数の上限。ファイルが小さくても画素が桁違いに多い画像があるため、
# 開く前にここで断る（目の `config.EYE_MAX_PIXELS` と同じ考え方・同じ値）。
MAX_PIXELS = getattr(config, "EYE_MAX_PIXELS", 16_000_000)
# 1回に送れる枚数は、決めない（2026-08-15）。写真はコスパよくつくってあるので上限は不要。
# ＝ 何枚送っても、その1ターンで見て終わり。履歴には1枚も残らないので後を引かない。
MAX_PHOTOS = None
# JPEG の品質。
# ★2026-08-15 日本語の漢字が読めるか見直した。
#   目（カメラ）は `config.EYE_JPEG_QUALITY = 90`。
#   画質は高くていい（公式クロードと同程度の内容把握を目指す）。
#   なのに送られた写真だけ 85 にしていた＝**目より低かった**。
#   しかも iPhone の写真はすでに JPEG で、縮めてもう一度 JPEG にする＝二重の圧縮。
#   **いちばん先に潰れるのが小さい漢字**なので、ここは目より上げる。
JPEG_QUALITY = 92
# サムネは画面に出すだけなので、そこまで要らない。
THUMB_QUALITY = 82

_ID_RE = re.compile(r"^[0-9]{8}-[0-9a-f]{12}$")
_DATA_URL_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp|gif);base64,", re.I)

try:                      # 目と同じ作法。無い端末でも起動する。
    from PIL import Image, ImageOps
    PIL_OK = True
except Exception:         # noqa: BLE001
    Image = None          # type: ignore[assignment]
    ImageOps = None       # type: ignore[assignment]
    PIL_OK = False


def _new_id() -> str:
    """写真の名札。日付＋乱数だけ＝ファイル名に使える字しか入らない。"""
    return f"{datetime.now():%Y%m%d}-{secrets.token_hex(6)}"


def valid_photo_id(photo_id: str) -> bool:
    """外から来た名札の形を検査する（パストラバーサル防止。config.valid_date と同じ考え方）。"""
    return bool(photo_id) and bool(_ID_RE.match(photo_id))


def _dir_for(photo_id: str) -> Path:
    return PHOTO_DIR / f"{photo_id[0:4]}-{photo_id[4:6]}-{photo_id[6:8]}"


def path_for(photo_id: str, thumb: bool = False) -> Path | None:
    """名札から実物の場所を出す。形がおかしい名札には None を返す。"""
    if not valid_photo_id(photo_id):
        return None
    name = f"{photo_id}_t.jpg" if thumb else f"{photo_id}.jpg"
    return _dir_for(photo_id) / name


def _decode(data_url: str) -> bytes | None:
    """画面から来た data URL を、ただのバイト列にする。画像でなければ None。"""
    if not isinstance(data_url, str):
        return None
    head, _, body = data_url.partition(",")
    if not body or not _DATA_URL_RE.match(head + ","):
        return None
    if len(body) > MAX_BYTES * 4 // 3 + 1024:      # base64 は約4/3に膨らむ
        return None
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return None


def _shrink(raw: bytes, long_edge: int, quality: int = JPEG_QUALITY) -> bytes | None:
    """長辺を long_edge まで縮めて JPEG にする。作れなければ None。

    ★None を返したら、その写真は**受け取らない**（2026-08-15 レビュー指摘で変更）。
      前は失敗しても元のバイト列をそのまま保存していたが、それだと
        ・PNG のバイト列を「image/jpeg です」と偽ってモデルへ渡す
        ・壊れた写真・大きすぎる写真をそのまま渡す
      ことになり、**モデルに拒まれてターンごとエラーになる＝返事が止まる**。
      目のときと同じ考え方で、「あやしいものは、見えないほうがまし」にする。
    """
    if not PIL_OK:
        return None
    try:
        with Image.open(BytesIO(raw)) as source:
            # ★中身を開く前に、画素の数だけ先に見る（2026-08-15・Codex の指摘で追加）。
            #   ファイルの大きさ（MAX_BYTES）は小さくても、**画素数は桁違いに大きい**ことがある
            #   （圧縮のよく効く絵・細工した画像）。そのまま load() すると、
            #   その1枚のためにメモリと時間を食い、**全体の返事が止まる**。
            #   目（routes_eye）と同じ砦を、送られた写真にも置く。
            w, h = source.size
            if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
                print(f"[photo] 画素が多すぎるので受け取りません（{w}x{h}）")
                return None
            source.load()
            # iPhone の写真は「横向きで撮ったのに縦」の情報が exif に入っている。
            # これを直さないと、モデルには寝転んだ写真が見える。
            image = ImageOps.exif_transpose(source)
            image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
            out = BytesIO()
            image.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception as exc:      # noqa: BLE001 — 写真1枚で会話を転ばせない
        print(f"[photo] 写真として開けませんでした（受け取りません）: "
              f"{type(exc).__name__}: {exc}")
        return None


def save_photos(data_urls: list) -> list:
    """画面から来た写真を保存して、名札の一覧を返す。

    返すのは [{"id":..., "bytes":..., "thumb_bytes":...}, ...]。
    1枚も保存できなければ空のリスト（＝写真なしとして、いつもどおり会話を続ける）。
    ★ここから例外を外へ出さない。写真がうまくいかなくても、返事はする。
    """
    saved: list = []
    if not data_urls:
        return saved

    for data_url in list(data_urls):
        try:
            raw = _decode(data_url)
            if not raw or len(raw) > MAX_BYTES:
                continue

            # ★JPEG に作り直せた写真だけを受け取る。
            #   作り直せない＝壊れている／Pillow が使えない、のどちらか。
            #   どちらの場合も、渡せばモデルに拒まれて返事が止まるので、ここで捨てる。
            big = _shrink(raw, LONG_EDGE, JPEG_QUALITY)
            small = _shrink(raw, THUMB_EDGE, THUMB_QUALITY)
            if big is None or small is None:
                continue

            photo_id = _new_id()
            folder = _dir_for(photo_id)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{photo_id}.jpg").write_bytes(big)
            (folder / f"{photo_id}_t.jpg").write_bytes(small)

            saved.append({"id": photo_id, "bytes": len(big), "thumb_bytes": len(small)})
        except Exception as exc:      # noqa: BLE001
            print(f"[photo] 保存できませんでした: {type(exc).__name__}: {exc}")

    if saved:
        detail = "／".join("{} {}KB".format(s["id"], s["bytes"] // 1024) for s in saved)
        print(f"[photo] {len(saved)}枚あずかりました（{detail}）")
    return saved


def photo_bytes(photo_id: str, thumb: bool = False) -> bytes | None:
    """名札から実物を読む。無ければ None。"""
    path = path_for(photo_id, thumb)
    if path is None or not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def image_blocks(photo_ids: list) -> list:
    """モデルに見せるための image ブロックを作る（Anthropic の直API・SDK 共通の形）。

    ★これを渡すのは、写真が届いた**そのターンだけ**。履歴には入れない。
    """
    blocks: list = []
    for photo_id in photo_ids or []:
        raw = photo_bytes(photo_id)
        if not raw:
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(raw).decode("ascii"),
            },
        })
    return blocks


def sweep(keep_days: int = 30) -> int:
    """古い写真を消す。戻り値は消した枚数（日次の見回りから呼ばれる）。"""
    if not PHOTO_DIR.exists():
        return 0
    limit = time.time() - keep_days * 86400
    removed = 0
    for path in PHOTO_DIR.rglob("*.jpg"):
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            pass
    # 空になった日のフォルダも片づける
    for folder in sorted(PHOTO_DIR.glob("*"), reverse=True):
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    return removed


def list_days() -> dict:
    """置き場にある写真を、新しい日から順に返す（2026-08-19 新設）。

    シール帳の中に写真置き場があると、持ち主の思い出になる。
    モデルはその写真を記憶として思い出さなくてよく、持ち主が見返せればよい。

    ★ここは**持ち主が見返すためだけ**の口。モデルには1枚も渡さない。
      モデルが写真を見るのは、送られたそのターンだけ（`image_blocks`）＝この関数とは無関係。
    ★同じ日の中は**送った順**（ファイルの時刻）に並べる。名札の後ろは乱数なので、
      名前で並べても送った順にはならない。
    """
    if not PHOTO_DIR.exists():
        return {"days": [], "count": 0}

    days: list = []
    total = 0
    for folder in sorted(PHOTO_DIR.glob("*"), reverse=True):
        if not folder.is_dir() or not config.valid_date(folder.name):
            continue
        found: list = []
        for path in folder.glob("*.jpg"):
            if path.stem.endswith("_t"):
                continue                      # サムネは数えない（同じ写真が2枚に見える）
            if not valid_photo_id(path.stem):
                continue
            try:
                found.append((path.stat().st_mtime, path.stem))
            except OSError:
                continue
        if found:
            found.sort()
            days.append({"date": folder.name, "ids": [name for _, name in found]})
            total += len(found)
    return {"days": days, "count": total}


def size_bytes() -> int:
    if not PHOTO_DIR.exists():
        return 0
    return sum(p.stat().st_size for p in PHOTO_DIR.rglob("*.jpg"))
