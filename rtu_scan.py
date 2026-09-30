#!/usr/bin/env python3
"""
rtu_scan.py — Modbus RTU 匯流排掃描工具

用途：接一台不認識的設備上線時，找出它的鮑率、同位設定和從站位址。

為什麼需要它：
    RS-485 上「沒有回應」至少對應四種病因 —— A/B 接反、從站位址錯、
    鮑率不對、從站沒電 —— 而程式完全分辨不出是哪一種。
    症狀分不出來，就只能照成本由低到高逐一排除：

        1. 三用電表量 V+ 對 V−        排除沒電        十秒
        2. 執行這支程式                排除鮑率、位址    兩分鐘
        3. 對調 A/B 再跑一次           排除極性反接     一分鐘
        4. 以上都不行                  線斷了或從站壞了

參數依據（260929 階段六b 實測）：
    正常單次交易 31 ms  ->  timeout 預設 0.15 s（五倍餘裕）
    retries 0           ->  失敗一次只花 timeout，不是 timeout × 4

    這兩個數字決定這支工具能不能用：
        timeout 1.0 + retries 3  ->  741 次嘗試要 49 分鐘   不可行
        timeout 0.15 + retries 0 ->  741 次嘗試約 2 分鐘    可行
    沒量過那 31 ms，就不敢把 timeout 設這麼短。

只用功能碼 0x04 探測就夠：
    任何活著的從站，收到不支援的功能碼會回一個例外碼 1 的錯誤回應，
    不會沉默。所以「收到任何回應」＝「那裡有東西」，包括拒絕。

用法：
    python rtu_scan.py
    python rtu_scan.py --addrs 1-32 --bauds 9600,19200
    python rtu_scan.py --parity N,E,O          # 同位也不確定時
"""

import argparse
import itertools
import logging
import sys
import time

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# pymodbus 每次逾時都會印一行。掃描會產生上百次逾時，全印出來看不到結果。
logging.getLogger('pymodbus').setLevel(logging.CRITICAL)

PROBE_FUNCTION = 4      # 0x04 Read Input Registers
PROBE_ADDRESS = 1       # 探測用的暫存器位址。存不存在都無所謂 —— 見上面說明。


def parse_range(text):
    """把 '1-32' 或 '1,5,9' 或 '1-16,20' 解析成位址串列。"""
    out = []
    for part in text.split(','):
        part = part.strip()
        if '-' in part:
            lo, hi = part.split('-')
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def probe(client, addr):
    """探測一個位址。回傳 (有沒有回應, 說明文字)。"""
    try:
        rr = client.read_input_registers(
            PROBE_ADDRESS, count=1, device_id=addr)
    except ModbusException:
        return False, '沉默'
    except Exception as exc:          # noqa: BLE001
        return False, f'{type(exc).__name__}: {exc}'

    if rr.isError():
        # 收到格式正確的拒絕 —— 裝置在那裡，只是不接受這個暫存器位址或功能碼。
        return True, f'有回應但拒絕：{rr}'
    return True, f'讀到 {rr.registers}'


def main():
    p = argparse.ArgumentParser(
        description='掃描 Modbus RTU 匯流排，找出鮑率、同位與從站位址')
    p.add_argument('--port', default='/dev/ttyUSB0')
    p.add_argument('--bauds', default='9600,14400,19200',
                   help='要掃的鮑率，逗號分隔（預設 9600,14400,19200）')
    p.add_argument('--parity', default='N',
                   help='要掃的同位，逗號分隔，可填 N,E,O（預設只掃 N）')
    p.add_argument('--addrs', default='1-247',
                   help='要掃的位址範圍（預設 1-247）')
    p.add_argument('--timeout', type=float, default=0.15,
                   help='單次逾時秒數（預設 0.15，依據正常回應 31 ms）')
    p.add_argument('--retries', type=int, default=0)
    args = p.parse_args()

    bauds = [int(b) for b in args.bauds.split(',')]
    parities = [x.strip().upper() for x in args.parity.split(',')]
    addrs = parse_range(args.addrs)

    combos = len(bauds) * len(parities) * len(addrs)
    estimate = combos * args.timeout * (1 + args.retries)

    print(f'序列埠   {args.port}')
    print(f'鮑率     {bauds}')
    print(f'同位     {parities}')
    print(f'位址     {addrs[0]}–{addrs[-1]}（共 {len(addrs)} 個）')
    print(f'逾時     {args.timeout} s，retries {args.retries}')
    print(f'組合數   {combos}')
    print(f'預估耗時 約 {estimate:.0f} 秒 = {estimate/60:.1f} 分鐘')
    print(f'（掃描時間 = 組合數 × 逾時，可以事先算出來）\n')

    found = []
    t0 = time.monotonic()

    for baud, parity in itertools.product(bauds, parities):
        stopbits = 1 if parity != 'N' else 1
        client = ModbusSerialClient(
            port=args.port, baudrate=baud, bytesize=8,
            parity=parity, stopbits=stopbits,
            timeout=args.timeout, retries=args.retries)

        if not client.connect():
            print(f'!! 無法開啟 {args.port}', file=sys.stderr)
            print('   檢查：模組有沒有插好、帳號在不在 dialout 群組、'
                  '有沒有別的程式佔用', file=sys.stderr)
            return 2

        for addr in addrs:
            alive, detail = probe(client, addr)
            if alive:
                print(f'  ★ 找到  baud={baud:<6} parity={parity} '
                      f'addr={addr:<4} {detail}')
                found.append((baud, parity, addr, detail))

        client.close()
        print(f'-- baud={baud} parity={parity} 掃完，'
              f'累計 {time.monotonic() - t0:.1f} s')

    # ---- 摘要 ----
    elapsed = time.monotonic() - t0
    print(f'\n掃描完成，耗時 {elapsed:.1f} s，'
          f'平均每個位址 {elapsed / combos * 1000:.0f} ms')

    if not found:
        print('\n沒有找到任何從站。接下來依序排除：')
        print('  1. 三用電表量 V+ 對 V− 有沒有電壓')
        print('  2. 對調 A 和 B 兩條線，重跑一次')
        print('  3. 加掃同位：--parity N,E,O')
        print('  4. 都不行 -> 線斷了，或從站壞了')
        return 1

    print(f'\n找到 {len(found)} 個從站：')
    for baud, parity, addr, detail in found:
        print(f'  baud={baud}  parity={parity}  addr={addr}   {detail}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
