import os

HOST = os.environ.get('PLC_HOST', '127.0.0.1')
PORT = int(os.environ.get('PLC_PORT', '5020'))

import time

from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient(HOST, port=PORT, timeout=3)
print('connect:', client.connect())

for addr in (1, 100, 1024):
    w = client.write_register(address=addr, value=4321)
    if w.isError():
        print(f'addr {addr:5d} : 寫入被拒  {w}')
        continue
    a = client.read_holding_registers(address=addr, count=1).registers[0]
    time.sleep(2)
    b = client.read_holding_registers(address=addr, count=1).registers[0]
    print(f'addr {addr:5d} : 寫入 ok    立刻讀={a:5d}   2 秒後={b:5d}')

client.close()