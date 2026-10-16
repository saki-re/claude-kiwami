# -*- coding: utf-8 -*-
"""クロード極 — プロジェクト（人格ごとの箱・2026-08-02 新設）

持ち主の指示：
  - 公式クロードのように**プロジェクト単位**でメモリを持ちたい
  - シスプロもプロジェクトごとに指定したい
  - 実務（いまの1）とクロード（いまの2）は**そのまま**

1プロジェクト＝1ファイル（data/projects/<id>.json）。
シスプロ本体は別ファイル（config.sys_file を参照）、
メモリは袋ごと（data/memory/<id>/）に分かれている。

★ID は persona の文字列をそのまま引き継いでいる（"1"・"2"・"3"）。
  持ち主が画面から作るものだけ uuid になる。どちらもただの文字列。
"""
import json
import uuid

import config

_KEYS = ("name", "core", "memory", "order")


def _path(pid: str):
    return config.PROJ_DIR / f"{pid}.json"


def _write(d: dict) -> dict:
    _path(d["id"]).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def ensure_defaults() -> None:
    """出荷時の3つが無ければ作る。**すでにあるものには触らない**（持ち主の編集を消さない）。"""
    for d in config.DEFAULT_PROJECTS:
        if not _path(d["id"]).exists():
            _write(dict(d))


def listing() -> list[dict]:
    """画面の選択欄と絞り込みに出す一覧。order の小さい順。"""
    ensure_defaults()
    out = []
    for p in config.PROJ_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda d: (d.get("order", 999), d.get("name", "")))
    return out


def get(pid: str) -> dict:
    """1件。知らない ID が来ても落とさない。

    知らない ID は「素のクロード」相当（表に出ないシスプロなし・メモリなし）にしておく。
    データが無いのにメモリを繋ぐほうが害になるため、安全側はこちら。
    """
    ensure_defaults()
    p = _path(str(pid))
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"id": str(pid), "name": "", "core": False, "memory": False, "order": 999}


def name_of(pid: str) -> str:
    """画面のヘッダーやスレッド一覧に出す表示名。"""
    return get(pid).get("name") or "（不明）"


def is_default(pid: str) -> bool:
    return str(pid) in {d["id"] for d in config.DEFAULT_PROJECTS}


def create(name: str = "", core: bool = False, memory: bool = True) -> dict:
    pid = uuid.uuid4().hex[:12]
    d = {
        "id": pid,
        "name": (name or "").strip() or "新しいプロジェクト",
        "core": bool(core),
        "memory": bool(memory),
        "order": 900,
    }
    # 空のシスプロを置いておく（画面の編集欄がすぐ開けるように）
    config.sys_file(pid).write_text("", encoding="utf-8")
    return _write(d)


def update(pid: str, **kw) -> dict:
    d = get(pid)
    d["id"] = str(pid)
    for k in _KEYS:
        if kw.get(k) is not None:
            d[k] = kw[k]
    return _write(d)


def delete(pid: str) -> bool:
    """出荷時の3つは消さない（実務を誤って消すと戻せないため）。

    プロジェクトの札とシスプロは消すが、**メモリの袋は残す**。
    間違って消したときに、袋だけは戻せるようにしておく。
    """
    if is_default(pid):
        return False
    p = _path(str(pid))
    if not p.exists():
        return False
    p.unlink()
    f = config.sys_file(pid)
    if f.exists():
        f.unlink()
    return True
