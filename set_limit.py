import os

HOST = os.environ.get('PLC_HOST', '127.0.0.1')
PORT = int(os.environ.get('PLC_PORT', '5020'))

import sys

from pymodbus.client import ModbusTcpClient

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10

client = ModbusTcpClient(HOST, port=PORT, timeout=3)
client.connect()
w = client.write_register(address=1, value=limit)
print('寫入', limit, ':', 'error' if w.isError() else 'ok')
print('讀回  :', client.read_holding_registers(address=2, count=1).registers[0])
client.close()