# 一次性驗證。階段二的產物，之後不會再改
# 用途：確認 Modbus 通不通、位址對不對
import os
import time

from pymodbus.client import ModbusTcpClient

HOST = os.environ.get('PLC_HOST', '127.0.0.1')
PORT = int(os.environ.get('PLC_PORT', '5020'))

client = ModbusTcpClient(HOST, port=PORT, timeout=3)
print('connect:', client.connect())

for i in range(10):
    r = client.read_holding_registers(address=0, count=4)
    if r.isError():
        print('error:', r)
    else:
        #print(f"i={i}")
        print(time.strftime('%H:%M:%S'), r.registers)
    time.sleep(1)

client.close()