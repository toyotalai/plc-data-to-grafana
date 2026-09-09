import sys

from pymodbus.client import ModbusTcpClient

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10

client = ModbusTcpClient('127.0.0.1', port=5020, timeout=0.5)
client.connect()
w = client.write_register(address=1, value=limit)
print('寫入', limit, ':', 'error' if w.isError() else 'ok')
print('讀回  :', client.read_holding_registers(address=2, count=1).registers[0])
client.close()