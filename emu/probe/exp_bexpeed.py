# -*- coding: utf-8 -*-
"""V27 探针：B↔EXPEED 位串行总线第一次被解成字节。

老线定性过「位串行、不是显存」，但没人把位流还原成命令。这一版做了：

  * 协议常量实测：CS = 0x70000002 bit2、CLK = 0x7000000A bit1、
    DATA = 0x7000000A bit2，命令字节 = 寄存器号 | 读标志 0x80，LSB 先；
  * 抓包解码：整场运行（boot + 开机装配 + 开门 + 100 万步）只有 **2 帧** ——
      #0  8 位  0x86            → 读寄存器 6
      #1  40 位 46 00 36 00 F6  → 读周期里的时钟相位（主机驱动的位，不是器件返回）
    另有 8 次 16 位写（0x402FC–0x40350）= 端口方向/配置，不是数据。
  * 结论：固件整场只向显示控制器**读了一个寄存器**。V26 说「总线几乎不动」，
    这一版把它精确到「只读了一次寄存器 6」。

A 协议常量与端口配置 / B 抓包与解码 / C 结论 / D 能报不能报
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import brealos, bgate, bexpeed


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------- A 协议
head("A 协议常量与端口配置")

print("CS   = 0x%08X bit2 (0x%02X)" % (bexpeed.CS_ADDR, bexpeed.CS_BIT))
print("CLK  = 0x%08X bit1 (0x%02X)" % (bexpeed.BUS_ADDR, bexpeed.CLK_BIT))
print("DATA = 0x%08X bit2 (0x%02X)" % (bexpeed.BUS_ADDR, bexpeed.DATA_BIT))
print("命令字节 = 寄存器号 | 0x80（读），LSB 先")
print()
print("端口配置写（0x402FC–0x40350，8 次 16 位写）：")
m0 = create("d300-b")
m0.load()
for ln in m0.disasm_at(0x402F0, 18).splitlines():
    print("   " + ln)

# ---------------------------------------------------------------- B 抓包
head("B 抓包：整场运行 → 帧 → 位 → 字节")

m = create("d300-b")
tap = bexpeed.ExpeedTap().attach(m)
m.load()
m.reset()
m.enable_cov()
m.attach_flow()
n = 0
while not m.cpu.halted and n < 191520:
    m.step(1)
    n += 1
brealos.sta_tsk(m, 2)
m.raise_irq(0x3F)
m.run(max_steps=200000, spin_guard=False)
m.run(max_steps=800000, spin_guard=False)
bgate.open_gate(m)
m.run(max_steps=1000000, spin_guard=False)
tap.detach()

s = tap.summary()
print("boot %d 步；装配 + 开门 + 100 万步后：" % n)
print("  端口配置写 %d 次，位串行帧 %d 个，位数 %s"
      % (s["cfg_writes"], s["frames"], s["bits"]))
print()
for c in s["cmds"]:
    if c["kind"] == "data":
        print("  #%d 数据相位：%s" % (c["idx"], c["payload"].hex(" ")))
    else:
        print("  #%d %-5s 寄存器 0x%02X  负载 %s"
              % (c["idx"], c["kind"], c["reg"], c["payload"].hex(" ") or "（无）"))
print()
print("原始位（前 16 位）：", tap.frames[0][:16] if tap.frames else None)
print("同一帧解成字节：", [hex(b) for b in bexpeed.decode_bits(tap.frames[0])] if tap.frames else None)

# ---------------------------------------------------------------- C 结论
head("C 结论：整场只向显示控制器读了一个寄存器")

print("2 帧、8 次配置写 —— 这就是固件与 EXPEED 之间的全部往来。")
print("对照 V26 的数字：位串行写 238 次 ≈ 119 个时钟位，一帧 640×480 要 307,200 位。")
print()
print("能报：")
print("  * 位串行协议被解成字节：CS/CLK/DATA 三根线 + LSB 先 + 读标志 0x80")
print("  * 整场只有 2 帧：读寄存器 6（+5 字节时钟相位）")
print("  * 另有 8 次 16 位端口配置写（0x402FC–0x40350）")
print("不能报：")
print("  * 寄存器 6 是什么（读回来的值我们没建模，总线读回 0）")
print("  * 写命令的完整格式（整场一次写都没发）")
print("  * 「读标志 = bit7」只在老线上反证过，1.11 上未单独复核")
print()
print("⚠️ 口径：帧按 CS 分；读命令后面那段时钟相位不是新命令（本探针标成 data）。")
