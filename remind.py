# -*- coding: utf-8 -*-
"""クロード極 — リマインダーに登録する道具（全プロジェクト・2026-09-19 新設）

経緯：公式 Claude アプリでは iPhone のリマインダーに登録できるので、同じことを極でもできるようにしたい、という要望から作った。

★登録先は極の一覧（reminder_app.py・画面は /reminders）。
★iPhone のリマインダーへは、札の「iPhoneのリマインダーに入れる」ボタンから入れる。押すと公式 Claude アプリが
  頼む文入りで開き（claude.ai/new?q=…）、持ち主が送ると公式 Claude が iPhone のリマインダーに入れる。
  （iPhone のリマインダーに書けるのは iPhone の中のアプリだけ。ショートカットを作る形・ディスコードで知らせる形は採らなかった）
★会話の中には札が残る（返事と一緒に保存・開き直しても出る）。
"""
from datetime import datetime

from claude_agent_sdk import tool

import projects
import reminder_app

TOUROKU = "mcp__kiwami_memory__remind_touroku"
TOOL_NAMES = [TOUROKU]


def normalize(args: dict, now: datetime | None = None) -> dict | str:
    """道具の引数を札の形にする（画面の札用。登録そのものは reminder_app.add）。読めなければ断り文。"""
    args = args or {}
    title = str(args.get("youken") or "").strip()
    if not title:
        return "用件が空なので、登録していません。"
    p = reminder_app.parse_due(str(args.get("nichiji") or ""), now)
    if isinstance(p, str):
        return p
    due, morning = p
    return {"title": title, "due": due, "notes": str(args.get("memo") or "").strip(),
            "label": reminder_app.label(due), "morning": morning}


def event_text(r: dict) -> str:
    return r["title"] + (f"（{r['label']}）" if r["label"] else "（日時なし）")


def result_text(row: dict | str) -> str:
    """AI に返す文。"""
    if isinstance(row, str):
        return row
    when = f"（{row['label']}）" if row["label"] else "（日時なし）"
    lines = [f"リマインダーの一覧に登録しました：{row['title']}{when}",
             "★まだ iPhone のリマインダーには入っていません。会話の中の札の「iPhoneのリマインダーに入れる」を"
             "持ち主が押すと、公式 Claude アプリが開き、送ると iPhone のリマインダーに入ります。"
             "返事ではそのことを短く伝え、「iPhoneに登録しました」とは言わないこと。"]
    if row.get("morning"):
        lines.append("時刻の指定が無かったので、朝9時に知らせる形にしました。そのことも持ち主に一言伝えること。")
    lines.append("一覧は、クロード極のメニュー（スマホマーク）の「リマインダー」で見られます（各行にも同じボタンがあります）。")
    return "\n".join(lines)


_DESC = (
    "持ち主のリマインダーに1件登録する。会話の中に「iPhoneのリマインダーに入れる」ボタンつきの札が出る。"
    "持ち主が「リマインドして」「リマインダーに入れて」「◯時に教えて」「忘れないようにして」と言ったときに使う。"
    "youken … 用件（リマインダーの題名になる。短く具体的に）。"
    "nichiji … 知らせる日時（YYYY/MM/DD HH:MM）。日付だけなら YYYY/MM/DD（朝9時に知らせる）。決まっていなければ空。"
    "「明日」「3時間後」などは、持ち主の発言の頭の［いま …］の日時から数えて入れる。"
    "memo … 添えておきたいこと（無ければ空）。"
    "用件が2つあれば、2回呼ぶ（1回に1件）。"
)


def tools(pid: str) -> list:
    """kiwami_memory のサーバーに足す1本。登録した人の名前は、そのプロジェクトの名前。"""

    @tool("remind_touroku", _DESC, {"youken": str, "nichiji": str, "memo": str})
    async def _touroku(args):
        row = reminder_app.add(args.get("youken", ""), args.get("nichiji", ""), args.get("memo", ""),
                               who=projects.name_of(pid))
        return {"content": [{"type": "text", "text": result_text(row)}]}

    return [_touroku]
