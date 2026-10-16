"""みんかぶ投資信託のページの HTML を読む係（確かめ用）。

持ち主がブラウザで開いたページを、ブックマークの「取り込む」（torikomi.js）が写して渡します。
ここは、その写しが合っているかを確かめ役（tools/verify/verify_toushin.py）が答え合わせするための読み方です。

  ランキング  /ranking/return?term=<月数>&page=<n>  … 1ページ20件。並び順＝その期間の順位。表の数字は1年のリターン
  個別ファンド /fund/<ID>/return                    … 3ヶ月・6ヶ月・1年・3年・5年のリターンと、全ファンド中の順位
"""
from __future__ import annotations

import html as _html
import re

BASE = "https://itf.minkabu.jp"
TERMS = (3, 6, 12, 36)  # 画面に出す期間（月数）。36＝3年（年率）。みんかぶに1ヶ月と24ヶ月は無い


def ranking_url(term: int, page: int) -> str:
    return f"{BASE}/ranking/return?term={term}&page={page}"


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _num(s: str) -> float | None:
    m = re.search(r"[-+−]?\d[\d,]*\.?\d*", s)
    if not m:
        return None
    return float(m.group(0).replace(",", "").replace("−", "-"))


def parse_ranking(html: str) -> list[dict]:
    """ランキング1ページぶん。[{rank, id, name, company, nav, nav_diff, ret12, fee, rating}]"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r'id="fund_ids" value="([0-9A-Za-z]{8})"', tr)
        rank = re.search(r'<span class="text-lg">\s*(\d+)\s*</span>', tr)
        if not m or not rank:
            continue
        fid = m.group(1)
        name = re.search(r'href="/fund/%s"[^>]*>(.*?)</a>' % fid, tr, re.S) \
            or re.search(r'<a[^>]*href="/fund/%s">(.*?)</a>' % fid, tr, re.S)
        company = re.search(r'<div class="text-itf-xs text-slate-500">(.*?)</div>', tr, re.S)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 5:
            continue
        ps = re.findall(r"<p([^>]*)>(.*?)</p>", tds[1], re.S)
        nav = _num(_text(ps[0][1])) if ps else None
        diff = None
        if len(ps) > 1:
            diff = _num(_text(ps[1][1]))
            if diff is not None and "negative" in ps[1][0]:
                diff = -abs(diff)
        rows.append({
            "rank": int(rank.group(1)),
            "id": fid,
            "name": _text(name.group(1)) if name else "",
            "company": re.sub(r"\s+", "", _text(company.group(1))) if company else "",
            "nav": nav,
            "nav_diff": diff,
            "ret12": _num(_text(tds[2])),
            "fee": _num(_text(tds[3])),
            "rating": tds[4].count("★") or None,
        })
    return rows


def parse_fund(html: str) -> dict:
    """個別ファンドのリターンのページ。

    {nav, nav_md("09/30"), nav_diff, returns: {月数: リターン%}, ranks: {月数: 全ファンド中の順位},
     stdev / stdev_ranks（値動きの荒さ＝年率の標準偏差）, sharpe / sharpe_ranks（荒さの割の増え方＝シャープレシオ）,
     nisa: 名前の上の帯の NISA の印の文字（帯が無ければ None）}
    表に「---」とある期間は入れない。
    """
    out: dict = {"nav": None, "nav_md": None, "nav_diff": None, "returns": {}, "ranks": {},
                 "stdev": {}, "stdev_ranks": {}, "sharpe": {}, "sharpe_ranks": {}, "nisa": None}
    info = re.search(r'<div class="[^"]*\bbg-MK-fund-info\b[^"]*">(.*?)</h1>', html, re.S)
    if info:
        out["nisa"] = [_text(x) for x in re.findall(r'<a[^>]*href="[^"]*nisa[^"]*"[^>]*>(.*?)</a>', info.group(1), re.S)]
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    flat = _text(body)
    m = re.search(r"基準価額\s+(\d{2}/\d{2})\s+([\d,]+)\s*円\s*([-+−]?[\d,]+)?", flat)
    if m:
        out["nav_md"] = m.group(1)
        out["nav"] = _num(m.group(2))
        out["nav_diff"] = _num(m.group(3)) if m.group(3) else None
    for table in re.findall(r"<table.*?</table>", body, re.S):
        trs = [[_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
               for tr in re.findall(r"<tr.*?</tr>", table, re.S)]
        head = next((r for r in trs if r and r[0] == "期間"), None)
        vals = next((r for r in trs if r and r[0] == "リターン"), None)
        if not head or not vals:
            continue
        for row_name, key, rank_key, pct in (("リターン", "returns", "ranks", True), ("標準偏差", "stdev", "stdev_ranks", False),
                                             ("シャープレシオ", "sharpe", "sharpe_ranks", False)):
            row = next((r for r in trs if r and r[0] == row_name), None)
            if not row:
                continue
            for label, cell in zip(head[1:], row[1:]):
                months = _months(label)
                v = re.match(r"\s*([-+−]?\d[\d,]*\.?\d*)\s*" + ("%" if pct else ""), cell)
                if months is None or not v:
                    continue
                out[key][months] = float(v.group(1).replace(",", "").replace("−", "-"))
                k = re.search(r"\(\s*(\d+)\s*位\s*\)", cell)
                if k:
                    out[rank_key][months] = int(k.group(1))
        break
    return out


def parse_compare(html: str) -> list[dict]:
    """比較のページ（/fund/compare?fund_ids=…）。列ごとに1本。

    [{id, name, fee, returns: {月数: %}, sharpe: {月数: 値}, info: {…}}]（1年・3年・5年。「-」は入れない）
    info＝torikomi.js の版6と同じ読み方の、なりたちの行（bf 手数料・dl／dy 分配金・st 決算回数・sd 設定日・ud 信託期間・rr RR分類）
    """
    for table in re.findall(r"<table.*?</table>", html, re.S):
        rows = {}
        for tr in re.findall(r"<tr.*?</tr>", table, re.S):
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
            if cells:
                rows[re.sub(r"\s", "", _text(cells[0]))] = cells
        if not all(k in rows for k in ("ファンド", "リターン", "シャープレシオ")):
            continue

        def years(cell: str) -> dict[int, float]:
            # 空白だけの部品は飛ばす（ブラウザでコピーすると「1年」と数字のあいだに <span>&nbsp;</span> が挟まるため）
            spans = [t for t in (_text(x) for x in re.findall(r"<span[^>]*>(.*?)</span>", cell, re.S)) if t]
            out = {}
            for a, b in zip(spans, spans[1:]):
                m, v = re.fullmatch(r"(\d+)年", a), re.fullmatch(r"([-+−]?[\d,]*\.?\d+)%?", b)
                if m and v:
                    out[int(m.group(1)) * 12] = float(v.group(1).replace(",", "").replace("−", "-"))
            return out

        funds = []
        for i, cell in enumerate(rows["ファンド"][1:], 1):
            link = re.search(r'<a[^>]*href="/fund/([0-9A-Za-z]{8})"[^>]*>(.*?)</a>', cell, re.S)
            if not link:
                continue
            fee = rows.get("実質信託報酬")
            funds.append({"id": link.group(1), "name": _text(link.group(2)),
                          "fee": _num(_text(fee[i])) if fee and i < len(fee) else None,
                          "returns": years(rows["リターン"][i]), "sharpe": years(rows["シャープレシオ"][i]),
                          "info": _compare_info(rows, i)})
        return funds
    return []


def _compare_info(rows: dict, i: int) -> dict:
    """比較のページの、なりたちの行（torikomi.js の CV・DT と同じ読み方。「-」だけのセルは無いものとする）。"""
    def cv(key):
        cell = rows.get(key)
        t = _text(cell[i]) if cell and i < len(cell) else ""
        return "" if re.fullmatch(r"-+", t) else t

    def dt(t):
        m = re.fullmatch(r"(\d{4})/(\d\d)/(\d\d)", t)
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
    fk = next((k for k in rows if k.startswith("手数料")), None)
    dv, ud = cv("分配金"), cv("信託期間")
    dl, dy = re.search(r"前回\s*([\d,]+)\s*円", dv), re.search(r"累計\s*([\d,]+)\s*円", dv)
    return {"bf": _num(cv(fk)) if fk and cv(fk) else None, "dl": _num(dl.group(1)) if dl else None,
            "dy": _num(dy.group(1)) if dy else None, "st": cv("決算回数") or None, "sd": dt(cv("設定日")),
            "ud": "none" if "無期限" in ud else dt(ud), "rr": cv("RR分類") or None}


def _months(label: str) -> int | None:
    m = re.match(r"\s*(\d+)\s*(ヶ月|ケ月|カ月|か月|年)", label)
    if not m:
        return None
    n = int(m.group(1))
    return n * 12 if m.group(2) == "年" else n


# 「ブルーチップ」「ブルームバーグ」「ベアリング」は、ブル・ベア型ではないので外す。
# 前がカタカナの「ブル」（サステナブル・ウェアラブル・インベスタブル・ファーブル・ルーブル）と「トリプル維新」も外す（2026-10-01 本番の名前で見つけた）
LEVERAGE = re.compile(r"(?<![ァ-ヶー])ブル(?!ー)|ベア(?!リング)|レバレッジ|レバ|インバース|ダブル|トリプル(?!維新)|[2-9２-９][\.．]?[0-9０-９]?倍")


def is_leveraged(name: str) -> bool:
    """名前から、ブル・ベア（レバレッジ・インバース）型かを見る。"""
    return bool(LEVERAGE.search(name))
