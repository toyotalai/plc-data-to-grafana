# =============================================================================
# collect.py — 組態驅動的資料採集程式
#
# 這支程式不知道自己接了什麼設備。設備清單、暫存器位址、換算係數，
# 全部在 devices.toml 裡。加一顆感測器只要改組態，程式碼一行都不用動。
#
# 驗收標準：在這個檔案裡 grep 不到任何設備專屬的字串
#           （192.168.50.1、xymd02、ttyUSB0、9600 都不該出現）
#
# 階段三的原則不變：把值誠實地存下來，包括失敗的那些。
# 不補值、不平滑、不跳過失敗。原始資料一旦被動過，就再也還原不回來了。
# =============================================================================

import logging
import os
import sqlite3
import time
import tomllib

from pymodbus.client import ModbusSerialClient, ModbusTcpClient

# pymodbus 自己的 logger 會在每次失敗時印一行，跟我們的輸出重複。
# 留著會污染日誌檔，所以關掉。
logging.getLogger('pymodbus').setLevel(logging.CRITICAL)

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.environ.get('CONFIG_PATH', os.path.join(HERE, 'devices.toml'))
DB_PATH = os.environ.get('DB_PATH', os.path.join(HERE, 'readings.db'))


# ---------------------------------------------------------------------------
# 組態
# ---------------------------------------------------------------------------
def load_config(path):
    """讀組態檔。TOML 讀進來就是巢狀的 dict 和 list，不需要額外處理。"""
    with open(path, 'rb') as f:        # tomllib 吃 bytes，所以是 'rb'
        return tomllib.load(f)


def build_client(dev):
    """依照 protocol 建立對應的 client。

    每個設備有自己的 timeout 和 retries —— 這是商業 gateway「每個埠有自己
    的參數」的對應物。TCP 走乙太網路可以給寬一點，RTU 走 9600 bps 要給緊
    一點，因為失敗耗時 = timeout × (1 + retries)。
    """
    protocol = dev['protocol']

    if protocol == 'modbus_tcp':
        return ModbusTcpClient(
            dev['host'],
            port=dev['port'],
            timeout=dev.get('timeout', 1.0),
            retries=dev.get('retries', 0))

    if protocol == 'modbus_rtu':
        return ModbusSerialClient(
            port=dev['port'],
            baudrate=dev.get('baudrate', 9600),
            bytesize=dev.get('bytesize', 8),
            parity=dev.get('parity', 'N'),
            stopbits=dev.get('stopbits', 1),
            timeout=dev.get('timeout', 1.0),
            retries=dev.get('retries', 0))

    raise ValueError(f"未知的 protocol: {protocol!r}（支援 modbus_tcp / modbus_rtu）")


# ---------------------------------------------------------------------------
# 讀取
# ---------------------------------------------------------------------------
def read_tag(client, dev, tag):
    """讀一個 tag。回傳 (value, quality, error)。

    這個函式**絕不丟例外**。採集程式不能因為單次失敗而死掉——
    死掉就留不下失敗紀錄，儀表板上會是一段空白，而不是一段標著「讀不到」
    的資料。

    失敗分兩種形式，pymodbus 的行為不一樣：
      從站回了錯誤回應 -> 回傳 ExceptionResponse 物件，isError() 抓得到
      從站完全沒回應   -> **丟出** ModbusIOException，isError() 抓不到
    """
    fc = tag['function']
    address = tag['address']
    device_id = dev.get('device_id', 1)

    try:
        if fc == 3:      # 0x03 Read Holding Registers（設定值）
            rr = client.read_holding_registers(address, count=1, device_id=device_id)
        elif fc == 4:    # 0x04 Read Input Registers（量測值）
            rr = client.read_input_registers(address, count=1, device_id=device_id)
        else:
            return 0.0, 0, f'不支援的功能碼 {fc}（目前支援 3 和 4）'
    except Exception as exc:          # noqa: BLE001 ← 刻意攔截全部
        return 0.0, 0, f'{type(exc).__name__}: {exc}'

    if rr.isError():
        return 0.0, 0, repr(rr)

    raw = rr.registers[0]

    # 有號 16 位元的二補數。不做這步的話，冬天 -5.0 °C 會讀成 6548.6。
    if tag.get('signed', False) and raw > 32767:
        raw -= 65536

    return raw * tag.get('scale', 1.0), 1, None


def reconnect(client, name):
    """先關舊的，再重連。

    TCP 必須這樣做：函式庫的 connect() 看到舊的連線物件還在，會直接回傳
    True 什麼也不做——階段六的「重連從未真的重連」就是漏了 close()。

    RTU 不需要：RS-485 是無狀態的，沒有連線可以半開。實測拔掉 A 線再接
    回去，下一秒自己就恢復了。所以這個行為由組態的 reconnect_on_error
    決定，而不是寫死在程式裡。
    """
    try:
        client.close()
        client.connect()
    except Exception as exc:          # noqa: BLE001
        print(f'{time.strftime("%H:%M:%S")}  RECONNECT FAILED [{name}]: {exc!r}')


# ---------------------------------------------------------------------------
# 資料庫
# ---------------------------------------------------------------------------
def open_db(path):
    conn = sqlite3.connect(path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reading (
            ts      REAL    NOT NULL,   -- Unix 時間戳（秒，UTC）
            tag     TEXT    NOT NULL,   -- 訊號名稱
            value   REAL    NOT NULL,
            quality INTEGER NOT NULL    -- 0=讀取失敗，1=正常
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reading_tag_ts ON reading(tag, ts)')
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------
def main():
    cfg = load_config(CONFIG_PATH)
    gw = cfg.get('gateway', {})
    interval = gw.get('interval', 1.0)
    slow_threshold = gw.get('slow_threshold', 0.2)

    devices = cfg.get('device', [])
    if not devices:
        raise SystemExit(f'組態檔裡沒有任何 device：{CONFIG_PATH}')

    # 啟動時把載入的內容印出來。看 log 就知道組態有沒有生效，
    # 不用去猜「我改的那個檔案有沒有被讀到」。
    print(f'config : {CONFIG_PATH}')
    print(f'db     : {DB_PATH}')
    print(f'週期   : {interval} s')

    sessions = []          # [(dev, client), ...]
    n_tags = 0
    for dev in devices:
        client = build_client(dev)
        ok = client.connect()
        tags = dev.get('tag', [])
        n_tags += len(tags)
        print(f'  device {dev["name"]:10s} {dev["protocol"]:12s} '
              f'connect={ok}  tags={len(tags)}')
        sessions.append((dev, client))
    print(f'共 {len(sessions)} 個設備、{n_tags} 個 tag   (Ctrl+C to stop)\n')

    conn = open_db(DB_PATH)

    prev_m = prev_w = None
    n_ok = n_bad = 0

    try:
        while True:
            # ─── 這一輪的起點：兩個時鐘「同時」取樣 ───
            now = time.monotonic()   # 增量式：只能量差，不受校時影響
            now_w = time.time()      # 絕對式：可以當時間戳

            # ─── 檢查 1：迴圈有沒有變慢 ───
            if prev_m is not None and now - prev_m > interval * 1.05:
                print(f'{time.strftime("%H:%M:%S")}  LOOP LATE: '
                      f'週期={now - prev_m:.3f}s')

            # ─── 檢查 2：兩個時鐘有沒有走不一樣快 ───
            if prev_m is not None:
                dm = now - prev_m
                dw = now_w - prev_w
                if abs(dm - dw) > 0.05:
                    print(f'{time.strftime("%H:%M:%S")}  CLOCK SKEW: '
                          f'mono={dm:.3f}s  wall={dw:.3f}s  差={dw - dm:+.3f}s')

            prev_m, prev_w = now, now_w

            next_t = now + interval   # 下次該醒的時刻，用 monotonic 算
            ts = now_w                # 這一輪所有 tag 共用同一個時間戳
            t0 = now

            # ─── 走訪所有設備的所有 tag ───
            rows = []
            for dev, client in sessions:
                dev_failed = False
                for tag in dev.get('tag', []):
                    value, quality, err = read_tag(client, dev, tag)
                    rows.append((ts, tag['name'], value, quality))

                    if err:
                        dev_failed = True
                        print(f'{time.strftime("%H:%M:%S")}  '
                              f'READ FAILED [{dev["name"]}/{tag["name"]}]: {err}')

                    n_ok += quality
                    n_bad += (1 - quality)

                # 一個設備最多重連一次，不是每個失敗的 tag 都重連
                if dev_failed and dev.get('reconnect_on_error', False):
                    reconnect(client, dev['name'])

            t1 = time.monotonic()

            # ─── 一輪寫一次，一次 commit ───
            conn.executemany(
                'INSERT INTO reading (ts, tag, value, quality) VALUES (?,?,?,?)',
                rows)
            conn.commit()

            t2 = time.monotonic()

            # ─── 檢查 3：工作本身有沒有變慢 ───
            if t2 - t0 > slow_threshold:
                print(f'{time.strftime("%H:%M:%S")}  SLOW  '
                      f'read={1000*(t1-t0):.0f}ms  write={1000*(t2-t1):.0f}ms')

            # ─── 心跳：每 60 輪印一行，證明它還活著 ───
            if n_ok + n_bad and (n_ok + n_bad) % (60 * len(rows)) == 0:
                print(f'{time.strftime("%H:%M:%S")}  ok={n_ok}  bad={n_bad}')

            # ─── 睡到下一輪 ───
            # 用「目標時刻 − 現在」來算，不是固定 sleep(interval)。
            # 固定 sleep 會讓週期隨著讀取耗時漂移：成功 1.03 s、失敗 1.30 s。
            time.sleep(max(0, next_t - time.monotonic()))

    except KeyboardInterrupt:
        print(f'\nstopped.  ok={n_ok}  bad={n_bad}')
    finally:
        for _, client in sessions:
            client.close()
        conn.close()


if __name__ == '__main__':
    main()
