# =============================================================================
# uploader.py — 把 SQLite 裡的資料上送到 MQTT broker
#
# 架構 C（串聯）：collect.py 寫 SQLite，這支程式讀出來往上送。
#   SQLite 本身就是斷網緩衝——不需要另外做記憶體佇列，
#   而且斷電也不會掉，因為資料早就落地了。
#
# 這支程式不碰 collect.py，也不改 reading 表。只讀，加上一張自己的進度表。
#
# 三個絕對不能弄錯的地方：
#   1. 先發送成功，才更新進度。順序反過來就會漏資料。
#   2. publish() 回傳成功 != broker 收到了。必須等 PUBACK。
#   3. 換資料庫檔案之前，要先把舊的排空。（v2 修正，見下）
#
# -----------------------------------------------------------------------------
# v2（2026-10-05）修正一個實測抓到的缺口
#
#   v1 偵測到 current.db 換指向之後，直接關掉舊連線開新的。
#   但這支程式每 upload_interval 秒才撈一次，collect 重啟那一刻距離上一次
#   撈最多 upload_interval 秒——那幾秒寫進舊檔案的資料還沒被撈走，
#   然後就跟著舊檔案被永遠留下了。
#
#   實測：upload_interval=5 的情況下漏了 9 筆。
#     舊資料庫 MAX(rowid)=16731，它自己的 upload_state.last_rowid=16722。
#
#   而且它不會報錯——跟它要修的那個 M7 問題是同一類的安靜漏法。
#
#   修法：切換之前先 drain()，把舊資料庫剩下的全部送完。
#         排不乾淨就印出檔名和剩餘筆數再切，不要安靜地丟掉。
# =============================================================================

import json
import os
import sqlite3
import time
import tomllib

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.environ.get('CONFIG_PATH', os.path.join(HERE, 'devices.toml'))

# 預設指向符號連結，不是某個特定的資料庫檔案。
# collect.service 每次啟動會把這個連結指向新產生的 readings_<時間戳>.db。
DB_PATH = os.environ.get('DB_PATH', '/var/lib/plc-gateway/current.db')

# drain 時最多跑幾批就放棄。避免舊檔案積壓極多時卡住新資料的上送。
MAX_DRAIN_BATCHES = 20


# ---------------------------------------------------------------------------
# 組態
# ---------------------------------------------------------------------------
def load_config(path):
    with open(path, 'rb') as f:
        return tomllib.load(f)


def build_tag_map(cfg):
    """從組態建立 tag -> device 的對照。

    為什麼需要這個：reading 表只有 tag 欄，沒有 device 欄。扁平的 tag 名
    在單機落地時夠用，要組出分層的 MQTT topic 就不夠了。

    不改資料表（那是跑過 14.8 小時的東西），改成在這裡查組態——
    devices.toml 本來就有完整的 device -> tag 結構。

    代價：組態改了沒重啟這支程式，對照表就是舊的。所以啟動時要印出來。
    """
    tag_map = {}
    for dev in cfg.get('device', []):
        for tag in dev.get('tag', []):
            tag_map[tag['name']] = dev['name']
    return tag_map


# ---------------------------------------------------------------------------
# 資料庫
# ---------------------------------------------------------------------------
def open_db(path):
    """開資料庫並確保進度表存在。

    timeout=5.0：collect.py 每秒在寫，SQLite 寫入時會鎖整個資料庫。
    不設的話讀取可能直接撞上 SQLITE_BUSY 而丟例外。
    先不改 WAL 模式——那會多兩個檔案，而且在 SD 卡上的行為還沒量過。
    """
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS upload_state (
            id          INTEGER PRIMARY KEY CHECK (id = 1),  -- 結構上只能有一列
            last_rowid  INTEGER NOT NULL,
            updated_at  REAL    NOT NULL
        )''')
    conn.execute(
        'INSERT OR IGNORE INTO upload_state (id, last_rowid, updated_at) '
        'VALUES (1, 0, 0.0)')
    conn.commit()
    return conn


def read_progress(conn):
    row = conn.execute('SELECT last_rowid FROM upload_state WHERE id = 1').fetchone()
    return row[0] if row else 0


def fetch_batch(conn, last_rowid, limit):
    """撈出還沒送的列。rowid 是 SQLite 的隱藏主鍵，插入順序即遞增順序。

    為什麼不用 ts 當進度：一輪三個 tag 共用同一個時間戳（collect.py 刻意
    這樣設計，讓同輪的值可以對齊），所以 ts 會重複，拿來當「送到哪了」
    會在邊界上漏掉同輪的其他 tag。
    """
    return conn.execute(
        'SELECT rowid, ts, tag, value, quality FROM reading '
        'WHERE rowid > ? ORDER BY rowid LIMIT ?',
        (last_rowid, limit)).fetchall()


def count_tail(conn):
    """這個資料庫還有幾列沒送。"""
    try:
        return conn.execute(
            'SELECT COUNT(*) FROM reading WHERE rowid > ?',
            (read_progress(conn),)).fetchone()[0]
    except sqlite3.Error:
        return -1


def commit_progress(conn, rowid):
    conn.execute(
        'UPDATE upload_state SET last_rowid = ?, updated_at = ? WHERE id = 1',
        (rowid, time.time()))
    conn.commit()


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def make_topic(prefix, site, tag_map, tag):
    """plcgw/<site>/<device>/<tag>

    對照不到的 tag 歸到 unknown，不丟棄也不猜——
    跟 quality=0 同樣的原則：不知道就誠實標成不知道。
    """
    return f'{prefix}/{site}/{tag_map.get(tag, "unknown")}/{tag}'


def make_payload(ts, value, quality):
    """{"ts": 1759381234.567, "v": 30.3, "q": 1}

    quality 必須送。只送數值的話，「真的讀到 0」和「讀不到，占位 0」
    在下游就永遠分不開了——而這個專題花了三個階段在證明這兩件事要分開。

    ts 也必須送，而且是採集當下的時間。靠「broker 收到的時間」當時間戳
    的話，斷網十分鐘之後補送，那十分鐘的資料會全部被標成補送當下——
    整段歷史壓扁成一個瞬間，而且看起來完全正常。
    """
    return json.dumps({'ts': ts, 'v': value, 'q': quality},
                      separators=(',', ':')).encode()


def build_client(mq, site):
    """建立 MQTT client，並設定遺言（LWT）。

    LWT：連線時先告訴 broker「如果我沒說再見就斷了，請代我發這一則」。

    為什麼這件事對這個專題特別重要——
    輪詢的時候「沒有資料」分得出是「讀不到」還是「正常但沒變化」，
    因為你每秒都去問。改成事件驅動之後「安靜」就有兩種意思了。
    LWT 等於把輪詢的那個節拍，用另一種形式加回來。
    """
    client_id = mq.get('client_id', f'uploader-{site}')

    # paho-mqtt 2.x 的建構子多了 CallbackAPIVersion；1.x 沒有。
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=client_id)
    except AttributeError:            # paho-mqtt 1.x
        client = mqtt.Client(client_id=client_id)

    status_topic = f"{mq.get('topic_prefix', 'plcgw')}/{site}/status"
    client.will_set(status_topic,
                    json.dumps({'online': False}).encode(),
                    qos=1, retain=True)

    # 補送時會一次塞很多筆，預設 20 的 inflight 上限會變成瓶頸
    client.max_inflight_messages_set(100)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client, status_topic


def publish_batch(client, rows, prefix, site, tag_map, qos, wait_timeout):
    """發送一批。回傳 (最後成功的 rowid, 成功筆數, 錯誤訊息或 None)。

    這個函式絕不丟例外——上送程式不能因為單次失敗而死掉，
    死掉就停止補送了。

    ⚠️ 關鍵：publish() 回傳成功只代表「排進本地佇列」，不代表 broker 收到。
       斷網的時候它照樣回成功。只看回傳值就更新進度的話，那批資料
       會永遠不再被撈出來——這就是「寫入成功 != 命令被接受」換了一層協定。

       所以先全部送出（讓它們在線上並行，不要一筆等一筆），
       再逐一等 QoS 1 的 PUBACK 回來，全部確認才算成功。
    """
    infos = []
    try:
        for rowid, ts, tag, value, quality in rows:
            topic = make_topic(prefix, site, tag_map, tag)
            info = client.publish(topic, make_payload(ts, value, quality), qos=qos)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                return None, 0, f'publish rc={info.rc}（broker 連不上？）'
            infos.append((rowid, info))
    except Exception as exc:          # noqa: BLE001 ← 刻意攔截全部
        return None, 0, f'{type(exc).__name__}: {exc}'

    # ---- 等 PUBACK ----
    last_ok = None
    n_ok = 0
    for rowid, info in infos:
        try:
            info.wait_for_publish(timeout=wait_timeout)
        except Exception as exc:      # noqa: BLE001
            return last_ok, n_ok, f'wait_for_publish: {type(exc).__name__}: {exc}'
        if not info.is_published():
            return last_ok, n_ok, f'rowid {rowid} 逾時未確認（{wait_timeout}s）'
        last_ok = rowid
        n_ok += 1

    return last_ok, n_ok, None


def send_pending(conn, client, cfg_tuple, batch_size):
    """撈一批並送出，成功才更新進度。回傳 (撈到幾列, 送成功幾列, 錯誤或 None)。

    主迴圈和 drain() 共用這一段，確保「先發成功才更新進度」只有一份實作。
    """
    prefix, site, tag_map, qos, wait_timeout = cfg_tuple

    last_rowid = read_progress(conn)
    rows = fetch_batch(conn, last_rowid, batch_size)
    if not rows:
        return 0, 0, None

    ok_rowid, n_ok, err = publish_batch(
        client, rows, prefix, site, tag_map, qos, wait_timeout)

    if ok_rowid is not None:
        commit_progress(conn, ok_rowid)   # ← 確認送達才更新

    return len(rows), n_ok, err


def drain(conn, client, cfg_tuple, batch_size):
    """把這個資料庫剩下的全部送完。回傳 (送出筆數, 錯誤或 None)。

    切換資料庫檔案之前一定要跑這個。

    v1 沒有這一步，結果是：上一輪撈完之後、collect 重啟之前寫進舊檔案的
    那幾筆，會跟著舊檔案被永遠留下。實測漏了 9 筆，而且完全不報錯。

    設上限的理由：broker 同時也斷著的話，這裡會一直失敗。
    與其卡在舊檔案讓新資料也送不出去，不如放棄、印出剩餘筆數、往前走。
    """
    total = 0
    for _ in range(MAX_DRAIN_BATCHES):
        n_rows, n_ok, err = send_pending(conn, client, cfg_tuple, batch_size)
        total += n_ok
        if err:
            return total, err
        if n_rows == 0:
            return total, None
    return total, f'超過 {MAX_DRAIN_BATCHES} 批仍未清空'


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------
def main():
    cfg = load_config(CONFIG_PATH)
    gw = cfg.get('gateway', {})
    mq = cfg.get('mqtt', {})

    site = gw.get('site')
    if not site:
        raise SystemExit(f'組態的 [gateway] 缺少 site：{CONFIG_PATH}')

    host = mq.get('host', 'localhost')
    port = mq.get('port', 1883)
    qos = mq.get('qos', 1)
    prefix = mq.get('topic_prefix', 'plcgw')
    interval = mq.get('upload_interval', 5.0)
    batch_size = mq.get('batch_size', 500)
    wait_timeout = mq.get('publish_timeout', 10.0)

    tag_map = build_tag_map(cfg)
    cfg_tuple = (prefix, site, tag_map, qos, wait_timeout)

    # 啟動時把組態印出來。看 log 就知道它認為誰是誰，不用猜。
    print(f'config : {CONFIG_PATH}')
    print(f'db     : {DB_PATH}')
    print(f'site   : {site}')
    print(f'broker : {host}:{port}  qos={qos}')
    print(f'週期   : {interval} s，每批最多 {batch_size} 列')
    print('tag -> device 對照：')
    for tag, dev in sorted(tag_map.items()):
        print(f'  {tag:16s} -> {dev:10s}  topic: {prefix}/{site}/{dev}/{tag}')
    print()

    client, status_topic = build_client(mq, site)

    # loop_start 起一條背景執行緒處理網路 I/O。
    # 沒有它，wait_for_publish() 會永遠等不到 PUBACK。
    client.loop_start()

    try:
        client.connect(host, port, keepalive=30)
        client.publish(status_topic, json.dumps({'online': True}).encode(),
                       qos=1, retain=True)
    except Exception as exc:          # noqa: BLE001
        print(f'{time.strftime("%H:%M:%S")}  連不上 broker：{exc!r}（會持續重試）')

    conn = open_db(DB_PATH)
    current_real = os.path.realpath(DB_PATH)
    print(f'目前的資料庫實體檔案：{current_real}\n')

    n_sent = 0
    n_fail_rounds = 0
    last_report = time.monotonic()

    try:
        while True:
            t0 = time.monotonic()
            stamp = time.strftime('%H:%M:%S')

            # ─── M7：連結換人了嗎 ───
            # collect.service 重啟會把 current.db 指向新檔案。
            # 已經開著的連線不會跟著換——它還抓著舊檔案，然後很安靜地
            # 「沒有新資料要送」。這是會卡住而且不報錯的那種坑。
            real = os.path.realpath(DB_PATH)
            if real != current_real:
                old = os.path.basename(current_real)
                new = os.path.basename(real)

                # ⚠️ v2：切換前先排空舊的。
                # 少了這一步，上一輪撈完到 collect 重啟之間寫進去的那幾筆
                # 會跟著舊檔案被永遠留下（實測 9 筆，而且不報錯）。
                n_drained, drain_err = drain(conn, client, cfg_tuple, batch_size)
                n_sent += n_drained
                tail = count_tail(conn)

                if tail > 0:
                    print(f'{stamp}  ⚠️ SWITCH WITH TAIL: {old} '
                          f'還有 {tail} 筆未送（{drain_err}）')
                print(f'{stamp}  DB SWITCHED: {old} -> {new}'
                      f'（切換前補送 {n_drained} 筆）')

                conn.close()
                conn = open_db(DB_PATH)
                current_real = real

            # ─── 正常的一輪 ───
            try:
                n_rows, n_ok, err = send_pending(conn, client, cfg_tuple, batch_size)
                n_sent += n_ok
                if err:
                    n_fail_rounds += 1
                    print(f'{stamp}  UPLOAD FAILED: {err}'
                          f'（這批成功 {n_ok}/{n_rows}，進度停在 {read_progress(conn)}）')
            except sqlite3.Error as exc:
                print(f'{stamp}  DB ERROR: {exc!r}')

            # ─── 每分鐘一行心跳，順便看積壓 ───
            if time.monotonic() - last_report >= 60:
                print(f'{stamp}  sent={n_sent}  '
                      f'backlog={count_tail(conn)}  fail_rounds={n_fail_rounds}')
                last_report = time.monotonic()

            # ─── 睡到下一輪（對齊時鐘，跟 collect.py 同一個寫法）───
            time.sleep(max(0, t0 + interval - time.monotonic()))

    except KeyboardInterrupt:
        print(f'\nstopped.  sent={n_sent}  fail_rounds={n_fail_rounds}  '
              f'backlog={count_tail(conn)}')
    finally:
        try:
            client.publish(status_topic, json.dumps({'online': False}).encode(),
                           qos=1, retain=True).wait_for_publish(timeout=2)
        except Exception:             # noqa: BLE001
            pass
        client.loop_stop()
        client.disconnect()
        conn.close()


if __name__ == '__main__':
    main()
