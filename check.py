import datetime
import sqlite3

conn = sqlite3.connect(r'D:\Projects\counter\readings.db')

# ── 基本統計 ──
mn, mx, cnt, ok, bad = conn.execute('''
    SELECT MIN(ts), MAX(ts), COUNT(*),
           SUM(quality = 1), SUM(quality = 0)
    FROM reading
''').fetchone()

span = mx - mn
print(f'時間跨度 : {span:.1f} 秒 ({span/3600:.2f} 小時)')
print(f'總筆數   : {cnt}')
print(f'正常/失敗: {ok} / {bad}')

# ── 取樣間隔統計 ──
g_min, g_max, g_avg, _ = conn.execute('''
    SELECT MIN(gap), MAX(gap), AVG(gap), COUNT(*) FROM (
        SELECT ts - LAG(ts) OVER (ORDER BY ts) AS gap FROM reading
    ) WHERE gap IS NOT NULL
''').fetchone()

drift = g_avg - 1.0    #平均每一次遠離1.0秒多遠
print(f'\n取樣間隔 : 最小 {g_min:.4f}s   最大 {g_max:.4f}s   平均 {g_avg:.6f}s')
print(f'           抖動   ±{1000*max(g_avg-g_min, g_max-g_avg):.1f} ms')
print(f'           相位漂移 {1000*drift:+.3f} ms/次'
      f'  →  漂滿一秒約 {1/abs(drift)/60:.1f} 分鐘')   # 幾分鐘之後，會飄移超過一秒

# ── 真正的中斷（間隔超過 1.5 秒的）───
print('\n異常間隔（> 1.5 秒）：')
rows = conn.execute('''
    SELECT prev_ts, gap FROM (
        SELECT LAG(ts) OVER (ORDER BY ts) AS prev_ts,
               ts - LAG(ts) OVER (ORDER BY ts) AS gap
        FROM reading
    ) WHERE gap > 1.5
''').fetchall()

if rows:  # 間隔超過 1.5 秒的筆數，如果有就印出來。
    for prev_ts, gap in rows:
        t = datetime.datetime.fromtimestamp(prev_ts, datetime.timezone.utc).astimezone()
        print(f'  {t:%H:%M:%S}  間隔 {gap:.3f}s')
else:
    print('  無')

# ── 最近 10 筆 ──
print('\n最近 10 筆：')
for ts, tag, value, quality in conn.execute(
        'SELECT ts, tag, value, quality FROM reading ORDER BY ts DESC LIMIT 10'):
    t = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone()
    print(f'  {t:%H:%M:%S}.{t.microsecond//1000:03d}  {tag}  {value:8.0f}  q={quality}')

conn.close()