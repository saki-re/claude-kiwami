# -*- coding: utf-8 -*-
"""クロード極 — 手帳（話したことを、話題ごとの粒にして勝手に覚えていく）

持ち主の指示：
  2026-09-02 … 元にした別のアプリのベクトルメモリを、クロード極にもつける。日記でなくてよい。手帳・メモの中身で
  2026-09-03 … 手帳に話した内容そのままが入っていたので、話題ごとの粒の形に直す。
  → **手帳に入るのは粒（話題ごとの段落）だけ。会話そのままは入れない。**
     元にしたアプリの記憶と同じ形 ── 会話は会話ログ（data/threads）に置いたまま、そこから書いた段落だけが記憶になる。
     粒を書くのは techo_writer.py（Sonnet 5・元にした日記プロンプトと同じ骨組み。2026-09-12 に Haiku から上げた）。
  - **いまの「おぼえていること」（memory.py の差し込み）はそのまま置く** … 手帳はその横に足す

★2026-08-20、元にしたアプリで「勝手に覚える係」をやめたときの理由が3つある。
  消す内容が多い・記憶にならないものが混ざる・文字数がどんどん伸びる。
  手帳は、その3つが起きない形にしてある ──
    伸びない … 貯めるのは外（ここ）で、話に関係するものだけを思い出す
    拾い漏れない … 会話そのものはスレッドに全部残る。粒は日ごと・話題ごとに書く
    消す手間 … 粒は「何の話をして、何が決まったか」だけ。画面から1件ずつ直せる・忘れられる

★**原本を必ずファイルに残す。** 索引（ベクトル）が壊れても、原本から何度でも
  作り直せる（rebuild・**生成AI 0回・0円**）。元にしたアプリが日記から作り直せるのと同じ安心の形。
  原本ごと失っても、会話ログから書き直せる（POST /techo/redigest・Sonnet 5）。
★**袋（pid）はまたがない**（持ち主の決めごと・2026-08-27）。検索も一覧も pid で閉じる。
★**例外を外へ投げない。** 手帳が使えなくても、極の会話は必ず動く。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import config
import techo_vec

# ── 目盛り（元にしたアプリの memory_service.py の実測値を引き継ぐ。勝手に変えない）──────
MIN_QUERY_CHARS = 8       # これ未満の相槌（「うん」）では思い出さない＝ほぼランダム想起になるため
TOP_K = 20                # 索引から生で取る候補の数
MAX_DISTANCE = 0.195      # これより遠いものは捨てる（cosine・Ruri v3 での実測値）
MARGIN = 0.035            # 1位との差がこれを超えたら捨てる（1位だけ当たっているときの巻き添え防止）

# ── 差し込みの上限（持ち主の決めごと・2026-09-02）────────────
# ★**絞るのは件数だけ。字数では文を切らない。**
#   件数は的外れが混ざるので絞ってよいが、文字数は気にしない、という方針。
#   いちど 3件・合計400字・1件150字まで締めてみたが、**答えの手前で文が切れた**。
#   思い出せない思い出は、積むだけ無駄になる。
# ★合計の上限は「万一のときの手すり」として残すが、**文の途中では切らない**。
#   超えたときは、その1件を丸ごと入れないだけ（意味の通らない切れ端を作らない）。
MAX_RECALL = 3            # 差し込む件数の上限。★的外れが混ざるのを防ぐのは、こちらの役目
MAX_BLOCK_CHARS = 4000    # 合計の手すり。1粒は長くて600字（techo_writer.GRAIN_MAX_CHARS）×3件なので、ふだんは当たらない

_ROOT = config.DATA_DIR / "techo"
_ROOT.mkdir(parents=True, exist_ok=True)


def enabled() -> bool:
    """手帳が使えるか（部品とモデルがそろっているか）。"""
    return techo_vec.available()


# ── 原本（data/techo/<袋>/<日付>.jsonl）───────────────────
_PID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_pid(pid: str) -> str | None:
    """袋の名前として安全か。安全でなければ None。

    ★8030 はネットワークに公開されている。`?persona=../x` のような値を
      そのままフォルダ名にすると、**袋の外にフォルダが作れる＝「袋はまたがない」の壁が
      URL 1本で越えられる**（2026-09-02・レビューでの指摘）。
      袋の id は英数字と `_ -` だけ（`projects.create` が作るのは16進の12桁）。
    """
    s = str(pid or "").strip()
    return s if _PID_OK.match(s) else None


def _dir(pid: str) -> Path:
    safe = safe_pid(pid)
    if safe is None:
        raise ValueError(f"袋の名前として使えません: {pid!r}")
    d = _ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _day_file(pid: str, day: str) -> Path:
    return _dir(pid) / f"{day}.jsonl"


def _append(pid: str, rec: dict) -> None:
    """原本へ1行足す。**索引より先に、必ずここへ書く**（あとで作り直せるように）。"""
    p = _day_file(pid, rec["date"])
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_all(pid: str) -> list[dict]:
    """その袋の原本を全部読む（新しい順）。壊れた行は飛ばす（会話は止めない）。"""
    if safe_pid(pid) is None:
        return []                      # 使えない袋の名前＝何も無いのと同じに扱う
    out = []
    for p in sorted(_dir(pid).glob("*.jsonl")):
        try:
            # utf-8-sig … PowerShell が付けた BOM があっても読めるように（この家の決まり）
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def _norm(s: str) -> str:
    """空白と大小のゆれを落として突き合わせる（memory.py と同じ考え方）。"""
    return re.sub(r"[\s　]+", "", (s or "")).lower()


# ── 覚える ─────────────────────────────────────────────
async def _save(pid: str, kind: str, thread_id: str, text: str,
                day: str | None = None, ts: str | None = None) -> str | None:
    """1件を、原本 → 索引 の順で入れる。索引に入らなくても原本には残す。"""
    text = (text or "").strip()
    if not text or safe_pid(pid) is None:
        return None
    now = datetime.now()
    ts = ts or now.isoformat(timespec="seconds")
    # 日付は ts から取る。★過去の会話を書き直すとき、全部が「今日」になってしまうため
    #   （原本のファイル名も日付なので、ここを間違えると当時の並びが失われる）。
    if not day:
        day = ts[:10] if len(ts) >= 10 and ts[4] == "-" else now.strftime("%Y-%m-%d")
    rec = {
        "id": uuid.uuid4().hex,
        "pid": str(pid),
        "kind": kind,
        "thread": str(thread_id or ""),
        "date": day,
        "ts": ts,
        "text": text,
    }
    try:
        _append(pid, rec)                      # ★先に原本。ここが本体
    except OSError as e:
        print(f"[techo] 原本に書けませんでした: {type(e).__name__}: {e}")
        return None

    vecs = await techo_vec.embed([text])       # 0円（このPCの中の計算）
    if vecs:
        await techo_vec.add(
            [rec["id"]], vecs, [text],
            [{"pid": rec["pid"], "kind": kind, "date": rec["date"], "thread": rec["thread"]}],
        )
    return rec["id"]


async def remember_note(pid: str, thread_id: str, text: str,
                        day: str | None = None) -> str | None:
    """粒を1件覚える。同じ袋に同じ本文が既にあれば覚えない（重複よけ）。

    ★手帳に入るのは、これだけ（2026-09-03）。会話そのままは入れない。
      粒を書くのは techo_writer.py（話題ごとの段落・その話をした日の日付で）。
    """
    text = (text or "").strip()
    if not text:
        return None
    key = _norm(text)
    for r in _read_all(pid):
        if _norm(r.get("text", "")) == key:
            return None
    return await _save(pid, "note", thread_id, text, day=day)


# ── 思い出す ───────────────────────────────────────────
def _one_line(s: str) -> str:
    """差し込む1行にするため、改行を潰す（箇条書きが崩れないように）。"""
    return re.sub(r"\s*\n+\s*", " ／ ", (s or "").strip())


async def recall(pid: str, query_text: str) -> str:
    """思い出す。**差し込む文字列そのもの**を返す。何も無ければ空文字。

    ★呼ぶ側（agent_bridge）は、これを持ち主の発言のほうへ添える。
      システムプロンプトに入れてはいけない（resume のキャッシュが毎ターン壊れて高くつく）。
    """
    q = (query_text or "").strip()
    if len(q) < MIN_QUERY_CHARS:
        return ""                     # 相槌では引かない（ほぼランダム想起になるため）
    if not enabled():
        return ""
    vecs = await techo_vec.embed([q], query=True)
    if not vecs:
        return ""
    got = await techo_vec.query(vecs[0], n_results=TOP_K, where={"pid": str(pid)})

    docs = (got.get("documents") or [[]])[0] or []
    metas = (got.get("metadatas") or [[]])[0] or []
    dists = (got.get("distances") or [[]])[0] or []
    if not docs:
        return ""

    rows = []
    best = min(dists) if dists else 0.0
    for doc, meta, dist in zip(docs, metas, dists):
        if dist > MAX_DISTANCE:
            continue
        if (dist - best) > MARGIN:
            continue                  # 1位だけ当たっている回の巻き添えを防ぐ
        rows.append({"text": doc, "meta": meta or {}, "dist": dist})
    if not rows:
        return ""
    rows.sort(key=lambda r: r["dist"])

    lines, total = [], 0
    for r in rows[:MAX_RECALL]:
        body = _one_line(r["text"])      # ★途中で切らない（切ると答えの手前で終わる）
        day = r["meta"].get("date") or ""
        line = f"- （{day}）{body}" if day else f"- {body}"
        if total + len(line) > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    head = "【手帳】前に話したことです。関係なければ使わなくてよいです。"
    return head + "\n" + "\n".join(lines)


def _words(keyword: str) -> list[str]:
    """探す言葉を、空白で区切って1語ずつにする（「電気代 650」→ 電気代・650）。"""
    return [w for w in (_norm(x) for x in re.split(r"[\s　]+", keyword or "")) if w]


async def search_all(pid: str, keyword: str, n_vec: int = TOP_K) -> list[dict]:
    """当たった粒を**全部**、残す順（文字が合うもの → 意味が近いもの）に並べて返す。

    ★文字が合う粒を先に置く。2026-10-14、ある言葉で引くと文字の合う粒が16件あり、
      新しい順に8件で切ったせいで、古い粒が落ちた。
      極には「ほかにもある」と伝わらず、持ち主に「出どころがない」と答えた。
    ★空白で区切った言葉は、**全部を含む粒**だけを文字の一致にする（絞り込み用）。
      意味の近さのほうは、言葉をそのまま1つの問いとして引く。
    """
    kw = (keyword or "").strip()
    rows = _read_all(pid)                      # 新しい順。id を引くのにも使う
    words = _words(kw)
    lit, vec, seen = [], [], set()

    # ① 文字の一致。金額や固有名詞のように「意味は遠いが文字は同じ」ものに当てる。
    #   言葉が空なら全部（画面の一覧と同じ）。
    for r in rows:
        k = _norm(r.get("text", ""))
        if k in seen or not all(w in k for w in words):
            continue
        seen.add(k)
        lit.append(dict(r))

    # ② 意味の近さ。★①の件数に関わらず必ず回す（2026-09-02・レビューでの指摘）。
    if kw and enabled():
        vecs = await techo_vec.embed([kw], query=True)
        if vecs:
            by_text = {_norm(r.get("text", "")): r for r in rows}
            got = await techo_vec.query(vecs[0], n_results=max(n_vec, TOP_K),
                                        where={"pid": str(pid)})
            docs = (got.get("documents") or [[]])[0] or []
            dists = (got.get("distances") or [[]])[0] or []
            for doc, dist in zip(docs, dists):     # 近い順に並んで返ってくる
                if dist > MAX_DISTANCE:
                    continue
                k = _norm(doc)
                if k in seen:
                    continue
                src = by_text.get(k)
                if not src:
                    continue          # 原本にもう無いぶんは出さない
                seen.add(k)
                vec.append(dict(src))
    return lit + vec


async def search(pid: str, keyword: str, limit: int = 10) -> list[dict]:
    """意味の近さ（ベクトル）と、文字の一致（原本）の**両方**で引く。

    ★両方を使うのは、金額や固有名詞のように「意味は遠いが文字は同じ」ものにも
      当てるため（ベクトルだけだと数字の並びを取りこぼす）。
    ★返すのは原本の1件まるごと（id・kind・date・ts・thread・text）。
      画面の検索窓が、そのまま直したり消したりできるようにするため（2026-09-02）。
    ★limit で切るのは search_all の残す順（文字が合うものが先）。並べるのは新しい順。
    """
    out = (await search_all(pid, keyword, n_vec=limit))[:limit]
    out.sort(key=lambda h: h.get("ts", ""), reverse=True)   # 新しい順で返す
    return out


async def update(pid: str, tid: str, text: str) -> dict | None:
    """1件を書き直す（持ち主が画面から直すときに使う）。

    原本の行を差し替えて、索引も同じ id で入れ直す。
    ★索引を入れ直さないと、**画面の字は変わったのに、思い出すのは古い文のまま**になる。
      見えているものと思い出すものがずれるのが、いちばん困る。
    """
    text = (text or "").strip()
    if not text or not tid or safe_pid(pid) is None:
        return None

    hit = None
    for p in _dir(pid).glob("*.jsonl"):
        try:
            lines = p.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        keep, changed = [], False
        for line in lines:
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except json.JSONDecodeError:
                keep.append(s)
                continue
            if rec.get("id") == tid:
                rec["text"] = text
                rec["edited"] = datetime.now().isoformat(timespec="seconds")
                hit = rec
                changed = True
                s = json.dumps(rec, ensure_ascii=False)
            keep.append(s)
        if changed:
            # 一時ファイル経由で置き換える（途中で落ちても原本が消えないように）
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text(("\n".join(keep) + "\n") if keep else "", encoding="utf-8")
            tmp.replace(p)
            break
    if not hit:
        return None

    await techo_vec.delete_ids([tid])
    vecs = await techo_vec.embed([text])
    if vecs:
        await techo_vec.add(
            [tid], vecs, [text],
            [{"pid": str(hit.get("pid", pid)), "kind": hit.get("kind", "note"),
              "date": hit.get("date", ""), "thread": hit.get("thread", "")}],
        )
    return hit


# ── 画面用（ベクトルを使わない＝速い・モデルが無くても動く）───────
def listing(pid: str, limit: int = 50) -> list[dict]:
    """原本から新しい順に。手帳の一覧はこれで足りる。"""
    return _read_all(pid)[:limit]


def _stats_of(rows: list[dict]) -> dict:
    return {
        "note": len(rows),
        "days": len({r.get("date", "") for r in rows if r.get("date")}),
        "enabled": enabled(),
    }


def panel(pid: str, limit: int = 50) -> dict:
    """画面用。**原本を1回だけ読んで**、一覧と件数をまとめて返す。

    ★listing() と stats() を続けて呼ぶと、同じファイルを2回丸読みする。画面の口はここを使う。
    """
    rows = _read_all(pid)
    return {"items": rows[:limit], "stats": _stats_of(rows)}


def stats(pid: str) -> dict:
    """{"note": 粒の数, "days": 日数, "enabled": bool}。原本から数える。"""
    return _stats_of(_read_all(pid))


async def forget(pid: str, tid: str) -> bool:
    """1件わすれる。原本の行を消し、索引からも消す。"""
    tid = (tid or "").strip()
    if not tid or safe_pid(pid) is None:
        return False
    hit = False
    for p in _dir(pid).glob("*.jsonl"):
        try:
            lines = p.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        keep = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            try:
                if json.loads(s).get("id") == tid:
                    hit = True
                    continue
            except json.JSONDecodeError:
                pass
            keep.append(s)
        if hit:
            # 一時ファイル経由で置き換える（途中で落ちても原本が消えないように）
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text(("\n".join(keep) + "\n") if keep else "", encoding="utf-8")
            tmp.replace(p)
            break
    if hit:
        await techo_vec.delete_ids([tid])
    return hit


async def clear(pid: str) -> int:
    """その袋の手帳を空にする（原本も索引も）。消した件数を返す。

    ★書き直し（techo_writer.backfill wipe=True）の前にだけ使う。
      呼ぶ側が先に写しを取っておく。会話ログ（data/threads）は触らない。
    """
    if safe_pid(pid) is None:
        return 0
    rows = _read_all(pid)
    ids = [r["id"] for r in rows if r.get("id")]
    if ids:
        await techo_vec.delete_ids(ids)
    for p in _dir(pid).glob("*.jsonl"):
        try:
            p.unlink()
        except OSError as e:
            print(f"[techo] 原本を消せませんでした: {p.name}: {type(e).__name__}: {e}")
    return len(rows)


async def rebuild(pid: str | None = None) -> int:
    """原本から索引を作り直す。**生成AIを1回も呼ばない＝0円。**

    ★索引（ベクトル）が壊れても、ここを1回走らせれば戻る。落ち着いて直せる形。
    """
    if not enabled():
        return 0
    pids = [pid] if pid else [d.name for d in _ROOT.iterdir() if d.is_dir()]
    if pid:
        ids = [r["id"] for r in _read_all(pid) if r.get("id")]
        await techo_vec.delete_ids(ids)
    else:
        await techo_vec.reset()

    total = 0
    for one in pids:
        rows = [r for r in _read_all(one) if (r.get("text") or "").strip()]
        for i in range(0, len(rows), 32):          # 32件ずつ（まとめて渡すほうが速い）
            batch = rows[i:i + 32]
            vecs = await techo_vec.embed([r["text"] for r in batch])
            if not vecs:
                continue
            ok = await techo_vec.add(
                [r["id"] for r in batch], vecs, [r["text"] for r in batch],
                [{"pid": str(r.get("pid", one)), "kind": r.get("kind", "note"),
                  "date": r.get("date", ""), "thread": r.get("thread", "")} for r in batch],
            )
            if ok:
                total += len(batch)
    return total
