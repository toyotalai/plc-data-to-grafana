#!/usr/bin/env python3
"""localhost 對照組用的 Modbus TCP 伺服器（只監聽 127.0.0.1，暫存器值固定為 0，不模擬計數器）"""
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import StartTcpServer

try:
    from pymodbus.datastore import ModbusDeviceContext as SlaveCtx
except ImportError:
    from pymodbus.datastore import ModbusSlaveContext as SlaveCtx

HOST, PORT = "127.0.0.1", 5020

store = SlaveCtx(hr=ModbusSequentialDataBlock(1, [0] * 16))
try:
    context = ModbusServerContext(devices=store, single=True)
except TypeError:
    context = ModbusServerContext(slaves=store, single=True)

print(f"sim listening on {HOST}:{PORT}", flush=True)
StartTcpServer(context=context, address=(HOST, PORT))
