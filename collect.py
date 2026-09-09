# 這支程式要用來跑長時間運行，並且將數據寫入資料庫。
# 階段三只做一件事：把值誠實地存下來，包括失敗的那些。
# 「誠實」是重點——不補值、不平滑、不跳過失敗。原始資料一旦被動過，之後就再也還原不回來了。

import sqlite3
import time

from pymodbus.client import ModbusTcpClient

DB_PATH  = r'D:\Projects\counter\readings.db'
HOST     = '127.0.0.1'
PORT     = 5020
TAG      = 'counter_value'
INTERVAL = 1.0

# ---------- 建表（已存在就跳過）----------
conn = sqlite3.connect(DB_PATH)
conn.execute('''
CREATE TABLE IF NOT EXISTS reading (
    ts      REAL    NOT NULL,   -- Unix 時間戳（秒，UTC）
    tag     TEXT    NOT NULL,   -- 訊號名稱 counter_value
    value   REAL    NOT NULL,
    quality INTEGER NOT NULL    -- 0=讀取失敗，1=正常
)''')
conn.execute('CREATE INDEX IF NOT EXISTS idx_reading_tag_ts ON reading(tag, ts)') 
#建立複合索引（tag和ts），比較快找到元素
conn.commit()  #提交變更

client = ModbusTcpClient(HOST, port=PORT, timeout=0.5)  # 連線超時 0.5 秒
#client.connect()
if not client.connect():                                # 第 4 點
    print(f'cannot connect to {HOST}:{PORT}')
print(f'writing to {DB_PATH}   (Ctrl+C to stop)')

prev_m = prev_w = None      # 上一輪的兩個時鐘讀數；_m是 monotonic，_w 是 wall clock
n_ok = n_bad = 0            # 取值成功／失敗的筆數

# 開始讀取數據
try:
    while True:
        # ─── 這一輪的起點：兩個時鐘「同時」取樣 ───
        now   = time.monotonic()   # 增量式：開機後累積秒數，只能量「差」，不受校時影響
        now_w = time.time()        # 絕對式：Unix Epoch 起算，可以當時間戳

        # ─── 檢查 1：迴圈有沒有變慢（monotonic 自己跟自己比）───
        if prev_m is not None and now - prev_m > 1.05:
            print(f'{time.strftime("%H:%M:%S")}  LOOP LATE: 週期={now - prev_m:.3f}s')

        # ─── 檢查 2：兩個時鐘有沒有走不一樣快 ───
        if prev_m is not None:
            dm = now   - prev_m    # monotonic 走了多久
            dw = now_w - prev_w    # 牆上時鐘走了多久
            if abs(dm - dw) > 0.05:
                print(f'{time.strftime("%H:%M:%S")}  CLOCK SKEW: '
                      f'mono={dm:.3f}s  wall={dw:.3f}s  差={dw - dm:+.3f}s')

        prev_m, prev_w = now, now_w

        next_t = now + INTERVAL    # 下次該醒的時刻（用 monotonic 算，不會被校時干擾）
        ts     = now_w             # 存進資料庫的時間戳（用同一瞬間的牆上時鐘）
        t0     = now               # 每次讀取Modbus的開始時間

        # ─── 讀 Modbus ───
        try:
            r = client.read_holding_registers(address=0, count=1)
            if r.isError():
                value, quality = 0.0, 0  # 讀取失敗，存 0.0 並標記品質為 0
                print(f'{time.strftime("%H:%M:%S")}  MODBUS ERROR: {r!r}')
                # !r：使用 Python 的 repr(r) 顯示方式，適合除錯。
                client.connect()
            else:
                value, quality = float(r.registers[0]), 1
        except Exception as e:  # noqa: BLE001 ← 採集程式不能因單次失敗而死
            value, quality = 0.0, 0
            print(f'{time.strftime("%H:%M:%S")}  READ FAILED: {e!r}')
            client.connect()       # 試著重連

        t1 = time.monotonic()

        # ─── 寫一列進資料庫 ───
        conn.execute(
            'INSERT INTO reading (ts, tag, value, quality) VALUES (?,?,?,?)',
            (ts, TAG, value, quality))
        conn.commit()

        t2 = time.monotonic()   # 每次寫入資料庫的結束時間

        # ─── 檢查 3：工作本身有沒有變慢（讀取Modbus + 寫入資料庫）───
        if t2 - t0 > 0.2:
            print(f'{time.strftime("%H:%M:%S")}  SLOW  '
                  f'read={1000*(t1-t0):.0f}ms  write={1000*(t2-t1):.0f}ms')

        # ─── 統計 ───
        n_ok  += quality
        n_bad += (1 - quality)

        if (n_ok + n_bad) % 60 == 0:    # 每 60 筆印一行，證明它還活著
            print(f'{time.strftime("%H:%M:%S")}  ok={n_ok}  bad={n_bad}  last={value:.0f}')

        # ─── 睡到下一秒 ───
        time.sleep(max(0, next_t - time.monotonic()))
        # 用增量式時鐘算「下次該醒的時刻 − 現在」，防止累積漂移。
        # 負數表示已經超過下次採集時間，睡 0 秒，直接進下一輪。

except KeyboardInterrupt:   # Ctrl+C to stop
    print(f'\nstopped.  ok={n_ok}  bad={n_bad}')
finally:
    client.close()
    conn.close()