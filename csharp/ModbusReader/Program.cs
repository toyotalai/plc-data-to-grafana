using System.Net.Sockets;  //提供 TCP/IP 連線能力
using NModbus; //提供 Modbus 通訊函式庫，C# API 讀寫 Modbus 設備

const string HOST  = "127.0.0.1";
const int    PORT  = 5020;
const byte   SLAVE = 1;      // 對應 pymodbus 的預設 slave id
const ushort ADDR  = 0;      // 對應 %QW0
const ushort COUNT = 4;

using var client = new TcpClient();   //建連線；自動關閉
client.Connect(HOST, PORT);
Console.WriteLine($"connect: {client.Connected}");
//client.Connected 會回傳 true / false

var master = new ModbusFactory().CreateMaster(client);  
//建Modbus層，也就是說，現在這個 master 物件就能發送讀寫 Modbus 指令
master.Transport.ReadTimeout = 500;   // 對應 pymodbus 的 timeout=0.5

for (int i = 0; i < 10; i++)
{
    try 
    {
        ushort[] regs = master.ReadHoldingRegisters(SLAVE, ADDR, COUNT); //讀暫存器
        Console.WriteLine($"{DateTime.Now:HH:mm:ss} [{string.Join(", ", regs)}]");
    }
    catch (Exception e)
    {
        Console.WriteLine($"error: {e.Message}");
    }
    Thread.Sleep(1000);
}