#!/usr/bin/env python3
"""離線驗證：對既有資料庫跑四條資料品質規則，列出觸發事件（唯讀）"""
import sqlite3, sys, time
from collections import Counter

# ---- 門檻（初稿，待驗證）----
R1_WIN_S, R1_MIN_BAD = 60, 3   # R1 網路失效：60 秒內 q=0 >= 3 筆
R2_RUN_BAD = 10                # R2 設備不在：連續 q=0 >= 10 筆
R3_GAP_S = 1.01                # R3 時間序列洞：相鄰間隔 > 1.01 s（由直方圖決定）
R4_FROZEN = 30                 # R4 值凍結：q=1 且值連續 30 次不變

def load(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    return c.execute("SELECT ts, quality, value FROM reading ORDER BY ts").fetchall()

def events(rows):
    ev = []
    bad = [ts for ts, q, v in rows if q == 0]
    j, last = 0, None
    for i, t in enumerate(bad):
        while bad[j] < t - R1_WIN_S:
            j += 1
        if i - j + 1 >= R1_MIN_BAD and (last is None or t - last > R1_WIN_S):
            ev.append((t, 'R1 網路失效', f'{i-j+1} 筆 / {R1_WIN_S}s'))
            last = t
    run = 0
    for ts, q, v in rows:
        run = run + 1 if q == 0 else 0
        if run == R2_RUN_BAD:
            ev.append((ts, 'R2 設備不在', f'連續 {run} 筆 q=0'))
    for (t0, *_), (t1, *_) in zip(rows, rows[1:]):
        if t1 - t0 > R3_GAP_S:
            ev.append((t1, 'R3 時間序列洞', f'{t1 - t0:.3f}s'))
    run, prev = 0, None
    for ts, q, v in rows:
        run = run + 1 if (q == 1 and v == prev) else 0
        prev = v if q == 1 else None
        if run == R4_FROZEN:
            ev.append((ts, 'R4 值凍結', f'連續 {run + 1} 筆 = {v}'))
    return sorted(ev)

for db in sys.argv[1:]:
    rows = load(db)
    ev = events(rows)
    print(f"\n===== {db}  ({len(rows)} 筆) =====")
    cnt = Counter(e[1] for e in ev)
    for k in ['R1 網路失效', 'R2 設備不在', 'R3 時間序列洞', 'R4 值凍結']:
        print(f"  {k:<8}: {cnt.get(k, 0)} 次")
    for t, k, d in ev[:25]:
        print(f"    {time.strftime('%m-%d %H:%M:%S', time.localtime(t))}  {k}  {d}")
    if len(ev) > 25:
        print(f"    …（共 {len(ev)} 筆事件，只列前 25）")
