# -*- coding: utf-8 -*-
"""クロード極 — 設定と置き場所

認証まわりの考え方は次のとおり。
  - ANTHROPIC_API_KEY は起動時に環境から外す（外さないと CLI が従量課金の鍵に落ちる）
  - SDK1 … その端末の Claude Code のログイン（ambient）をそのまま使う。鍵は要らない
  - SDK2 … 別アカウントの OAuth トークン。サブプロセスにだけ注入する
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"          # スマホから書き換わるもの（git には載せない）
DATA_DIR.mkdir(exist_ok=True)

# .env を読む。
# 素の load_dotenv() は起動時の作業フォルダの .env を探す。
# 極は claude/ から起動するので、明示のパスで指す（repo 直下を指すと存在せず、
# dotenv は黙って何もしない＝SDK2 が使えなくなる。2026-08-01 に実際に踏んだ）。
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

# ── 認証 ───────────────────────────────────────────────
# 従量課金の鍵が環境にあると CLI がそちらに落ちる。起動時に外して、
# サブスクのログイン（5時間枠）を使わせる。
ANTHROPIC_API_KEY = os.environ.pop("ANTHROPIC_API_KEY", None)

# Claude Code の auto-memory（~/.claude/projects/<cwd>/memory/MEMORY.md）を止める。
# これは setting_sources=[] の管轄外で、空リストにしてもセッション開始時に
# システムプロンプトへ丸ごと読み込まれる。
# 2026-08-01 に実測：入れる前は入力960トークンあり、極が MEMORY.md の中身
# （開発の申し送り）をそのまま引用できてしまった。極は実務用なので、
# 開発クロードの記憶が混ざるのは害でしかない。setdefault なので外から明示すれば従う。
os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")

# Claude Code が会話の途中に差し込む英語の「# Environment」（作業場所・git・Windows・PowerShell/Bash・
# OS・モデル名）を止める（2026-09-13）。ログインには関係しないので、ここで全体に入れる。
os.environ.setdefault("CLAUDE_CODE_CARVED_SLATE", "0")

# 極の CLI の設定の場所（2026-09-13）。~/.claude.json のアカウント情報から
# メールアドレスが会話の頭に毎回添えられるのを止める。CLAUDE_CONFIG_DIR は公式の設定。
# ★全体には入れない ── SDK1 の鍵が空のとき、極はこの端末の Claude Code のログイン（~/.claude）に戻るので、
#   ここを変えるとそのログインが外れる。鍵を CLI に渡すところ（agent_bridge._options・techo_writer._ask_writer）で、
#   鍵があるときだけ使う。
CLI_HOME = DATA_DIR / "cli_home"
CLI_HOME.mkdir(parents=True, exist_ok=True)

# SDK2＝別アカウントのトークン。空なら SDK2 は使えない（画面に出さない）。
# .env の同じ名前を読む。
# ※起動時の写し。いま効いている値が要るところでは下の sdk_tokens() を使う。
SDK2_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN_2", "").strip()


def sdk_tokens() -> tuple[str, str]:
    """いま .env にある鍵を、その都度読んで返す（sdk1, sdk2）。

    ★起動時の写しを使ってはいけない（2026-08-13 に実際に踏んだ）。
      極は 8/2 14:09 に起動したまま動き続け、8/10 19:18 に入れ替えた SDK1 の鍵が
      11日間届かず、SDK1・SDK2 とも 401 になった。極は誰も再起動しない＝写しでは腐る。
      毎回読み直せば、鍵を入れ替えても極の再起動は要らない。

    dotenv_values は os.environ を汚さない＝この python 自身の認証環境は触らない。
    .env が読めない・行が無いときは起動時の値へ落とす（黙って空にしない）。
    """
    try:
        vals = dotenv_values(ENV_FILE)
    except OSError:
        vals = {}
    t1 = (vals.get("CLAUDE_CODE_OAUTH_TOKEN")
          or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "") or "").strip()
    t2 = (vals.get("CLAUDE_CODE_OAUTH_TOKEN_2") or SDK2_OAUTH_TOKEN or "").strip()
    return t1, t2

PORT = int(os.environ.get("KIWAMI_PORT", "8030"))

# 声かけの通知先（ntfy.sh のトピック名）。**空なら通知しない**＝声かけ自体は動く。
# 他の通知経路とは分けてある。
NTFY_TOPIC = os.environ.get("KIWAMI_NTFY_TOPIC", "").strip()

# ── シスプロ ────────────────────────────────────────────
# 1は2層。内側（core）はコード同梱でスマホからは触れない。外側（user）はスマホから書く。
SYS_CORE_FILE  = BASE_DIR / "system_prompt_core.txt"
SYS_USER_FILE  = DATA_DIR / "system_prompt_user.txt"
SYS_2_FILE     = DATA_DIR / "system_prompt_2.txt"

# ── プロジェクト（2026-08-02 追加）──────────────────────────
# 公式アプリのプロジェクトと同じ考え方で、シスプロとメモリを人格ごとに分ける。
# もとは persona="1"/"2" の2つ固定だった。**その文字列をそのまま ID として引き継ぐ**ので、
# いままでのスレッド（store の persona 項目）は1本も引っ越さない。
PROJ_DIR = DATA_DIR / "projects"
PROJ_DIR.mkdir(exist_ok=True)

# 置き場所が data/ ではないものを、ここで名指しする。
#   1・2 … もとのファイル名のまま（引っ越しをさせない）
#   3   … ★表に出ないシスプロ（system_prompt_core.txt）と同じく **コード側**（claude/ 直下）に置く。
#         data/ は .gitignore で丸ごと外れているので、そこに置くと別のパソコンへ運べない。
#         claude/ 直下なら `!claude/*.txt` で GitHub に載る（2026-08-02）。
#         ※表に出ないシスプロと違って画面からは編集できる。直したら git に差分が出るのでコミットすること。
LEGACY_SYS_FILES = {
    "1": SYS_USER_FILE,
    "2": SYS_2_FILE,
    "3": DATA_DIR / "system_prompt_3.txt",
}


def sys_file(pid: str):
    """そのプロジェクトの「外側」シスプロの置き場所。"""
    return LEGACY_SYS_FILES.get(str(pid)) or DATA_DIR / f"system_prompt_{pid}.txt"


# 出荷時にある3つ。無ければ起動時に作られる。**この3つは画面から消せない。**
#   core   … 表に出ないシスプロ（system_prompt_core.txt）を混ぜるか
#   memory … おぼえていることを繋ぐか（袋はプロジェクトごとに分かれている）
DEFAULT_PROJECTS = [
    {"id": "1", "name": "実務", "core": True,  "memory": True,  "order": 1},
    {"id": "2", "name": "クロード",   "core": False, "memory": False, "order": 2},
    {"id": "3", "name": "相棒",      "core": False, "memory": True,  "order": 3},
]

# 記録（taicho.py）の道具3本を持たせるプロジェクト（2026-09-19）。
# ★いまは "3" だけ。ほかのプロジェクトには説明文のぶんも1トークンも載らない
#   （実務は 265〜310 トークンで話せている。そこを崩さないため）。
TAICHO_PROJECTS = frozenset({"3"})

# ── モデル ─────────────────────────────────────────────
# 極は「最新＋Opus の 4 世代」。4.5〜4.8 は 2026-08-13 に追加。
# Fable 5.1 は 2026-09-02 に追加（IDは日付なしの claude-fable-5-1）。
MODELS = [
    ("",                            "おまかせ（既定）"),
    ("claude-opus-5-5",             "Opus 5.5"),
    ("claude-opus-5",               "Opus 5"),
    ("claude-fable-5-1",            "Fable 5.1"),
    ("claude-fable-5",              "Fable 5"),
    ("claude-sonnet-5",             "Sonnet 5"),
    ("claude-opus-4-8",             "Opus 4.8"),
    ("claude-opus-4-7",             "Opus 4.7"),
    ("claude-opus-4-6",             "Opus 4.6"),
    ("claude-opus-4-5",             "Opus 4.5"),
    ("claude-haiku-4-5-20251001",   "Haiku 4.5"),
]

# 思考の深さを選べるモデルだけ（画面の EFFORT_BY_MODEL と同じ中身）
# 4.8/4.7 は adaptive 世代なので「いままで通り(keep)」を出さない。
EFFORT_BY_MODEL = {
    "claude-fable-5-1": {"levels": ["low", "medium", "high", "xhigh", "max"], "def": "xhigh"},
    "claude-fable-5":   {"levels": ["low", "medium", "high", "xhigh", "max"], "def": "xhigh"},
    "claude-opus-5-5":  {"levels": ["low", "medium", "high", "xhigh", "max"], "def": "xhigh"},
    "claude-opus-5":    {"levels": ["low", "medium", "high", "xhigh", "max"], "def": "xhigh"},
    "claude-opus-4-8":  {"levels": ["off", "low", "medium", "high", "xhigh", "max"], "def": "xhigh"},
    "claude-opus-4-7":  {"levels": ["off", "low", "medium", "high", "xhigh", "max"], "def": "xhigh"},
    "claude-opus-4-6":  {"levels": ["off", "keep", "low", "medium", "high", "xhigh", "max"], "def": "max"},
    # ★Opus 4.5 は触らない（2026-09-20）。
    #   effort が効かない世代で、深さは下の THINKING_BUDGETS（budget_tokens）で表している。
    "claude-opus-4-5":  {"levels": ["off", "keep", "max"],   "def": "max"},
}

# 4.5 は effort max が効かない世代なので budget_tokens に訳す。
# 「最大」は CLI で出せるいちばん大きい 63999（2026-09-10。前の 32000 は CLI が 31999 に切り詰めて
# 「おまかせ」と同じ深さだった）。出力の上限は agent_bridge._options が 64000 に上げる。
# 4.6 は adaptive＋effort で送る（agent_bridge.ADAPTIVE_BY_CLI）ので、ここは使わない。
THINKING_BUDGETS = {"off": 0, "keep": None, "max": 63999}


# ── 新しいモデルに要る、クロードコードの版（2026-09-23・Opus 5.5）──
# Opus 5.5 を頼むと、CLI が古いと API が断る：
#   「Claude Code 2.1.278 does not support this model; version 2.1.280 or newer is required」
# claude_agent_sdk が同梱している CLI は 2.1.258（部品の最新 0.2.156 でも 2.1.276）で届かない。
# そこで **このモデルのときだけ** 別に入っている新しい claude.exe を使う（ClaudeAgentOptions.cli_path）。
# ほかのモデルは今までどおり同梱の CLI ＝ 送る中身も動きも1文字も変わらない。
# ★同梱の CLI が 2.1.280 以上になったら、この表を空にするだけで元どおりになります。
MODEL_MIN_CLI: dict[str, tuple[int, int, int]] = {
    "claude-opus-5-5": (2, 1, 280),
}

_path_cli_cache: list = []      # 新しいクロードコードは1回だけ調べて覚える（[(場所, 版)]）


def _path_cli_info() -> tuple:
    """同梱とは別に入っているクロードコードの (場所, 版) を返す。無ければ (None, None)。

    探す順は ①`claude install` の既定の置き場（~/.local/bin/claude.exe）→ ②PATH。
    ★見るのは **.exe だけ**。npm で入れた claude は claude.cmd（node を呼ぶ中継ファイル）で、
      SDK 自身がそれを断る作りになっている。
    ★入れ直す1行：`claude install latest`（お金はかかりません。今ある npm 版はそのまま残ります）"""
    if _path_cli_cache:
        return _path_cli_cache[0]
    info = (None, None)
    cands = [str(Path.home() / ".local" / "bin" / "claude.exe")]
    which = shutil.which("claude.exe")
    if which:
        cands.append(which)
    for exe in cands:
        if not os.path.isfile(exe):
            continue
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout or "")
            if m:
                info = (exe, tuple(int(x) for x in m.groups()))
                break
        except Exception:
            pass
    _path_cli_cache.append(info)
    return info


def agent_cli_path(model: str):
    """そのモデルに、同梱より新しいクロードコードが要るなら、その場所を返す。
    要らない・見つからない・版が足りないときは None（＝SDK が今までどおり同梱を使う）。"""
    need = MODEL_MIN_CLI.get(model or "")
    if not need:
        return None
    exe, ver = _path_cli_info()
    if exe and ver and ver >= need:
        return exe
    print(f"[cli] {model} には Claude Code {'.'.join(map(str, need))} 以上が要りますが、"
          f"手元にあるのは {'.'.join(map(str, ver)) if ver else '見つかりません'}。"
          f"同梱のCLIのまま進みます（この経路では断られます）")
    return None


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_date(s) -> bool:
    """'YYYY-MM-DD' の形か（ファイル名にして安全か）。"""
    return isinstance(s, str) and bool(_DATE_RE.match(s))

# ── 画面の版（2026-08-20）──────────────────────────────
# `/` を配るときに index.html の `__UI_VERSION__` をこの値へ置き換える。
# 端末に残っている古い写しは古い版を持ったままになるので、応答ヘッダの
# `X-UI-Version` と食い違ったときだけ、画面が自分で1回読み直す。
# ＝ **写しを捨てずに**「直した画面が届く」を成り立たせる仕組み。
_UI_FILE = Path(__file__).resolve().parent / "index.html"


def ui_version() -> str:
    """index.html の版。中身が変われば必ず変わる。読めなければ空文字。"""
    try:
        st = _UI_FILE.stat()
        return f"{int(st.st_mtime)}-{st.st_size}"
    except Exception:
        return ""


# ── スマホマークから開く、ほかの部屋（2026-09-06）──────────────────
# ★図鑑は大きいので git には載せません。このフォルダの zukan/ に置いたものを、
#   このサーバーがそのまま配ります（置き場所を変えたいときは KIWAMI_ZUKAN_DIR）。
# ★入っていないパソコンでは、メニューにその行が出ません（手帳と同じ作法）。
ZUKAN_DIR = Path(os.environ.get("KIWAMI_ZUKAN_DIR") or (BASE_DIR / "zukan"))

# 別画面（もともと別のポートに立てる作り）。ここではこのサーバーの中に相乗りさせるので、
# 予約タスクも常駐も増えません。大きいデータは、初めて開かれたときに1回だけ読みます。
WINPOST_DIR = BASE_DIR / "winpost"


def zukan_ok() -> bool:
    """図鑑が、このパソコンに入っているか。"""
    return (ZUKAN_DIR / "data" / "dex.js").exists()


def winpost_ok() -> bool:
    """別画面を配れるか。"""
    return (WINPOST_DIR / "index.html").exists()
