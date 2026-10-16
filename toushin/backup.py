"""貯めたデータの控えを、Google ドライブ（<バックアップ先>\\toushin\\）へ取る係。

  python backup.py      いま1回（データが前の控えから変わっていなければ、何もしない）

notify.py（予約タスク Toushin-Notify・毎朝9時）が、お知らせの前に呼ぶ。
控え＝toushin_YYYY-MM-DD.zip（toushin.db を SQLite の backup で写したもの・stars.json・picks.json・holdings.json＝買った値段）。新しい14個を残す。
同じ置き場にある、ほかの控えには触らない（別の箱に置く）。
TOUSHIN_BACKUP_DIR で、確かめ役が一時フォルダに向ける。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import store

DEST = Path(os.environ.get("TOUSHIN_BACKUP_DIR") or r"<バックアップ先>\toushin")
KEEP = 14


def _changed_since(last: Path | None) -> bool:
    if last is None:
        return True
    newest = max((p.stat().st_mtime for p in (store.DB, store.STARS, store.PICKS, store.HOLDINGS) if p.exists()), default=0)
    return newest > last.stat().st_mtime


def run(today: date | None = None) -> dict:
    today = today or date.today()
    if not store.DB.exists():
        return {"made": False, "why": "まだデータがありません"}
    if not DEST.parent.exists():
        return {"made": False, "why": f"控えの置き場が見えません（{DEST.parent}）"}
    DEST.mkdir(exist_ok=True)
    olds = sorted(DEST.glob("toushin_*.zip"))
    if not _changed_since(olds[-1] if olds else None):
        return {"made": False, "why": "前の控えから変わっていません"}
    out = DEST / f"toushin_{today.isoformat()}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "toushin.db"
        src, dst = sqlite3.connect(store.DB), sqlite3.connect(snap)
        try:
            src.backup(dst)         # 書き込み中でも、そろった形で写す
        finally:
            dst.close()
            src.close()
        part = out.with_suffix(".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "toushin.db")
            for extra in (store.STARS, store.PICKS, store.HOLDINGS):
                if extra.exists():
                    z.write(extra, extra.name)
        part.replace(out)
    removed = []
    for old in sorted(DEST.glob("toushin_*.zip"))[:-KEEP]:
        old.unlink()
        removed.append(old.name)
    return {"made": True, "file": str(out), "removed": removed}


if __name__ == "__main__":
    print(date.today().isoformat(), run())
