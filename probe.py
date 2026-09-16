import os

HOST = os.environ.get('PLC_HOST', '127.0.0.1')
PORT = int(os.environ.get('PLC_PORT', '5020'))

from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient(HOST, port=PORT, timeout=3)
print('connect:', client.connect())

for base in (0, 100, 1024):
    r = client.read_holding_registers(address=base, count=8)
    if r.isError():
        print(f'holding {base:5d}~{base+7:<5d} : ERROR  {r}')
    else:
        print(f'holding {base:5d}~{base+7:<5d} : {r.registers}')

client.close()