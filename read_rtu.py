#!/usr/bin/env python3
"""
read_rtu.py — 讀取 XY-MD02 溫溼度（Modbus RTU over RS-485）

角色與 read_modbus.py（Modbus TCP 版）對應：診斷用的讀取工具，不寫資料庫。

參數依據來自 260929 階段六b 建置紀錄的實測：
  - 正常單次交易約 31 ms          -> timeout 預設 0.3 s（十倍餘裕）
  - pymodbus 預設 retries=3，失敗要 4.005 s
                                  -> retries 預設 0，避免故障時拖垮採集節奏
  - 從站沒回應時 pymodbus「丟例外」而非回傳物件
                                  -> 必須 try/except，否則程式直接中斷
  - 溫度是有號 16 位元             -> 低於 0 °C 要做二補數換算

用法：
  python read_rtu.py                      # 讀一次
  python read_rtu.py -n 30 --interval 1   # 每秒讀一次，讀 30 次
  python read_rtu.py --addr 2 --baud 19200
"""

import argparse
import sys
import time

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

# XY-MD02 輸入暫存器（功能碼 0x04）
TEMP_REG = 1  # 0x0001 溫度，值 / 10
HUMI_REG = 2  # 0x0002 濕度，值 / 10

# 失敗的三種類別。對應 260929 紀錄第七之二節的症狀對照表。
KIND_PORT = "port"                # 裝置節點打不開：沒插、權限不足、被佔用
KIND_NO_RESPONSE = "no_response"  # 從站沒開口：A/B 反接、位址錯、鮑率錯、沒電
KIND_REFUSED = "refused"          # 從站有開口但拒絕：暫存器位址或功能碼不對


def to_signed(value: int) -> int:
    """16 位元無號轉有號（二補數）。低於 0 °C 時 XY-MD02 會回傳補數。"""
    return value - 65536 if value > 32767 else value


def read_once(client: ModbusSerialClient, addr: int) -> dict:
    """讀一次。回傳 dict，一定包含 ok / kind / elapsed。

    這個函式不丟例外——所有失敗都轉成回傳值，呼叫端才不會被中斷。
    """
    t0 = time.monotonic()
    try:
        rr = client.read_input_registers(TEMP_REG, count=2, device_id=addr)
    except ModbusException as exc:
        # 從站完全沒回應。程式看到的症狀一樣，但病因至少有四種，
        # 這裡分辨不出來——要靠 rtu_scan.py 或三用電表排除。
        return {
            "ok": False,
            "kind": KIND_NO_RESPONSE,
            "detail": f"{type(exc).__name__}: {exc}",
            "elapsed": time.monotonic() - t0,
        }

    elapsed = time.monotonic() - t0

    if rr.isError():
        # 收到的是格式正確的錯誤回應（ExceptionResponse）。
        # 這代表整條物理鏈路是通的，錯的是應用層參數。
        return {
            "ok": False,
            "kind": KIND_REFUSED,
            "detail": str(rr),
            "elapsed": elapsed,
        }

    raw_t, raw_h = rr.registers
    return {
        "ok": True,
        "kind": "ok",
        "temp": to_signed(raw_t) / 10.0,
        "humi": raw_h / 10.0,
        "raw": [raw_t, raw_h],
        "elapsed": elapsed,
    }


def build_client(args) -> ModbusSerialClient:
    return ModbusSerialClient(
        port=args.port,
        baudrate=args.baud,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=args.timeout,
        retries=args.retries,
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="讀取 XY-MD02 溫溼度（Modbus RTU over RS-485）"
    )
    p.add_argument("--port", default="/dev/ttyUSB0", help="序列埠（預設 /dev/ttyUSB0）")
    p.add_argument("--baud", type=int, default=9600, help="鮑率（預設 9600）")
    p.add_argument("--addr", type=int, default=1, help="從站位址（預設 1）")
    p.add_argument("--timeout", type=float, default=0.3,
                   help="單次逾時秒數（預設 0.3，依據正常回應 31 ms）")
    p.add_argument("--retries", type=int, default=0,
                   help="函式庫重試次數（預設 0，避免失敗耗時 timeout×(1+retries)）")
    p.add_argument("-n", "--count", type=int, default=1, help="讀幾次（預設 1）")
    p.add_argument("--interval", type=float, default=1.0, help="每次間隔秒數（預設 1）")
    args = p.parse_args()

    client = build_client(args)
    if not client.connect():
        print(f"失敗 [{KIND_PORT}]  無法開啟 {args.port}", file=sys.stderr)
        print("  檢查：模組有沒有插好、帳號在不在 dialout 群組、"
              "有沒有別的程式佔用這個裝置", file=sys.stderr)
        return 2

    fails = 0
    try:
        for i in range(args.count):
            r = read_once(client, args.addr)
            stamp = time.strftime("%H:%M:%S")
            ms = r["elapsed"] * 1000

            if r["ok"]:
                print(f"{stamp}  {r['temp']:6.1f} C  {r['humi']:6.1f} %  "
                      f"({ms:5.0f} ms)  raw={r['raw']}")
            else:
                fails += 1
                print(f"{stamp}  失敗 [{r['kind']}]  ({ms:5.0f} ms)  {r['detail']}")

            if i < args.count - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n中斷")
    finally:
        client.close()

    if args.count > 1:
        print(f"\n共嘗試 {args.count} 次，失敗 {fails} 次")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
