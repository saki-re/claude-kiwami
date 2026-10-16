# 投資信託ランキング（`toushin/`・8050）

**このフォルダの入口です。** 持ち主が Claude Code と一緒に直すときの手引きを、そのまま公開しています。
確かめ役（`tools/verify/verify_toushin.py`）と家族に見せる表を刷る台本（`tools/toushin_insatsu.py`）は、公開版には入っていません。

- **開く**：`http://localhost:8050`（同じ PC）か、同じ家の中のネットワークからの `http://<PCの名前>:8050`。合言葉なし・外（インターネット）へは出さない。持ち主は PC の Edge と iPhone の Safari で見る。
- **動いている場所**：1台の PC だけ。データ（`data/`）は git に無いので、ほかの PC には DB も下調べの写しも無い（ほかの PC で起こし直さない）。
- **起動**：予約タスク `Toushin-Ranking`（ログオン時）→ `_launch_hidden_toushin.vbs`（落ちたら5秒で起き直る）→ `_run_toushin.bat`（Python314 の場所が直書き）。登録し直しは `登録する.ps1`。
- **毎朝9時**：予約タスク `Toushin-Notify` → `notify.py`：先に `backup.py`（データが変わった日だけ Google ドライブへ控え）、次に新しい月の成績が無ければ **8050 の「自動で取る」を始める**（`/api/jidou` に tsuchi＝`jidou_torikomi.py --tsuchi`）。取り終わると、家族に見せる表（`tools/toushin_insatsu.py --8050`＝3ヶ月の成績順・100位まで）を `<置き場>` へ「【PDF】投資信託ランキング_2026年11月版.pdf」（＝10月末締め・その月の版がもうあれば作らない）で置き、別の PC で刷る用に同じ表の HTML を `<置き場>\投資信託ランキング\【印刷用】…月版.html` で置き（そちらの PC が `toushin_insatsu.py --html … --insatsu` で刷る）、「入れかえました」か「途中で止まりました」を Discord へ1通。みんかぶがまだ新しい月でなければ黙って、あしたの朝にまた見る。8050 に届かないときだけ、いままでの「更新の時期です」。登録し直しは `毎月のお知らせを登録する.ps1`。ログは `data/_notify_log.txt`。

---

## 1. これは何か

持ち主（と家族）が、みんかぶ投資信託のリターンのランキングを、期間をまたいで見比べて、**平均的に良いファンドを追う**ための画面。


### 作るときの要望（芯になったもの）

- みんかぶの投資信託のリターンランキング（1ヶ月・3ヶ月・6ヶ月・12ヶ月・24ヶ月・100件まで）をグラフにする
- 好きな期間を細かく選べて、ランキングのように並び、**平均的に良さそうなファンドを追える**こと
- PC で見られて、レスポンシブで iPhone でも見られること
- ファンドの3ヶ月と6ヶ月を同時に出すこと
- 値動きの荒さ・効率（シャープレシオ）も見られること
- 毎日少しずつ取り込むのは手間なので、とにかく手動の手間を減らすこと


---

## 2. 使い方（月1回）

1. 毎月1日ごろ、Discord に「更新の時期です」（画面を開いてもお知らせ）。
2. 「取り込む・つづき」→ いちばん上の「はじめる」。開いたページでブックマークの「取り込む（版の数つき・いまは「取り込む6」）」を押すだけで、次のページが自動で開く。最後に「今月ぶんは、全部取り込めました🎉」。
   **ブックマークを押さずに取るとき**は、その下の「自動で取る（ブックマークなし）」。8050 が `jidou_torikomi.py` を裏で回し（見えない Edge・1ページ1秒・ランキング300位まで→比較→♡のファンドのページ→NISA の印を最後まで（月1回）で15分くらい）、進み具合を同じ欄に出す。iPhone から押して閉じてもよい。
3. （1つずつ選んで開くときだけ）「ほかの取り込み」の中の、期間ごとの「…位を開く」と「まとめて取る」。
4. NISA の印は、たまに（最初に1回）。「NISA の印をまとめて取る」から NISA のランキングを開いて「取り込む」。
5. 追いかけたいファンドの値動きの荒さ・NISA は、そのファンドの「リターン」のページで「取り込む」（♡に入る）。
6. 見るとき：「平均」の並びのほか、「表」（列を押して並べかえ・PC で印刷）・「地図」（荒さ×1年の増え方）・「今月の動き」・「？」（数字の読み方）。持っているファンドは、くわしい面の「買った値段を書いておく」。

月が変わったら、**先にランキングを1ページ取り込んでから**、ファンドのページ・比較のページを押す（どちらも「いつまでの成績か」がページに無く、ランキングのいちばん新しい月にそろえて貯めるため＝`store.fund_period`）。
Edge で「取り込む」を押して新しいタブに `about:blank#blocked` と出たら、空のタブで押している（みんかぶのタブの中で押す）。お気に入りを新しいタブで開く設定でも同じことが起きる。

---

## 3. 流れ

```
みんかぶのページ（持ち主のブラウザ）
  ├ ブックマーク「取り込む」＝ torikomi.js を1行に詰めた javascript:（app.py の /api/bookmarklet が作る）
  │     開いているページを読む → <開いた画面の住所>/torikomi#<JSON> へ画面ごと移る（https→http は裏で送れないため）
  └ Ctrl+A → Ctrl+C → この画面で Ctrl+V
        index.html の fromPaste が /extract.js（＝同じ torikomi.js）で読む
→ index.html が POST /api/import → app.py が形と範囲を確かめる → store.py が SQLite へ足す
→ GET /api/latest（画面に渡す1組）・/api/fund/{id}（推移）→ index.html が描く
```

---

## 4. ファイルの係

| ファイル | 係 |
|---|---|
| `torikomi.js` | **みんかぶのページを読む、ただ1か所。** `X(D, L, P)` がページの種類ごとに読む（ランキング／ファンドのリターン／比較／NISA のランキング）。版 `V` |
| `app.py` | サーバー（8050）。`/`・`/torikomi`（画面）・`/api/import`（kind＝rank・fund・compare・nisa・unknown）・`/api/latest`・`/api/fund/{id}`・`/api/star`・`/api/picks`・`/api/holding`（買った値段）・`/api/bookmarklet`（torikomi.js を1行に詰める）・`/extract.js`。基準価額の日の決め方 `_nav_day`、比較のページのなりたちの確かめ `_compare_info` |
| `store.py` | SQLite（`data/toushin.db`）と ♡（`data/stars.json`）・「取る」（`data/picks.json`）・買った値段（`data/holdings.json`）。`latest()` が画面に渡す1組を組む（常連・続けて下がる＝`_streaks`、今月の動き＝`_month_moves`、月ごとの値段＝`_month_navs`） |
| `import_file.py` | 持ち主が用意した利回りの JSON を入れる（kind='file' で記録・「取る」の済み／未には数えない）。`scan()` が Google ドライブの置き場（`TOUSHIN_DROP_DIR`・既定 `<置き場>`）を見る。`app.py` の `_drop_loop` が、本番（`python app.py`）のときだけ5分ごとに呼ぶ |
| `jidou_torikomi.py` | 「自動で取る」。8050 の画面を見えない Edge で開いて `chainSteps()` に「まだのページ」を聞き、みんかぶのそのページを `torikomi.js` の `toushinExtract` で読んで `/api/import` へ送る（読み方・選び方を2か所に書かない）。`app.py` の `/api/jidou`（POST＝回す・GET＝様子）が呼び、記録は `data/jidou.log`、NISA を取り切った月は `data/jidou_nisa.json`。手で回すときは `python jidou_torikomi.py --depth 300` |
| `index.html` | 画面1枚（CSS・HTML・JS）。おまかせ＝`chainBlock`・`chainSteps`・`chainPicks`・`goNextSoon`（続きの印は localStorage の `toushin-chain`）。一覧・平均・♡・くわしい面・推移のグラフ・取り込みの面・お知らせ。初心者向け：`roughOf`（荒さ）・`badgesOf`（印）・`yenBlock`（100万円で）・`holdBlock`（買った値段）・`navmBlock`（月ごと）・`infoBlock`（なりたち）・`groupOf`／`matesBlock`（同じ指数）・`renderTable`（表）・`renderMap`（地図）・`openHelp`（`HELP` の17枚）・`openMoves`（今月の動き）・`renderFiltOn`（しぼりこみ中） |
| `minkabu.py` | **サーバーも使う**（`app.py` が住所 `ranking_url`・`BASE`、`store.py` が期間 `TERMS`・2倍・3倍型の見分け `is_leveraged`）。加えて、torikomi.js と同じ読み方を Python で書いた `parse_ranking`・`parse_fund`・`parse_compare`（確かめ役の答え合わせ用）。消さない |
| `notify.py` | 毎朝9時：控え（`backup.py`）→ 新しい月の成績が無ければ Discord へ1通（その月は1回・済みの印 `data/notified.json`）。宛先は環境変数か `.env` から探す（`TOUSHIN_DISCORD_WEBHOOK`）。`--dry`＝送らず条件と中身だけ・`--test`＝確かめの1通。画面の住所 `PAGE_URL` が直書き |
| `backup.py` | データが変わった日だけ `<バックアップ先>\toushin\toushin_日付.zip`（DB・♡・取る・買った値段）。新しい14個を残す。置き場が見えないと黙って飛ばす（`_notify_log.txt` に1行だけ） |
| `../toushin_tools.py` | Claude のアプリから呼ぶ道具4本（♡・探す・くわしく・今月の動き）。設定のスイッチ「投資信託ランキング」が入のときだけ載る。8050 の `/api/latest`・`/api/fund/{id}` に 127.0.0.1 から GET するだけ。確かめは `../tools/verify/verify_toushin_tools.py` |
| `_launch_hidden_toushin.vbs`・`_run_toushin.bat`・`登録する.ps1`・`毎月のお知らせを登録する.ps1` | 起動と予約タスク（`.ps1` は UTF-8 BOM・Python314 の場所が直書き） |
| `data/` | git に入れない。DB・♡・取る・`notified.json`・ログ（`_toushin_console.log`・`_notify_log.txt`）・控えを戻す前の写し・下調べの本物のページ（`raw/2026-09-30/`） |
| `../tools/verify/verify_toushin.py` | 確かめ役（6. を見る） |

---

## 5. データの形

**日付は2つ。**
- 成績（順位・リターン・効率）＝「成績の月」：ランキングの「2026/03 ~ 2026/08」の終わりの月の末日（例 2026-08-31）。ファンドのページ・比較のページには成績の期間が無いので、ランキングのいちばん新しい月にそろえる（`fund_period`）。
- 基準価額＝「値段の日」：ランキングは「更新：2026/10/01 07:09」の日。**18時より前の更新なら前の営業日**（土日だけ飛ばす・祝日は見分けない・時刻が読めなければその日のまま。`app._nav_day`）。ファンドのページは「基準価額 09/30」の日（`store.nav_day_from_md`）。

| 表 | 中身 |
|---|---|
| `rank_rows(date, term, rank, fund_id)` | 成績の月・期間ごとの並び。同じ月・同じ順位は置きかえ |
| `term_meta(date, term, span)` | 成績の期間の表示 |
| `fund_daily(date, fund_id, …)` | 成績の月の行＝r3〜r60（リターン）・k（全ファンド中の順位）・s／sk（値動きの荒さ＝標準偏差とその順位）・p／pk（効率＝シャープレシオとその順位）。値段の日の行＝nav・nav_diff・fee・rating。比較のページの fee だけは成績の月の行に入る |
| `funds(id, name, company, first_seen, last_seen, nisa)` | nisa は印の文字の JSON。**NULL＝まだ分からない・`[]`＝ページを見たが印は無かった**（画面の「—」と「なし」）。足すだけで消さない |
| `fund_info(fund_id, buy_fee, dist_last, dist_year, settle, setup, until, rr, at)` | なりたち（比較のページの手数料（上限・税込）・分配金（前回・1年累計 円／1万口）・決算回数・設定日・信託期間（`none`＝無期限）・RR分類）。ファンドごとに1行。「-」の欄は前の値のまま |
| `imports(at, date, kind, term, n, first, last, fund_id)` | 取り込みの記録。kind＝rank・fund・compare・nisa。「取る」の済み／未は fund と compare の日付で見る。**nisa の行は fund_id 欄にランキングの名前（`yield_nisa_growth`／`yield_nisa`）、first 欄にページ番号を置く**（取り込みの面の「◯ページ目まで」） |

`latest()`：期間ごとに、その期間を最後に取り込んだ月の並び（今月1年だけ取り込んでも6ヶ月は前の月のまま見える）。並ぶのはランキングに居るファンドと、♡・「取る」のファンド。
数字は欄ごとに、130日までさかのぼって入っている新しい値（`LOOK_BACK_DAYS`）。先月からの順位の動き（`mv`）は、その期間のひとつ前の月と比べる。
ほかに渡す欄：`r["60"]`・`risk.p["60"]`（5年・比較のページから）、`g["3"]`・`g["6"]`（貯めた基準価額から出した値上がり・前後7日）、`stay`・`down`（2か月以上だけ。取り込まなかった月で数えるのをやめる。画面の「常連」は3か月から）、`info`、`hold`、`navm`（♡と買った値段のファンドの月ごとの基準価額）、`coverage[期間].months`、上の段の `moves`（期間ごとの新しく入った・外れた。両方の月で1位から切れ目なく入っている深さ `depth` まで）・`m3_from`・`m6_from`。

---

## 6. 直すとき

### 確かめ役 `tools/verify/verify_toushin.py`（chromium と webkit・幅390 と 1280・0円）
- データは一時フォルダ（`TOUSHIN_DATA`）・サーバーは別のポート（8797）で回す。**本番の `toushin/data` と 8050 には触らない。** Discord も偽の受け口、控えも一時フォルダ。
- みんかぶの住所は全部、ここで作った**偽のページ（本物と同じ作り）**にすり替える。外へ出る通信は0回であることを最後に数える。
- `data/raw/2026-09-30/` に本物の写し（ランキング・個別・比較）が在れば、それでも答え合わせする（`<thead>` が無いことは、これで見つかった）。
- 貼り付けの確かめは、本当に Ctrl+A → Ctrl+C → Ctrl+V を押す（chromium だけ）。コピーで挟まる `<span>&nbsp;</span>` は、これで見つかった（保存した生の HTML には入っていない）。
- `static_checks` が、torikomi.js の禁止語・移る先・`//`、サーバーの通信部品、画面の `innerHTML` を数える。
- 初心者向けの分は `beginner_server_checks`（なりたち・5年・常連・続けて下がる・今月の動き・3/6ヶ月・月ごとの値段・買った値段）と `beginner_ui_checks`（絞り込み・表・地図・読み方・今月の動き・くわしい面・なかま・買った値段）。偽の比較のページ（`compare_html`）は本物と同じなりたちの行を持つ。老後シミュレーターの受け口は `verify_rougo.py` の `投資信託からの橋を確かめる`。
- 写真は最後に出る一時フォルダ。**直したら写真を自分の目で見る**（iPhone の幅の `webkit_390_*.png`）。

### 読めるページを足すとき（例：比較のページ・NISA のランキングを足した 10/1）
1. ページの作りを生の HTML で確かめる（WebFetch の要約は2回外れた）。
2. `torikomi.js` の `X` に枝を足す。表の形が違えば `{a:'…作った人に知らせてください'}` を返して写さずに止める形にする。**`V=` の数を1つ上げる。**
3. 新しい読み方なら `minkabu.py` に同じ読み方を Python で（確かめ役の答え合わせ用）。いつものランキングと同じ作りのページ（NISA のランキング）なら要らない（確かめ役は DB を直接見る）。
4. `app.py` の `/api/import` に kind を足し、形と範囲を確かめる（行の上限も見る）。`store.py` に入れ場所。
5. `index.html` の `showResult`・`applyResult`・`fromPaste`（貼り付けでの見分け）。
6. 確かめ役に偽のページ（本物と同じ作り）と確かめを足す。
7. 持ち主に「ブックマークの入れ直し」をお伝えする（古いブックマークは、読めないページで「入れ直して」と出る）。

### 起こし直し（8050 だけ・伺わなくてよい）
持ち主が取り込み中でないかを見てから（最後の取り込みから3分以上あいていれば）、8050 の python を止める → vbs が5秒で起こす。
最後の取り込みの時刻：`python -c "import sqlite3; print(sqlite3.connect('file:toushin/data/toushin.db?mode=ro', uri=True).execute('SELECT MAX(at) FROM imports').fetchone()[0])"`
取り込み中に止めると、その1回が「サーバーに届きませんでした」になる。

### 控えから戻す
8050 の python を止め（vbs が起こす前に）→ いまの `data/toushin.db` を `data/` の中に別名で写して残す → `<バックアップ先>\toushin\toushin_日付.zip` の `toushin.db`・`stars.json`・`picks.json`・`holdings.json` を `data/` へ。

### 記録
直しは `git` に、経緯（何を頼まれ・何を確かめたか）はコミットの文に。この README は「いまの形」を書き直す（経緯は書かない）。

---


---


