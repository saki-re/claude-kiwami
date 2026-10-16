# -*- coding: utf-8 -*-
# 老後シミュレーターの計算（正解）。生成AIは呼びません。
# 画面（rougo.html の window.ROUGO）と極の道具（rougo_tools.py）は、これと1円まで同じ答えになるように作ってあります。
# 使い方： python docs/rougo_keisan.py  → 下の見本の数字で、何歳までお金が持つかを並べます。
# MANGAKU は国民年金の満額（月額）、HOKEN は国民年金の保険料（月額）。年度が変わったら直します。
MANGAKU = 70608; KOUSEI_RATE = 5.481 / 1000; HOKEN = 17920; MENJO_OMOMI = 0.5; SHIKAKU = 120


def mirai_kind(p, a):
    """辞めてから60歳になるまでの a歳の年に、国民年金をどうするか。harau／menjo／yuuyo／minou"""
    for (kara, made, kind) in p.get('mirai', []):
        if kara <= a <= made:
            return 'minou' if (kind == 'yuuyo' and a >= 50) else kind
    return 'minou'


def nenkin(p):
    """年金の月額（円）。"""
    if p.get('nenkin_direct') is not None:
        return p['nenkin_direct']
    yameru = p['kirikae'] if p.get('kirikae') is not None else p['age']
    hataraku = max(0, yameru - p['age']) * 12
    kiso_hataraku = max(0, min(yameru, 60) - p['age']) * 12
    harau = menjo = yuuyo = 0
    for a in range(max(yameru, p['age']), 60):
        k = mirai_kind(p, a)
        if k == 'harau': harau += 12
        elif k == 'menjo': menjo += 12
        elif k == 'yuuyo': yuuyo += 12
    kousei_m = p['kousei_m'] + hataraku
    shikaku = p['noufu_m'] + p['menjo_m'] + p['yuuyo_m'] + kousei_m + harau + menjo + yuuyo
    if shikaku < SHIKAKU:
        return p.get('sonota', 0)
    kiso_m = min(480, p['noufu_m'] + p['kousei_m'] + kiso_hataraku + harau + MENJO_OMOMI * (p['menjo_m'] + menjo))
    rou = kiso_m / 480 * MANGAKU + p['nenshu'] / 12 * KOUSEI_RATE * kousei_m / 12
    d = (p['kaishi'] - 65) * 12
    rou *= (1 + 0.007 * d) if d >= 0 else (1 - 0.004 * -d)
    return rou + p.get('sonota', 0)


def nenkin_nen(p, a, n):
    """a歳の年の年金の月額。nenkin_kawaru＝[(何歳から, 新しい月額)] があれば、その年齢から置き換える。
    画面（年金の枠の「○歳から変わる」）と極の道具（rougo_tools.py）が使う欄です（2026-10-14）。無ければ n のまま。"""
    for (kara, gaku) in p.get('nenkin_kawaru', []):
        if a >= kara: n = gaku
    return n


def shishutsu(p, a, flat=None):
    if flat is not None: return flat
    m = p['shishutsu']
    for (kara, gaku) in p.get('kawaru', []):
        if a >= kara: m = gaku
    return m


def ichiji_nen(row, a):
    """一時費用の行が、a歳の年にいくらになるか。"""
    kind = row[0]
    if kind == '1kai':                       # ('1kai', 金額, 年齢)
        return row[1] if a == row[2] else 0
    if kind == 'kurikaeshi':                 # ('kurikaeshi', 金額, はじめの年齢, 何年ごと, 何回)
        _, g, kara, goto, kai = row
        return g if (a >= kara and (a - kara) % goto == 0 and (a - kara) // goto < kai) else 0
    if kind == 'narasu':                     # ('narasu', 合計, 何歳から, 何歳まで)
        _, g, kara, made = row
        return g / (made - kara + 1) if kara <= a <= made else 0
    return 0


def zandaka(p, flat=None, tasu=0, tsumitate=None):
    """[(年齢, 預金, NISA, 合計)] その年の終わり。
    tasu＝いまの預金に足す額。tsumitate＝ためる時期の月額を、この額に置き換える。"""
    yokin = p.get('yokin', 0) + tasu; nisa = p.get('nisa', 0); r = p.get('r', 0)
    n = nenkin(p); kirikae = p.get('kirikae'); out = []
    rows = p.get('tameru', [])
    if tsumitate is not None:                             # 行が無いときは、いまの年齢から預金に
        zen = sum(x[2] for x in rows); wari = (sum(x[2] for x in rows if x[3]) / zen) if zen else 0
        kara = min(x[0] for x in rows) if rows else p['age']
        rows = [(kara, 200, tsumitate * wari, True), (kara, 200, tsumitate * (1 - wari), False)]
    for a in range(p['age'], p['mokuhyo']):
        if p.get('sagaru') and a == p['sagaru'][0]:
            nisa *= (1 - p['sagaru'][1])                     # その年のはじめに NISA が下がったら
        nisa *= (1 + r)                                      # 年のはじめの残高に利回りを掛ける
        if kirikae is not None and a < kirikae:              # ためる時期：貯める額だけ足す
            for (kara, made, tsuki, toushi) in rows:
                if kara <= a <= made:
                    if toushi: nisa += tsuki * 12
                    else: yokin += tsuki * 12
        else:                                                # とりくずす時期：年金 − 支出
            yokin -= shishutsu(p, a, flat) * 12
            if a < 60 and p.get('nenkin_direct') is None and mirai_kind(p, a) == 'harau':
                yokin -= HOKEN * 12
            if a >= p['kaishi']: yokin += nenkin_nen(p, a, n) * 12
        for (nen, gaku) in p.get('isan', []):
            if a == nen: yokin += gaku
        for row in p.get('ichiji', []):
            yokin -= ichiji_nen(row, a)
        if yokin < 0 and nisa > 0:                           # 預金が足りなければ NISA から出す
            x = min(nisa, -yokin); nisa -= x; yokin += x
        out.append((a, yokin, nisa, yokin + nisa))
    return out


def tsukiru(p, **kw):
    for a, y, n, g in zandaka(p, **kw):
        if g < -0.5: return a
    return None


def ato_ikura(p, **kw):
    """目標の年齢まで持たせるのに、いまの預金にあといくら足せばいいか（1,000円きざみ・切り上げ）。"""
    if tsukiru(p, **kw) is None: return 0
    lo, hi = 0, 100000000
    if tsukiru(p, tasu=hi, **kw) is not None: return None   # 1億円を足しても持たない
    while hi - lo > 1000:
        mid = (lo + hi) // 2 // 1000 * 1000
        if mid == lo: mid += 1000
        if tsukiru(p, tasu=mid, **kw) is None: hi = mid
        else: lo = mid
    return hi


def tsuki_ikura(p, **kw):
    """目標の年齢まで持つ、とりくずす時期の月の支出の上限（100円きざみ・切り捨て）。"""
    if tsukiru(p, flat=0, **kw) is not None: return None
    lo, hi = 0, 1000000
    while hi - lo > 100:
        mid = (lo + hi) // 2 // 100 * 100
        if mid == lo: mid += 100
        if tsukiru(p, flat=mid, **kw) is None: lo = mid
        else: hi = mid
    return lo


def tsumitate_ikura(p, **kw):
    """目標の年齢まで持たせるのに、ためる時期に月いくら積み立てればいいか（100円きざみ・切り上げ）。"""
    lo, hi = 0, 1000000
    if tsukiru(p, tsumitate=hi, **kw) is not None: return None
    if tsukiru(p, tsumitate=0, **kw) is None: return 0
    while hi - lo > 100:
        mid = (lo + hi) // 2 // 100 * 100
        if mid == lo: mid += 100
        if tsukiru(p, tsumitate=mid, **kw) is None: hi = mid
        else: lo = mid
    return hi


def mihon_a(x, r=0.0, **kw):
    """見本A（架空）：親と住んでいて、x歳で一人暮らしになる人。"""
    d = dict(age=45, mokuhyo=90, yokin=3000000, nisa=500000, r=r, kirikae=x, nenkin_direct=70000, kaishi=65,
             tameru=[(45, 89, 20000, True), (45, 89, 10000, False)],
             shishutsu=79000, kawaru=[(65, 81000)],
             ichiji=[('kurikaeshi', 300000, 50, 10, 3), ('1kai', 150000, 60), ('narasu', 2000000, 46, 89)],
             isan=[(x, 2000000)])
    d.update(kw); return d


def mihon_b(yameru, mirai='harau', **kw):
    """見本B（架空）：働いていて、yameru歳で辞める人。"""
    d = dict(age=35, mokuhyo=90, yokin=1000000, nisa=0, r=0.0, kirikae=yameru, kaishi=65,
             noufu_m=60, menjo_m=0, yuuyo_m=0, kousei_m=120, nenshu=3500000,
             mirai=[(yameru, 59, mirai)], tameru=[(35, 89, 20000, False)],
             shishutsu=120000)
    d.update(kw); return d


if __name__ == '__main__':
    print('■ 見本A：一人暮らしになる年齢 × 利回り')
    print('  年齢 利回り 何歳で尽きる 89歳の終わり あといくら 月いくら 積立は月いくら')
    for x in (50, 55, 60, 65):
        for r in (0.0, 0.03):
            p = mihon_a(x, r)
            print(' ', x, r, tsukiru(p), round(zandaka(p)[-1][3]), ato_ikura(p), tsuki_ikura(p), tsumitate_ikura(p))
    print('■ 見本B：辞める年齢 × 辞めてからの国民年金')
    print('  年齢 どうするか 年金の月額 何歳で尽きる 月いくら あといくら')
    for y in (50, 55, 60, 65):
        for m in ('harau', 'menjo', 'minou'):
            p = mihon_b(y, m)
            print(' ', y, m, round(nenkin(p)), tsukiru(p), tsuki_ikura(p), ato_ikura(p))
    for k in (60, 70, 75):
        d = (k - 65) * 12
        r = 100 * ((1 + 0.007 * d) if d >= 0 else (1 - 0.004 * -d))
        x = (r * k - 100 * 65) / (r - 100)
        print('■ 損益分岐', k, '歳開始', int(x), '歳', round((x - int(x)) * 12), 'か月')
