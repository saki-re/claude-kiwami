# -*- coding: utf-8 -*-
"""クロード極 — サーバー本体（8030）

いまここにあるのは「喋れる細い1本」＋シスプロの読み書き。
スレッド・ログ・メモリ・声かけは、この上に足していく。
"""
import asyncio
import contextlib
import gzip
import hashlib
import json
import time
from pathlib import Path

import anyio
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

import agent_bridge
import config
import memory
import photo_store
import file_blocks
import proactive
import projects
import reminder_app
import store

try:
    import techo, techo_vec         # 手帳（話したことを粒にして勝手に覚える）。土台は techo.py / techo_vec.py
except Exception as e:               # 部品やモデルが無い環境でも極は今までどおり動く
    techo = techo_vec = None
    print(f"[techo] 手帳は使えません: {type(e).__name__}: {e}")
try:
    import jijitsu_guard                # 事実の見張り（2026-09-24）。ここでは門だけ使う（返事の上限を広げる）
except Exception as _e:                 # noqa: BLE001
    jijitsu_guard = None
    print(f"[jijitsu] 事実の見張りは使えません（上限は今までどおり）: {type(_e).__name__}: {_e}")
try:
    import techo_writer             # 手帳の粒を書く係（書き直しの口 /techo/redigest が使う）
except Exception as e:
    techo_writer = None
    print(f"[techo] 粒を書く係は使えません: {type(e).__name__}: {e}")

app = FastAPI(title="クロード極")
BASE_DIR = Path(__file__).resolve().parent

# ★API の `persona` は、2026-08-02 から「プロジェクト ID」を指す。
#   もとの "1"/"2" をそのまま ID として引き継いでいるので、名前は変えていない
#   （画面の送信・スレッドの persona 項目と揃えたまま中身だけ広げた形）。


@app.on_event("startup")
async def _start_background():
    """声かけの見張りを1本だけ動かす（オフのときは何もしないので、常に立てておく）。"""
    projects.ensure_defaults()      # 出荷時の3つを用意（あるものには触らない）
    memory.migrate_flat()           # 平置きだった記憶を実務の袋へ（1回きり・空振りなら何もしない）
    asyncio.create_task(proactive.loop())
    if techo_vec is not None:
        # モデルの読み込み（約12秒）をバックグラウンドで先に済ませておく。
        # 待たない＝起動そのものは塞がない。呼ばなくても遅延ロードで動くが、
        # 最初の1件が待たされないように、ここで温めておく。
        _techo_spawn(anyio.to_thread.run_sync(techo_vec.warm_up))


# ── 画面 ───────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """画面を配る口。

    ★2026-08-20 それまでは `no-cache, no-store, must-revalidate` ＝**キャッシュ完全禁止**で、
      開くたびに 115KB をまるごと落としていた。キャッシュ禁止は重くなるので撤去した。
      「キャッシュさせない作り（no-store）＝重くなる」ので、やらない。

      いまの形＝**gzip ＋ ETag(304) ＋ max-age=60**。
      実測 115KB → 31KB。1分以内の開き直しは通信0。変わっていなければ 304（本文0バイト）。

    ★「直した画面が届かない」は、版の仕組みで担保する（下の _ui_version_header）。
      画面に版を埋め、応答ヘッダの版と食い違ったら、画面が自分で1回読み直す。
    """
    path = BASE_DIR / "index.html"
    st = path.stat()
    ver = config.ui_version()
    etag = f'W/"{int(st.st_mtime)}-{st.st_size}"'
    base = {"ETag": etag, "Cache-Control": "max-age=60"}

    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=base)

    data = path.read_bytes().replace(b"__UI_VERSION__", ver.encode("utf-8"))
    if "gzip" in (request.headers.get("accept-encoding") or "").lower():
        data = gzip.compress(data, 6)
        base["Content-Encoding"] = "gzip"
        base["Vary"] = "Accept-Encoding"
    return Response(content=data, media_type="text/html; charset=utf-8", headers=base)


@app.middleware("http")
async def _ui_version_header(request: Request, call_next):
    """全部の応答に「いまの画面の版」を1つ付ける（2026-08-20）。

    クロード極には60秒ごとの見張りが無いので、専用の口では運べない。
    **新しい常駐やポーリングは作らず**、もともと画面が叩いている口の応答に相乗りさせる。

    ★返事（SSE）には付けない。ストリーミングのヘッダをいじらない。
    """
    response = await call_next(request)
    if "text/event-stream" not in (response.headers.get("content-type") or ""):
        v = config.ui_version()
        if v:
            response.headers["X-UI-Version"] = v
            # 画面側の fetch から読めるようにする（同じ生い立ちなので本来は不要だが、明示しておく）
            response.headers["Access-Control-Expose-Headers"] = "X-UI-Version"
    return response


def _json_gz(data, request: Request) -> Response:
    """JSON を返す。端末が gzip を受け取れるなら縮めて送る（2026-08-20）。

    実測：いちばん長いスレ 161KB → 55KB。スレ一覧も同じくよく縮む。
    ★ここだけで縮める。全体に GZip をかけると、返事（SSE）が途中で溜められて
      1文字ずつ流れてこなくなる。中身は1バイトも変えない。
    """
    raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    headers = {}
    if len(raw) > 4096 and "gzip" in (request.headers.get("accept-encoding") or "").lower():
        raw = gzip.compress(raw, 6)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(content=raw, media_type="application/json", headers=headers)


def _static_page(name: str, request: Request) -> Response:
    """単独のページを配る（2026-08-20）。

    それまでは `no-cache, no-store, must-revalidate` ＝ **毎回まるごと送っていた**
    （pokesleep 216KB／pokopia 220KB／pokesleep_iv 108KB）。

    ★ここは「版で読み直す仕組み」を持たない単独ページなので `max-age` は付けない。
      `no-cache` ＝ 毎回サーバーに聞く（＝いつでも最新のまま）。
      そのうえで **ETag が同じなら 304（本文0バイト）**、違えば **gzip** で送る。
      ＝ 直したものは今までどおり必ず届き、そのうえで軽くなる。
    """
    path = BASE_DIR / name
    st = path.stat()
    etag = f'W/"{int(st.st_mtime)}-{st.st_size}"'
    base = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=base)
    data = path.read_bytes()
    if "gzip" in (request.headers.get("accept-encoding") or "").lower():
        data = gzip.compress(data, 6)
        base["Content-Encoding"] = "gzip"
        base["Vary"] = "Accept-Encoding"
    return Response(content=data, media_type="text/html; charset=utf-8", headers=base)


# ── リマインダー（2026-09-19）───────────────────────
# 中身は reminder_app.py。道具（remind.py）から直接、外のアプリは下の POST /reminders/api から足す。
# iPhone のリマインダーへは、一覧と札の「iPhoneのリマインダーに入れる」ボタン（公式 Claude アプリへ渡す）から入れる。
@app.get("/rougo", response_class=HTMLResponse)
async def rougo(request: Request):
    """老後シミュレーター（2026-09-28）。

    年金・遺産・貯金・支出から「何歳まで持つか」を出す1枚もの。計算も保存（端末の localStorage）も
    画面の中で完結するので、サーバーは配るだけ。計算の正解は docs/rougo_keisan.py。
    """
    return _static_page("rougo.html", request)


@app.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request):
    """一覧の画面（reminders.html）。メニュー（スマホマーク）の「リマインダー」から開く。"""
    return _static_page("reminders.html", request)


class RemindIn(BaseModel):
    title: str = ""
    due: str = ""          # 'YYYY/MM/DD HH:MM'・'YYYY-MM-DDTHH:MM'（画面の日時の欄）・日付だけ・空
    notes: str = ""
    who: str = ""          # 登録した人（名前など）


class RemindDone(BaseModel):
    done: bool = True


class RemindEdit(BaseModel):
    title: str | None = None
    due: str | None = None


@app.get("/reminders/api")
async def reminders_list():
    return reminder_app.listing()


@app.post("/reminders/api")
async def reminders_add(req: RemindIn):
    row = reminder_app.add(req.title, req.due, req.notes, req.who)
    if isinstance(row, str):
        raise HTTPException(status_code=400, detail=row)
    return row


@app.post("/reminders/api/{rid}/done")
async def reminders_done(rid: int, req: RemindDone):
    row = reminder_app.set_done(rid, req.done)
    if row is None:
        raise HTTPException(status_code=404, detail="見つかりませんでした")
    return row


@app.patch("/reminders/api/{rid}")
async def reminders_edit(rid: int, req: RemindEdit):
    row = reminder_app.edit(rid, req.title, req.due)
    if row is None:
        raise HTTPException(status_code=404, detail="見つかりませんでした")
    if isinstance(row, str):
        raise HTTPException(status_code=400, detail=row)
    return row


@app.delete("/reminders/api/{rid}")
async def reminders_delete(rid: int):
    if not reminder_app.delete(rid):
        raise HTTPException(status_code=404, detail="見つかりませんでした")
    return {"deleted": rid}


# ── ほかの部屋（2026-09-06）────────────────────────
# ★図鑑と別画面を、
#   このサーバー（8030）の中から配ります。**新しく常駐するものは1つも増えません。**
#   スマホのホーム画面アプリから開いても、Safari へ飛び出さずに中で開けます。
@app.get("/rooms")
async def rooms():
    """スマホマーク（行き先メニュー）に、何を出せるか。画面が開いたときに1回だけ聞きます。

    ★入っていないものは、メニューにその行ごと出しません（手帳と同じ作法）。
    """
    # hikitsugi … スレ引き継ぎの口がある（2026-09-17）。画面は index.html をそのつど読むので、
    #   サーバーを起こし直す前から新しい画面が届く。この答えが無い＝古いサーバーのあいだは、
    #   ⋯ に「引き継いで新しいスレへ」を出さない（押しても何も起きない行を置かない）。
    # rougo … シミュレーターの口がある（2026-09-28）。無い＝古いサーバーのあいだは、メニューに行を出さない。
    # toushin … ランキングの道具の口がある（2026-10-01）。
    return {"zukan": config.zukan_ok(), "winpost": config.winpost_ok(), "hikitsugi": True, "rougo": True,
            "toushin": True}


# 図鑑（絵まで入った一式をそのまま配る）。
# ★`html=True` なので `/zukan/` で図鑑の index.html が開き、`/zukan/data/...` の
#   絵もそのまま届きます（図鑑の中は全部この場所からの相対で書かれているため）。
if config.zukan_ok():
    from fastapi.staticfiles import StaticFiles
    app.mount("/zukan", StaticFiles(directory=str(config.ZUKAN_DIR), html=True), name="zukan")
    print(f"[zukan] 図鑑を配ります: {config.ZUKAN_DIR}")
else:
    print(f"[zukan] 図鑑が見つからないので、メニューに出しません: {config.ZUKAN_DIR}")

# 別画面（もともと別のポートに立てる作り。ここでは間借りさせる）。
# ★`winpost/app.py` を、名前をずらして読み込みます。極自身も `app.py` という名前なので、
#   ふつうに import すると自分を読んでしまうためです。
if config.winpost_ok():
    try:
        import importlib.util
        import sys as _sys
        _wp_dir = str(config.WINPOST_DIR)
        if _wp_dir not in _sys.path:
            _sys.path.insert(0, _wp_dir)      # engine.py・game.py を読めるようにする
        _spec = importlib.util.spec_from_file_location(
            "winpost_app", config.WINPOST_DIR / "app.py")
        _wp = importlib.util.module_from_spec(_spec)
        _sys.modules["winpost_app"] = _wp
        _spec.loader.exec_module(_wp)
        app.mount("/winpost", _wp.app, name="winpost")
        print("[winpost] 別画面を配ります（名簿は初めて開かれたときに読みます）")
    except Exception as _e:
        print(f"[winpost] 別画面は配れません: {type(_e).__name__}: {_e}")


# ── ホーム画面アプリ（iPhone に「アプリとして」置くための2本）─────────────
# ★manifest は「/」直下から配る。/static のような下の階層に置くと、アプリの
#   受け持ち範囲がそこだけになる（2026-07-30 実測）。
@app.get("/manifest.json")
async def manifest():
    return FileResponse(
        BASE_DIR / "manifest.json",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icon.png")
async def icon():
    """ホーム画面のアイコン。作り直すときは claude/make_icon.py を走らせる。"""
    return FileResponse(BASE_DIR / "icon.png", media_type="image/png")


# ── 話す ───────────────────────────────────────────────
class ChatRequest(BaseModel):
    session_id: str = "default"
    text: str
    persona: str = "1"          # 1＝実務 / 2＝クロード
    account: str = "sdk1"       # sdk1 / sdk2（5時間枠の切り替え）
    model: str = ""
    effort: str | None = None
    web_search: bool = False
    # 別画面の道具（2026-09-04）。入のときだけ道具一式が載る＝切なら今までどおりの軽さ
    winpost: bool = False
    # 図鑑の道具（2026-09-06）。同じく、入のときだけ道具一式が載る
    pokedex: bool = False
    # Amazonの道具（2026-09-07）。同じく、入のときだけ。買う道具は入っていない
    amazon: bool = False
    # シミュレーターの道具（2026-09-29）。同じく、入のときだけ。0円
    rougo: bool = False
    # ランキングの道具（2026-10-01）。同じく、入のときだけ。0円・GET で聞くだけ
    toushin: bool = False
    # 事実の見張り（2026-09-24）。既定は入。切ると SDK にフックを渡さない＝今までどおり
    guard: bool = True
    seed: str = ""              # CLI セッションが無いときに送る「これまでの流れ」
    # 持ち主が送った写真（data URL）。★履歴には入らない。届いたターンにだけ渡す。
    images: list[str] = Field(default_factory=list)
    # 持ち主が送ったファイル（PDF・文字のファイル）（2026-10-01）。
    # [{"name","type","data"(data URL)}]。写真と同じく、履歴には目印の1行だけ。
    files: list[dict] = Field(default_factory=list)


async def _take_photos(req: ChatRequest):
    """送られた写真をあずかって、(写真の名札, アシスタントに渡す image ブロック, 履歴に残す文) を返す。

    ★ここが「コスパ」の分かれ目（2026-08-15）
      ・image ブロック … このターンの送信にだけ乗る。history には**入れない**。
      ・履歴に残るのは、下で作る短い1行だけ。
      ・「何が写っていたか」は、アシスタントが返事に書いてくれる。
        ＝要約のための生成を1回も足していない。

    写真まわりで何が起きても、会話は止めない（写真なしとして続ける）。

    ★縮小は重い（to_thread へ逃がす）。
      そのまま回すと、縮めているあいだ**返事が止まる**
      （枚数の上限は置いていないので、なおさら効く）。
    """
    try:
        saved = await asyncio.to_thread(photo_store.save_photos, req.images or [])
    except Exception as exc:      # noqa: BLE001
        print(f"[photo] あずかれませんでした: {type(exc).__name__}: {exc}")
        saved = []

    if not saved:
        return [], [], req.text

    photo_ids = [s["id"] for s in saved]
    blocks = (photo_store.image_blocks(photo_ids)
              if agent_bridge.supports_session_store() else [])

    if not blocks:
        # 見せられない経路（OpenAI互換・置き場が使えない端末）や、読み出しに失敗したとき。
        # 見えたふりをさせないため、見えていないことを本文に書く。
        mark = "（写真を送ったよ。でも、いまのアシスタントには見えていないみたい）"
        text = f"{mark}{req.text}".strip() if req.text.strip() else mark
        return photo_ids, [], text

    # 履歴に残る目印。アシスタントが次のターン以降に読んでも、写真があったことは分かる。
    mark = "（写真を送ったよ）" if len(blocks) == 1 else f"（写真を{len(blocks)}枚送ったよ）"
    text = f"{mark}{req.text}".strip() if req.text.strip() else mark
    return photo_ids, blocks, text


async def _take_files(req: ChatRequest):
    """送られたファイル（PDF・文字のファイル）を document ブロックにして、(ブロック, 履歴に残す目印) を返す（2026-10-01）。

    ★写真と同じ作り：ブロックはこのターンの送信にだけ乗り、履歴（store）には目印の1行だけ。
      大きさ・ページ数の砦は file_blocks.py に（断られる形のまま渡すと、スレッドが数ターン止まるため）。
    何が起きても、会話は止めない（ファイルなしとして続ける）。
    """
    if not req.files:
        return [], ""
    # ★まず PC に置く（2026-10-01）。
    #   置き場は claude/data/files/日付/。あとで極が道具で読み返す（file_tools.py）。
    try:
        await asyncio.to_thread(file_blocks.save_files, req.files)
    except Exception as exc:      # noqa: BLE001 — 置けなくても会話は止めない
        print(f"[file] PC に置けませんでした: {type(exc).__name__}: {exc}")
    try:
        blocks, names, notes = await asyncio.to_thread(file_blocks.make_blocks, req.files)
    except Exception as exc:      # noqa: BLE001
        print(f"[file] あずかれませんでした: {type(exc).__name__}: {exc}")
        blocks, names, notes = [], [], ["送ったファイルを開けなかった"]
    seen = True
    if blocks and not agent_bridge.supports_session_store():
        # 履歴から落とせない端末では渡さない（写真と同じ判断）。見えたふりをさせない。
        blocks, seen = [], False
    return blocks, file_blocks.mark_for(names, notes, seen=seen)


# 投げっぱなしにした処理を掴んでおく置き場。
# ★asyncio は task への参照が誰も無くなると、**途中でも回収してよい**ことになっている
#   （公式の注意書き）。掴んでおかないと、忙しい時間帯に覚え損ねる回が出る。
#   終わったら自分で抜ける（add_done_callback で discard）ので、増え続けることは無い。
_techo_tasks: set = set()


def _techo_spawn(coro) -> None:
    """手帳の処理を、待たずに走らせる（返事の表示を1秒も遅らせないため）。"""
    t = asyncio.create_task(coro)
    _techo_tasks.add(t)
    t.add_done_callback(_techo_tasks.discard)


# ★会話そのままを手帳へ入れる係（_techo_remember）は 2026-09-03 に外した。
#   会話はスレッドに置いたまま、
#   techo_writer が粒を書く（proactive.loop から tick）。★書くのは明け方4時すぎに1日1回（2026-09-04）。


# ── 走っている返事（2026-09-02）────────────────────────────
# iPhone で画面が閉じても、バックグラウンドで返事が来るようにする。
#
# ★何が起きていたか
#   iPhone は画面を閉じると通信を切る。それまでの作りは、切れたら **その場で
#   claude.exe を終わらせていた**＝書きかけの返事がそこで捨てられていた。
#   戻ってきても、返事は無い。
#
# ★これからの形
#   返事づくりを「画面とは別の1本」にして走らせる。画面が居なくなっても走り切り、
#   書き上がったらスレッドへ保存する。戻ってきた画面は、それを読み直すだけ。
#
# ★置き去りを繰り返さないための上限（2026-08-30 のファン事故と同じ轍を踏まない）
#   1本あたり RUN_LIMIT_SEC で打ち切る。そこまでに書けたぶんは残す。
RUN_LIMIT_SEC = 300          # 1本の返事にかける上限（5分）＝道具を持たないふつうの会話
# ★道具を持たせたときは、この5分では足りません（2026-09-07）。
#   ウェブ検索は外のページを何枚も開き、Amazonはお買い物係を待ちます
#   （探すの1回で最長240秒）。
#   足りないと「時間がかかりすぎたので、ここまでにしました」で切れます＝止まって見えます。
RUN_LIMIT_TOOLS_SEC = 900    # 道具を持たせたとき（15分）


def _run_limit(req) -> int:
    """この1本にかけてよい時間。道具を持たせたときだけ広げる。

    ★事実の見張り（2026-09-24・jijitsu_guard）の門が開く回も広げる。止められた回は
      「最初の返事＋判定役（10〜20秒）＋書き直し」で1本が長くなるので、5分だと書き直しが
      「時間がかかりすぎた」で切れるおそれがある。門は0円（スレッドを読むだけ）。
    """
    if req.web_search or req.winpost or req.pokedex or req.amazon:
        return RUN_LIMIT_TOOLS_SEC
    if jijitsu_guard is not None and getattr(req, "guard", True):
        try:
            if jijitsu_guard.opened_by(jijitsu_guard.gate_texts(req.session_id, req.text or ""), req.persona):
                return RUN_LIMIT_TOOLS_SEC
        except Exception:                 # noqa: BLE001 — 門がつまずいても今までどおり5分
            pass
    return RUN_LIMIT_SEC

RUN_KEEP_SEC = 900           # 書き終えた札を置いておく時間（戻ってきた画面が拾えるように）

# session_id -> {"events": [...], "evt": Event, "done": bool, "ended": float}
_runs: dict[str, dict] = {}


def _sweep_runs() -> None:
    """書き終えて時間が経った札を片づける（増えっぱなしにしない）。"""
    now = time.monotonic()
    for sid in [k for k, r in _runs.items()
                if r["done"] and now - r.get("ended", now) > RUN_KEEP_SEC]:
        _runs.pop(sid, None)


def is_running(sid: str) -> bool:
    """そのスレッドの返事を、いま書いている最中か。"""
    r = _runs.get(sid)
    return bool(r and not r["done"])


async def _reply_worker(sid: str, run: dict, replies, persona: str, model: str,
                        user_text: str, limit_sec: int = RUN_LIMIT_SEC,
                        msg_kind: str = "", msg_extra: dict | None = None) -> None:
    """返事を最後まで作って保存する。★画面が居なくなっても、ここは走り切る。

    msg_kind・msg_extra … 引き継ぎメモを書くときだけ（2026-09-17）。保存する発言に印を付ける。
    ★名前を kind にしないこと。下の `async for kind, payload` に上書きされて、印が「result」になる。
    """
    body, thinking = [], []
    reminders = []            # リマインダーの札（2026-09-19）。返事の発言と一緒に保存＝開き直しても押せる

    def push(kind, payload):
        run["events"].append({"kind": kind, "payload": payload})
        run["evt"].set()

    try:
        async with asyncio.timeout(limit_sec):
            async for kind, payload in replies:
                if kind == "text":
                    body.append(payload)
                elif kind == "thinking":
                    thinking.append(payload)
                elif kind == "tool" and isinstance(payload, dict) and payload.get("remind"):
                    reminders.append(payload["remind"])
                push(kind, payload)
    except TimeoutError:
        push("error", "時間がかかりすぎたので、ここまでにしました")
    except asyncio.CancelledError:
        # ★持ち主が ■ を押した（2026-09-17・/chat/stop）。事故ではないので断り文は出さない。
        #   それ以外の取り消し（サーバーを落とすとき）は、そのまま上へ返す。
        if not run.get("stop_requested"):
            raise
        push("stopped", "")
    except Exception as e:                        # noqa: BLE001 会話を黙らせない
        push("error", f"{type(e).__name__}: {e}")
    finally:
        # ★shield で包む：打ち切られている最中は次の await がすぐ止まるので、
        #   裸で書くと片づけ自体が中断されて claude.exe が残る。
        with contextlib.suppress(BaseException):
            await asyncio.shield(replies.aclose())
        stopped = bool(run.get("stop_requested"))
        # 空応答でも黙らせない（空欄は持ち主にとって「壊れた」に見える）
        # ★ただし ■ で止めたときは、書けたところまでだけを残す。1文字も無ければ何も足さない
        #   （止めたのは持ち主の判断。「うまく言葉が出ませんでした」は嘘になる）。
        text = "".join(body).strip()
        # 引き継ぎメモの印は、本文が書けたときだけ付ける。下の断り文に印が付くと、
        # 次の送信で「前のスレからのメモ」として頭に入ってしまう。
        if not text:
            msg_kind, msg_extra = "", None
        if not text and not stopped:
            text = "……（うまく言葉が出ませんでした。もう一度送ってみてください）"
        if not text and reminders:
            # ■ で止めても、出した札は残す（押せるボタンを黙って消さない）
            text = "（リマインダーの札だけ出しました）"
        if text:
            with contextlib.suppress(Exception):
                # ★そのままつなぐ（2026-09-02）。届くのは粒（ひとかけら）なので、
                #   ここで改行を挟むと1文字ごとに改行が入る。塊で来たときの区切りは
                #   agent_bridge のほうで入れてある。
                store.add_message(sid, "assistant", text, persona=persona,
                                  model=model, thinking="".join(thinking),
                                  kind=msg_kind, extra=msg_extra,
                                  reminders=reminders)
        if stopped:
            # ★途中で止めると、CLI のセッションには「やりかけの1ターン」が残る。
            #   そのまま resume すると食い違うので、次のターンはスレッドの流れから送り直させる
            #   （2026-09-07 の手当て）。
            with contextlib.suppress(Exception):
                agent_bridge.forget_session(sid)
        run["done"] = True
        run["ended"] = time.monotonic()
        push("done", "")


async def _drain(run: dict):
    """走っている1本を、画面へ流す。**途中で切れても、走っているほうは止めない。**"""
    i = 0
    while True:
        run["evt"].clear()
        while i < len(run["events"]):
            ev = run["events"][i]
            i += 1
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        if run["done"]:
            return
        try:
            await asyncio.wait_for(run["evt"].wait(), timeout=20)
        except TimeoutError:
            # 長考のあいだも、つながりを保つための空打ち（画面は読み飛ばす）
            yield ": まだ書いています\n\n"


@app.get("/chat/status")
async def chat_status(session_id: str = "default"):
    """そのスレッドの返事を、いま書いている最中か（戻ってきた画面が聞きに来る）。"""
    _sweep_runs()
    return {"session_id": session_id, "running": is_running(session_id)}


class StopRequest(BaseModel):
    session_id: str = "default"


@app.post("/chat/stop")
async def chat_stop(req: StopRequest):
    """■（止める）と ↑（割り込み）の口（2026-09-17）。

    ★止まりきってから返す。返ってすぐ次を送っても「まだ前のお返事を書いています」に当たらないように。
    走っていなければ {"stopped": false} を返すだけ＝押しても壊れない。
    """
    run = _runs.get(req.session_id)
    if not run or run["done"]:
        return {"stopped": False}
    run["stop_requested"] = True
    task = run.get("task")
    if task is not None and not task.done():
        task.cancel()
        # cancel は「お願い」なので、claude.exe の片づけまで終わるのを待つ（最大10秒）
        await asyncio.wait({task}, timeout=10)
    if task is not None and task.done() and not run["done"]:
        # 返事づくりが1行も動かないうちに取り消された（finally を通っていない）。
        # 「書いている最中」のまま残すと、そのスレッドはずっと断られるので、ここで閉じる。
        run["done"] = True
        run["ended"] = time.monotonic()
        run["events"].append({"kind": "done", "payload": ""})
        run["evt"].set()
    return {"stopped": True}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    _sweep_runs()

    # まだ前の返事を書いている最中なら、二重には走らせない。
    # （同じスレッドで2本走らせると、CLI セッションの対応が絡まる）
    if is_running(req.session_id):
        async def busy():
            yield ("data: " + json.dumps(
                {"kind": "error",
                 "payload": "まだ前のお返事を書いています。書き上がるまで待ってください"},
                ensure_ascii=False) + "\n\n")
            yield "data: {\"kind\": \"done\"}\n\n"
        return StreamingResponse(busy(), media_type="text/event-stream")

    # 送った分を先に残す（途中で切れても、持ち主の発言は消えない）
    photo_ids, photo_blocks, user_text = await _take_photos(req)
    # ファイル（PDF など）は写真より前に並べる＝目印も本文の頭に（画面の fileMark と同じ順）
    doc_blocks, file_mark = await _take_files(req)
    if file_mark:
        user_text = f"{file_mark}{user_text}"
    t = store.add_message(req.session_id, "user", user_text,
                          persona=req.persona, model=req.model, photos=photo_ids)
    # このスレで話しかけられた回数（いまの発言を含む）＝「◯往復目」
    turns = sum(1 for m in t.get("messages", []) if m.get("role") == "user")

    # CLI セッションが切れているときだけ、これまでの流れを添えて送りなおす
    seed = req.seed or (agent_bridge.needs_seed(req.session_id, req.account)
                        and store.history_text(req.session_id) or "")
    replies = agent_bridge.stream_reply(
        req.session_id, user_text,
        persona=req.persona, account=req.account, model=req.model,
        effort=req.effort, web_search=req.web_search, seed_text=seed,
        photo_blocks=[*doc_blocks, *photo_blocks], turns=turns,
        winpost=req.winpost, pokedex=req.pokedex,
        amazon=req.amazon, guard=req.guard, rougo=req.rougo, toushin=req.toushin,
    )

    run = {"events": [], "evt": asyncio.Event(), "done": False, "ended": 0.0}
    _runs[req.session_id] = run
    # ★画面とは別の1本にする。ここが「閉じても返事が来る」の芯。
    run["task"] = asyncio.create_task(
        _reply_worker(req.session_id, run, replies, req.persona, req.model, user_text,
                      limit_sec=_run_limit(req)))

    return StreamingResponse(_drain(run), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/photos")
async def list_photos():
    """クロード極に送った写真の置き場を、新しい日から順に返す（2026-08-19）。

    ★返すのは名札と日付だけ。写真そのものは `/photo/{id}` から1枚ずつ。
    ★ここは**持ち主が見返すためだけ**の口。アシスタントには1枚も渡さない
      （アシスタントが写真を見るのは、送られたそのターンだけ）。
    """
    return photo_store.list_days()


@app.get("/photo/{photo_id}")
async def get_photo(photo_id: str, thumb: int = 0):
    """持ち主が送った写真を、画面に返す（2026-08-15）。

    ★アシスタントに渡す道とは無関係。ここは「持ち主が自分で見返すため」だけの口。
      アシスタントが写真を見るのは、送られたそのターンの1回きり（photo_store.py 参照）。
    ★名札は英数字と決まった形しか通さない（photo_store.valid_photo_id）＝
      `..` などでフォルダの外へ出られない（config.valid_date と同じ守り）。
    """
    data = photo_store.photo_bytes(photo_id, thumb=bool(thumb))
    if data is None:
        raise HTTPException(status_code=404, detail="その写真は見つかりません")
    return Response(
        content=data,
        media_type="image/jpeg",
        # 写真は一度保存したら中身が変わらないので、端末に持っていてもらう
        # （スレを開き直すたびに取りに来ると、そのぶん遅くて重い）。
        headers={"Cache-Control": "private, max-age=604800"},
    )


# ── スレッド ────────────────────────────────────────────
class MetaRequest(BaseModel):
    title: str | None = None
    heart: bool | None = None
    star: bool | None = None
    folder: str | None = None
    archived: bool | None = None


@app.get("/sessions")
async def sessions(request: Request):
    # 2026-08-20：縮めて送る（一覧は本文を載せないが、数が増えると効いてくる）
    #
    # ★中身が前と同じなら「変わっていません」だけ返す（2026-09-02）。
    #   Cache-Control は no-cache＝**毎回サーバーに確かめる**（古いものを掴まない）。
    #   変わっていなければ 304 だけ返す＝本文は1バイトも流れない。
    #   ※ no-store は使わない（重くなるため）。
    data = {"sessions": store.listing()}
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    etag = 'W/"' + hashlib.md5(body).hexdigest() + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304,
                        headers={"ETag": etag, "Cache-Control": "no-cache"})
    resp = _json_gz(data, request)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/sessions")
async def new_session(persona: str = "1", model: str = ""):
    return store.create(persona, model)


@app.get("/sessions/{tid}")
async def get_session(tid: str, request: Request):
    # 2026-08-20：縮めて送る。長いスレは 161KB あり、開くたびに落としていた（→55KB）
    t = store.load(tid)
    return _json_gz(t or {"id": tid, "messages": [], "title": ""}, request)


@app.patch("/sessions/{tid}")
async def patch_session(tid: str, req: MetaRequest):
    t = store.update_meta(tid, **req.model_dump())
    return t or {"ok": False}


@app.delete("/sessions/{tid}")
async def delete_session(tid: str):
    agent_bridge.forget_session(tid)
    return {"ok": store.delete(tid)}


class HikitsugiRequest(BaseModel):
    """引き継ぎメモを書かせるときの、いまの画面の値（送信と同じもの）。"""
    account: str = "sdk1"
    model: str = ""
    effort: str | None = None
    web_search: bool = False
    winpost: bool = False
    pokedex: bool = False
    amazon: bool = False
    rougo: bool = False
    toushin: bool = False


@app.post("/sessions/{tid}/hikitsugi")
async def hikitsugi(tid: str, req: HikitsugiRequest):
    """スレ引き継ぎ（2026-09-17）。

    1. 「〇〇（続き）」の新しいスレを作る（プロジェクトは元のスレと同じ）
    2. 元のスレの相手に、引き継ぎメモを書かせる（agent_bridge.stream_hikitsugi）
    3. 書けたメモは、新しいスレの最初の発言として残る（印は kind="hikitsugi"）
    ★新しいスレの番号は、書き終わりを待たずにすぐ返す。書いているあいだは、ふだんの返事と同じく
      /chat/status が「まだ書いています」と答える＝画面を閉じても書き切り、■でも止められる。
    """
    _sweep_runs()
    src = store.load(tid)
    if not src or not src.get("messages"):
        return {"ok": False, "reason": "引き継ぐ話が、まだありません"}
    if is_running(tid):
        return {"ok": False, "reason": "このスレはまだお返事を書いています。書き終わってから引き継いでください"}

    persona = src.get("persona") or "1"
    # 書くのは、画面のピッカーで選んでいるモデルと深さ＝ふだんの送信と同じ（2026-09-19）。
    # ★前は元のスレのモデルを先に採っていたので、ピッカーを Opus 4.6 にしても Fable 5.1 のスレからは
    #   Fable 5.1 が書いていた（モデルが意図と違っていた）。Fable の枠が切れた晩に、
    #   モデルを替えて引き継げなかった。元のスレと同じモデルを選んでいれば、キャッシュはいままでどおり効く。
    model = req.model
    effort = req.effort
    src_title = src.get("title") or ""

    new = store.create(persona, model, title=store.continued_title(src_title))
    full = ("" if not agent_bridge.needs_seed(tid, req.account)
            else store.history_text(tid, limit=None, max_chars=agent_bridge.HIKITSUGI_MAX_CHARS))
    replies = agent_bridge.stream_hikitsugi(
        tid, full, persona=persona, account=req.account, model=model, effort=effort,
        web_search=req.web_search, winpost=req.winpost, pokedex=req.pokedex, amazon=req.amazon,
        rougo=req.rougo, toushin=req.toushin,
    )
    run = {"events": [], "evt": asyncio.Event(), "done": False, "ended": 0.0}
    _runs[new["id"]] = run
    run["task"] = asyncio.create_task(
        _reply_worker(new["id"], run, replies, persona, model, "",
                      limit_sec=RUN_LIMIT_TOOLS_SEC,     # 長いスレを読んで長く書くので、道具ありと同じ15分
                      msg_kind=store.HIKITSUGI,
                      msg_extra={"from": tid, "from_title": src_title}))
    return {"ok": True, "id": new["id"], "title": new["title"],
            "persona": persona, "model": model, "from_title": src_title}


@app.post("/session/reset")
async def session_reset(session_id: str = "default"):
    agent_bridge.forget_session(session_id)
    return {"ok": True}


# ── シスプロ（スマホから読み書きする） ──────────────────────
class SysPromptRequest(BaseModel):
    persona: str = "1"
    content: str = ""


@app.get("/system-prompt")
async def get_system_prompt(persona: str = "1"):
    """スマホの編集欄に出す中身。返すのは「外側」だけ（表に出ないシスプロは見せも触らせもしない）。"""
    try:
        content = config.sys_file(persona).read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    return {"persona": persona, "content": content}


@app.post("/system-prompt")
async def update_system_prompt(req: SysPromptRequest):
    config.sys_file(req.persona).write_text(req.content.strip() + "\n", encoding="utf-8")
    return {"ok": True, "persona": req.persona, "length": len(req.content.strip())}


@app.get("/system-prompt/preview")
async def preview_system_prompt(persona: str = "1"):
    """実際に効いている全文（日付＋内側＋外側）。確かめる用。"""
    text = agent_bridge.build_system_prompt(persona)
    return {"persona": persona, "text": text, "length": len(text)}


# ── メモリ（プロジェクトごとの袋。持ち主が直せる・消せる） ─────────────
class MemoryRequest(BaseModel):
    text: str


@app.get("/memories")
async def get_memories(persona: str = "1"):
    items = memory.listing(persona)
    # たたんだ記憶の頭（シスプロへ積むのと同じ切り方）と、毎回積む件数の上限を画面へ渡す（2026-10-14）
    for m in items:
        m["head"] = memory.head(m.get("text", ""))
    cap = memory.MAX_INJECT_SHARED if persona == memory.SHARED else memory.MAX_INJECT
    return {"persona": persona, "memories": items, "count": len(items), "max": cap}


@app.post("/memories")
async def add_memory(req: MemoryRequest, persona: str = "1"):
    m = memory.add(persona, req.text, source="user")
    return m or {"ok": False, "reason": "空か、もう同じものがあります"}


@app.patch("/memories/{mid}")
async def edit_memory(mid: str, req: MemoryRequest, persona: str = "1"):
    return memory.update(persona, mid, req.text) or {"ok": False}


@app.delete("/memories/{mid}")
async def remove_memory(mid: str, persona: str = "1"):
    return {"ok": memory.delete(persona, mid)}


# ── たたむ（2026-10-14）────────────────────────────────────
# たたんだ記憶は、シスプロへ頭だけ積む（memory.py の「たたむ」）。中身は変わらない。
class FoldRequest(BaseModel):
    fold: bool


@app.patch("/memories/{mid}/fold")
async def fold_memory(mid: str, req: FoldRequest, persona: str = "1"):
    return memory.set_fold(persona, mid, req.fold) or {"ok": False}


# ── ゴミ箱（2026-08-28）──────────────────────────────────
# 忘れた記憶は消えずにここへ入る。シスプロにも一覧にも積まれないが、戻せる。
# ★アシスタント自身が忘れられるようにした日に足した。AI が誤って捨てても実害を出さないため。
@app.get("/memories/trash")
async def get_trash(persona: str = "1"):
    items = memory.trash_listing(persona)
    return {"persona": persona, "memories": items, "count": len(items)}


@app.post("/memories/trash/{mid}/restore")
async def restore_memory(mid: str, persona: str = "1"):
    return memory.restore(persona, mid) or {"ok": False, "reason": "ゴミ箱にありません"}


# ── 手帳（話したことを勝手に覚えていく。いまの「おぼえていること」とは別の袋） ─────
# ★中身は techo.py（原本＋索引の出し入れ）・techo_vec.py（ベクトルの土台）。
#   `techo` が None（部品やモデルが無い環境）のときは、空の形をそのまま返すだけにする。
@app.get("/techo")
async def get_techo(persona: str = "1", limit: int = 50, q: str = ""):
    """手帳を見る。`q` を渡すと探す（意味の近さ＋文字の一致の両方で）。
    並ぶのは粒（話題ごとの段落）だけ（2026-09-03・会話そのままは入れない）。"""
    if techo is None:
        return {"persona": persona, "items": [], "stats": {"enabled": False}}
    q = (q or "").strip()
    if q:
        items = await techo.search(persona, q, limit=limit)
        stats = await anyio.to_thread.run_sync(techo.stats, persona)
        return {"persona": persona, "items": items, "stats": stats, "q": q}
    # ファイルを読む仕事は別のスレッドへ逃がす（画面の配信を待たせないため）。
    # panel() は原本を1回だけ読んで、一覧と件数の両方を返す。
    d = await anyio.to_thread.run_sync(techo.panel, persona, limit)
    return {"persona": persona, **d}


class TechoEdit(BaseModel):
    text: str


@app.patch("/techo/{tid}")
async def edit_techo(tid: str, req: TechoEdit, persona: str = "1"):
    """1件を書き直す。索引も同じ id で入れ直すので、思い出す中身も新しくなる。"""
    if techo is None:
        return {"ok": False}
    m = await techo.update(persona, tid, req.text)
    return m or {"ok": False, "reason": "見つかりませんでした"}


@app.delete("/techo/{tid}")
async def forget_techo(tid: str, persona: str = "1"):
    if techo is None:
        return {"ok": False}
    return {"ok": await techo.forget(persona, tid)}


@app.post("/techo/rebuild")
async def rebuild_techo(persona: str = "1"):
    """原本から索引を作り直す（生成AIを1回も呼ばない＝0円）。"""
    if techo is None:
        return {"count": 0}
    return {"count": await techo.rebuild(persona)}


@app.post("/techo/redigest")
async def redigest_techo(persona: str = "", wipe: int = 0):
    """手帳を、これまでの会話（data/threads）から書き直す（2026-09-03）。裏で走らせて、すぐ返す。

    wipe=1 … その袋の手帳を空にしてから書く（写しは先に取ること）。persona 空＝記憶を使う袋ぜんぶ。
    ★persona を付けずに wipe=1 を押すと、**記憶を使う袋が全部**空になる。1つの袋だけなら必ず persona を付ける。
    中で Sonnet 5 を呼ぶ（サブスク枠・2026-09-12 に Haiku から上げた）。進み具合は GET /techo/redigest。道具は claude/techo_import.py。
    """
    if techo is None or techo_writer is None:
        return {"ok": False, "reason": "このパソコンでは手帳を書けません"}
    if techo_writer.STATUS.get("running"):
        return {"ok": False, "reason": "いま書いている途中です", **techo_writer.STATUS}
    _techo_spawn(techo_writer.backfill(pid=persona or None, wipe=bool(wipe)))
    return {"ok": True, "started": True, "wipe": bool(wipe), "persona": persona or "すべて"}


@app.get("/techo/redigest")
async def redigest_status():
    """書き直しの進み具合（running／done／total／grains／note）。"""
    if techo_writer is None:
        return {"running": False, "note": "このパソコンでは手帳を書けません"}
    return dict(techo_writer.STATUS)


# ── プロジェクト（人格ごとの箱。画面から作る・直す・消す） ─────────────
class ProjectRequest(BaseModel):
    name: str | None = None
    core: bool | None = None        # 表に出ないシスプロを混ぜるか（既定の3つ以外では基本 false）
    memory: bool | None = None      # おぼえていることを繋ぐか
    order: int | None = None


@app.get("/projects")
async def get_projects():
    items = projects.listing()
    return {
        "projects": items,
        "locked": [d["id"] for d in config.DEFAULT_PROJECTS],   # 画面から消せないもの
        # みんな共通の袋（2026-09-02）。★プロジェクトの一覧には**入れない**。
        #   入れると会話の相手として選べてしまう。これは袋であって人格ではない。
        #   画面は、この名札を見て「みんな共通」の側を出す。
        "shared": {"id": memory.SHARED, "name": memory.SHARED_NAME},
    }


@app.post("/projects")
async def create_project(req: ProjectRequest):
    return projects.create(
        name=req.name or "",
        core=bool(req.core),
        memory=True if req.memory is None else bool(req.memory),
    )


@app.patch("/projects/{pid}")
async def patch_project(pid: str, req: ProjectRequest):
    return projects.update(pid, **req.model_dump())


@app.delete("/projects/{pid}")
async def remove_project(pid: str):
    ok = projects.delete(pid)
    return {"ok": ok, "reason": "" if ok else "はじめからある3つは消せません"}


# ── 声かけ（AI から話しかける＝5時間枠の始動） ─────────────────
class ProactiveConfig(BaseModel):
    enabled: bool | None = None
    hour: int | None = None
    minute: int | None = None


@app.get("/proactive")
async def get_proactive():
    cfg = proactive.load()
    return {**cfg, "notify_ready": bool(getattr(config, "NTFY_TOPIC", ""))}


@app.post("/proactive")
async def set_proactive(cfg: ProactiveConfig):
    return proactive.save(cfg.model_dump())


@app.post("/proactive/test")
async def test_proactive():
    """いま1回だけ声をかけさせる（持ち主が画面から試す用）。"""
    text = await proactive.send_now()
    return {"ok": bool(text), "text": text}


# ── 画面が使う一覧 ───────────────────────────────────────
@app.get("/models")
async def models():
    return {
        "models": [{"value": v, "label": l} for v, l in config.MODELS],
        "effort_by_model": config.EFFORT_BY_MODEL,
        # 起動時の写しではなく、いま .env にある鍵で判断する（config.sdk_tokens 参照）
        "sdk2_available": bool(config.sdk_tokens()[1]),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
