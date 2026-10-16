# -*- coding: utf-8 -*-
"""クロード極 — 手帳のベクトル土台（別のアプリの memory_embedder.py ＋ memory_store.py の移植）

持ち主の指示（2026-09-02）：
  - 元にしたアプリのベクトルメモリを、クロード極にもつける
  - 話したことを勝手に覚えていく（日記の形でなくてよい。手帳・メモの中身で）

★ここは「思い出す土台」だけを持つ。何を覚えるか・どう差し込むかは techo.py の役目。
★**この中の関数は、どれも例外を外へ投げない。** 手帳が使えなくても、極の会話は必ず動く。
  開発用PCにはモデルの実体が無い（server/models/ は .gitignore）ので、
  「読めなければ静かに休む」が既定の姿。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import anyio

# ── 埋め込みモデル ────────────────────────────────────────
# cl-nagoya/ruri-v3-130m … 日本語特化（ModernBERT-Ja ベース）の 512 次元モデル。
# 元にしたアプリが 2026-07-10 から使っている実績のあるもので、**同じ写しをそのまま読む**
# （513MB。極のために2つ目を置かない＝ディスクは1バイトも増えない）。
# ★モデルを変えたら索引を全部消して作り直すこと（次元が同じでも旧ベクトルとは互換ゼロ。
#   混ざると検索が静かにでたらめになる）。techo.py の距離しきい値も実測し直しになる。
_LOCAL_MODEL_DIR = Path(__file__).resolve().parent / "models" / "ruri-v3-130m"
_HAS_LOCAL_MODEL = (_LOCAL_MODEL_DIR / "model.safetensors").exists()
MODEL_DIR = str(_LOCAL_MODEL_DIR) if _HAS_LOCAL_MODEL else "cl-nagoya/ruri-v3-130m"

_model = None
_load_failed: str | None = None   # 一度でも読み込みに失敗したら理由を保持し、二度と再読み込みしない
# 読み込みの鍵。★起動時の温め（別スレッド）の最中に話しかけられると、
#   両方が「まだ読み込まれていない」を見て**モデルを二重に読む**（実測17秒・数百MBずつ）。
#   古いPCなので、ここは1本に絞る（2026-09-02・レビューでの指摘）。
_load_lock = threading.Lock()


def _deps_ok() -> bool:
    """部品が入っているか。**重い import はしない**（存在を見るだけ）。"""
    try:
        import importlib.util as u
        return bool(u.find_spec("sentence_transformers") and u.find_spec("chromadb"))
    except Exception:
        return False


def available() -> bool:
    """手帳が使える環境か。部品があって、モデルの実体があるか。

    ★モデルの読み込み（約12秒）はしない。画面や起動の判定に何度でも使えるようにするため。
    ★HF Hub 名へのフォールバックは「初回だけネットが要る」ので、実体が無いときは False。
      開発用PCで勝手に513MBを落としに行かせず、**静かに休む**のが既定の姿。
    """
    if _load_failed is not None:
        return False
    return _deps_ok() and _HAS_LOCAL_MODEL


def _get_model():
    """埋め込みモデルを遅延ロード。読み込みが失敗したら以後は即エラー（再読み込みしない）。

    ★torch/DLL が壊れた環境では『2度目の読み込み』がプロセスごと落とす native クラッシュに
      なりうる（元にしたアプリの memory_embedder.py の実測）。初回失敗を覚えて、危険な再ロードをしない。
    """
    global _model, _load_failed
    if _load_failed is not None:
        raise RuntimeError("手帳の埋め込みが使えません: " + _load_failed)
    if _model is not None:
        return _model                     # 読み込み済みなら鍵を取らずに返す（速い道）
    with _load_lock:
        if _load_failed is not None:      # 鍵を待つあいだに、別のスレッドが失敗していた
            raise RuntimeError("手帳の埋め込みが使えません: " + _load_failed)
        if _model is None:
            try:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_DIR)
            except BaseException as e:
                _load_failed = f"{type(e).__name__}: {e}"
                raise
    return _model


def _encode_sync(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    # normalize_embeddings=True … cosine 距離で扱うため正規化しておく
    vecs = model.encode(list(texts), normalize_embeddings=True)
    return [v.tolist() for v in vecs]


async def embed(texts: list[str], *, query: bool = False) -> list[list[float]]:
    """文章のリストを 512 次元ベクトルへ（CPU・threadpool・**0円**）。

    query=True は検索する側（持ち主の発話）、False は保存する側（手帳の本文）。
    Ruri v3 は「1+3 接頭辞」設計で、クエリ側「検索クエリ: 」・文書側「検索文書: 」を
    付けて埋め込むと精度が出る（付け忘れると劣化する。モデルカードの仕様）。
    プレフィックスは埋め込みの計算にだけ使い、保存する本文には付かない。
    失敗したら空リスト＝呼んだ側は「思い出さない」だけで、会話は止まらない。
    """
    if not texts:
        return []
    prefix = "検索クエリ: " if query else "検索文書: "
    try:
        return await anyio.to_thread.run_sync(_encode_sync, [prefix + t for t in texts])
    except BaseException as e:
        print(f"[techo] ベクトル化に失敗: {type(e).__name__}: {e}")
        return []


def warm_up() -> None:
    """起動直後にモデルを読み込んで、初回の待ち時間（約12秒）を消す（同期・重い）。
    呼ばなくても遅延ロードで動く。失敗しても静かに休むだけ。"""
    if not available():
        return
    try:
        _get_model()
        print("[techo] 手帳の準備ができました")
    except BaseException as e:
        print(f"[techo] モデルを読めませんでした（手帳は休みます）: {type(e).__name__}: {e}")


# ── 索引（ChromaDB・永続・cosine）─────────────────────────
def _is_ascii(p: Path) -> bool:
    try:
        str(p).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _default_db_dir() -> Path:
    """索引の置き場所を決める。

    ★元にしたアプリの memory_store.py の実測をそのまま引き継ぐ。
      索引の実体（data_level0.bin ほか）を書くのは C++ 側（hnswlib）で、
      **Windows では日本語や全角記号を含むパスへ、例外を1つも出さずに書けない**。
      置き場所のユーザー名に日本語が入る環境では、ここが黙って失敗する。
      そのまま WAL だけが刈られて索引が戻せなくなる＝元にしたアプリで4回起きた「記憶0件」の正体。

    英数字だけのパスならその場所を使い、**非ASCIIを含むときだけ**英数字フォルダへ逃がす。
    `KIWAMI_TECHO_DB` を立てれば、そちらが最優先。
    """
    env = os.environ.get("KIWAMI_TECHO_DB", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent / "data" / "techo_db"
    if _is_ascii(here):
        return here      # 英数字だけ＝そのまま。開発環境はここで返る
    # 逃がし先。ProgramData は Windows がどの環境でも英数字で持ち、ふつうの利用者でも書ける。
    program_data = os.environ.get("ALLUSERSPROFILE") or f"{here.drive}/ProgramData"
    for cand in (Path(program_data) / "kiwami" / "kiwami_techo_db",
                 Path(f"{here.drive}/kiwami_data/kiwami_techo_db")):
        if not _is_ascii(cand):
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
        except Exception:
            continue     # 作れない場所は候補から外す（黙って壊れるより、次を試す）
        return cand
    return here          # どこにも逃がせない＝これ以上は打つ手がない


TECHO_DB_DIR = _default_db_dir()
_COLLECTION_NAME = "kiwami_techo"
_client = None
_collection = None

_EMPTY = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


def _get_collection():
    global _client, _collection
    if _collection is None:
        import chromadb
        TECHO_DB_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(TECHO_DB_DIR))
        _collection = _client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},   # 距離は cosine（0=同じ, 2=真逆）
        )
    return _collection


def _add_sync(ids, embeddings, documents, metadatas) -> None:
    _get_collection().add(ids=ids, embeddings=embeddings,
                          documents=documents, metadatas=metadatas)


async def add(ids: list[str], embeddings, documents: list[str], metadatas: list[dict]) -> bool:
    """索引へ入れる。失敗しても False を返すだけ（原本は techo.py が別に残している）。"""
    if not ids:
        return False
    try:
        await anyio.to_thread.run_sync(_add_sync, ids, embeddings, documents, metadatas)
        return True
    except Exception as e:
        print(f"[techo] 索引に入れられませんでした: {type(e).__name__}: {e}")
        return False


def _query_sync(embedding, n_results, where):
    kw = {"query_embeddings": [embedding], "n_results": n_results,
          "include": ["documents", "metadatas", "distances"]}
    if where:
        kw["where"] = where
    return _get_collection().query(**kw)


async def query(embedding, n_results: int = 20, where: dict | None = None) -> dict:
    """似ているものを探す。**where で袋（pid）を必ず閉じること**（袋はまたがない）。
    失敗・0件のときは空の形をそのまま返す＝呼んだ側は分岐を増やさずに済む。"""
    if not embedding:
        return _EMPTY
    try:
        got = await anyio.to_thread.run_sync(_query_sync, embedding, n_results, where)
        return got or _EMPTY
    except Exception as e:
        print(f"[techo] 検索に失敗: {type(e).__name__}: {e}")
        return _EMPTY


def _delete_ids_sync(ids) -> None:
    _get_collection().delete(ids=list(ids))


async def delete_ids(ids: list[str]) -> int:
    """id を名指しで消す。消した件数を返す。"""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return 0
    try:
        await anyio.to_thread.run_sync(_delete_ids_sync, ids)
        return len(ids)
    except Exception as e:
        print(f"[techo] 索引から消せませんでした: {type(e).__name__}: {e}")
        return 0


def _count_sync(where) -> int:
    col = _get_collection()
    if not where:
        return col.count()
    got = col.get(where=where, include=[])
    return len(got.get("ids") or [])


async def count(where: dict | None = None) -> int:
    try:
        return await anyio.to_thread.run_sync(_count_sync, where)
    except Exception as e:
        print(f"[techo] 件数を数えられませんでした: {type(e).__name__}: {e}")
        return 0


def _reset_sync() -> None:
    global _client, _collection
    import chromadb
    TECHO_DB_DIR.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(TECHO_DB_DIR))
    try:
        _client.delete_collection(name=_COLLECTION_NAME)
    except Exception:
        pass                      # まだ無い＝消すものが無いだけ
    _collection = _client.get_or_create_collection(
        name=_COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


async def reset() -> bool:
    """索引を空にして作り直す（techo.rebuild が原本から入れ直すときに使う）。
    **原本（data/techo/*.jsonl）は消さない**ので、ここを空にしても失うものは無い。"""
    try:
        await anyio.to_thread.run_sync(_reset_sync)
        return True
    except Exception as e:
        print(f"[techo] 索引を作り直せませんでした: {type(e).__name__}: {e}")
        return False
