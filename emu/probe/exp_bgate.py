# -*- coding: utf-8 -*-
"""V25 探针：主循环的门字节、它的写者、开门之后、以及两处新的建模缺口。

V24 把主循环解成「clr_flg(3,~0x200000) → 查门字节 0x9F58D08F → wai_flg →
无条件跳回」，并记下「门字节从来没人写」。V25 追到写者：

  * 静态搜 0x9F58D08F 的 32 位立即数，共 19 处引用；其中 0x12F180 是
    `stb r0(=1), @(r12)` —— 唯一已知写点，紧跟着同一函数里的
    `set_flg(3, 0x200000)`（0x12F1A8）。两件事配套。
  * 动态：把门字节手工置 1（模拟那段代码跑过），主循环立刻走「干活分支」，
    +102 个指令地址、调进显示区 0x12E6F8，但只跑一次就退回等待 ——
    下一道门是状态字 0x9F58D450。

顺带撞出两处建模缺口（本版修了一处）：
  * 任务 4 在 0x1A4F9A 把 ROM 0x25F534 拷到 0x68010000，实测写到 0x6801DE84，
    而内核 RAM 只建了 64KB（到 0x6800FFFF）—— 全部写入被静默吞掉。
    V25 把 ram-kernel 扩到 128KB。
  * 显示任务（入口 0x12DFAE）停在 0x12E1D2 的 `int #0x40`，服务号 -170，
    处理函数 0x41BEA 返回 -17（E_NOSPT）：这个内核**没有实现**它。

A 门字节引用全景 / B 写者代码段 / C 开门实验 / D 干活分支结构
E 内核 RAM 上界 / F 任务停点表 / G 未实现的服务 -170 / H 能报不能报
"""
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import brealos, bflag, bgate


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def steady():
    m = brealos.boot()
    brealos.sta_tsk(m, 2)
    m.raise_irq(0x3F)
    m.run(max_steps=200000, spin_guard=False)
    m.run(max_steps=800000, spin_guard=False)
    return m


# ---------------------------------------------------------------- A 引用
head("A 门字节 0x9F58D08F 的引用全景（静态搜 32 位立即数）")

m = create("d300-b")
m.load()
r = bgate.refs(m, bgate.GATE)
print("引用 %d 处：" % len(r))
for pc in r:
    print("   %08X" % pc)
print()
print("读点（主循环）0x120170-0x120184：")
print(m.disasm_at(0x120170, 10))
print("\n写点 0x12F17A-0x12F186（唯一已知 stb）：")
print(m.disasm_at(0x12F178, 6))

# ---------------------------------------------------------------- B 写者
head("B 写者所在代码段：0x12F158-0x12F1AA")

print(m.disasm_at(0x12F158, 32))
print()
print("这段函数读两个内存字节做判断：0x9F58D098 与 0x9F58D873，")
print("先写门字节=1，再 set_flg(3,0x200000)，最后 ret —— 是「有事要处理」的信号束。")

# ---------------------------------------------------------------- C 开门
head("C 开门实验：把门字节置 1（模拟 0x12F180），主循环走干活分支")

m = steady()
prev = bytes(m.cov.buf)
c0, n0 = m.cov.words(), len(m.flow.calls)
print("开门前：PC=%08X 覆盖=%d 调用=%d 门字节=%02X 状态字=%04X"
      % (m.cpu.pc, c0, n0, bgate.gate(m), bgate.status(m)))
bgate.open_gate(m)
m.run(max_steps=1000000, spin_guard=False)
cur = bytes(m.cov.buf)
new = [m.cov.base + (i << 1) for i in range(m.cov.n) if cur[i] and not prev[i]]
print("开门后 100 万步：覆盖 +%d（新地址 %d）调用 +%d PC=%08X 门字节=%02X"
      % (m.cov.words() - c0, len(new), len(m.flow.calls) - n0, m.cpu.pc, bgate.gate(m)))
print("新地址按 4K 页：", {hex(p << 12): n for p, n in sorted(Counter(a >> 12 for a in new).items())})
print("热地址执行过：0x120184=%s 0x12E6F8=%s 0x12E630=%s 0x12F180=%s"
      % (m.cov.visited(0x120184), m.cov.visited(0x12E6F8),
         m.cov.visited(0x12E630), m.cov.visited(0x12F180)))
print("本窗口服务：", dict(sorted(Counter(x[2] for x in m.flow.calls[n0:]).items())))
print("flag3=%08X flag6=%08X flag12=%08X"
      % (bflag.pattern(m, 3), bflag.pattern(m, 6), bflag.pattern(m, 12)))
print()
print("读法：干活分支里每个调用点只走一次（下一轮就退回等待）——")
print("因为分支自身还要看状态字 0x9F58D450 的位，那一位没人置。")

# ---------------------------------------------------------------- D 干活分支
head("D 干活分支 0x120184-0x120260 的结构")

print(m.disasm_at(0x120184, 40))

# ---------------------------------------------------------------- E 内核 RAM
head("E 新缺口：内核 RAM 上界（任务 4 的拷贝写到 0x6801DE84）")

m = brealos.boot()
for i in range(1, 29):
    brealos.sta_tsk(m, i)
hi, cnt = 0, 0
_orig = m.bus.wr


def _cap(a, sz, v, pc=None):
    global hi, cnt
    if 0x68000000 <= a < 0x68020000:
        cnt += 1
        hi = max(hi, a)
    return _orig(a, sz, v, pc)


m.bus.wr = _cap
for rnd in range(8):
    m.raise_irq(0x3F)
    m.run(max_steps=100000, spin_guard=False)
m.bus.wr = _orig
print("0x68000000-0x6801FFFF 写入 %d 次，最高地址 %08X" % (cnt, hi))
print("0x68010000 处现在的内容：%s" % m.fetch_bytes(0x68010000, 16).hex(" "))
print("ram-kernel 现在是 128KB（0x68000000-0x6801FFFF）；V25 之前是 64KB，")
print("0x68010000 以上全被静默吞掉 —— 拷进去的表读回来是 0。")
print("拷贝源 0x25F534、目的 0x68010000 的指令：")
print(m.disasm_at(0x1A4F94, 8))

# ---------------------------------------------------------------- F 停点表
head("F 全开 28 任务：各任务最后执行到的应用区地址")

m = brealos.boot()
for i in range(1, 29):
    brealos.sta_tsk(m, i)
c = m.cpu
last_app = {}
for rnd in range(8):
    m.raise_irq(0x3F)
    k = 0
    while k < 100000:
        m.step(1)
        k += 1
        pc = c.pc
        if 0x100000 <= pc < 0x200000:
            last_app[m.bus.rd(0x68000000, 4)] = pc
recs = {r["tcb"]: r for r in brealos.records(m, include_idle=True)}
print("覆盖 %d 调用 %d 入口执行过 %s"
      % (m.cov.words(), len(m.flow.calls),
         [t["idx"] for t in brealos.states(m) if t["ran"]]))
print("  任务  状态字      最后应用区 PC   入口")
for tcb, pc in sorted(last_app.items(), key=lambda kv: recs.get(kv[0], {}).get("id", 99)):
    r = recs.get(tcb, {})
    print("  %-4s  %08X  %08X  %08X"
          % (r.get("id", "?"), m.bus.rd(tcb + 0x08, 4), pc, r.get("entry", 0)))
print()
print("任务 4 停在 0x1A4FA4 的拷贝循环里；任务 5（显示）停在 0x12E1D2 的 int #0x40；")
print("任务 3 停在 0x12CEAA 的 wai_flg；任务 2 在主循环的 clr_flg 上。")

# ---------------------------------------------------------------- G 未实现服务
head("G 显示任务用的服务 -170：内核返回 E_NOSPT")

print("任务 5 停点上下文：")
print(m.disasm_at(0x12E1C0, 10))
mm = brealos.boot()
print("\n服务号解析：")
for svc in (-46, -170):
    t = brealos.service_handler(mm, svc)
    print("  svc %4d → %s" % (svc, hex(t) if t else None))
r = brealos.kernel_call(mm, -170, r4=0x9F58D0F0, r5=3, r6=0x200000, r7=2,
                        r13=0x64, max_steps=5000)
print("调用 -170（timeout=0x64）：err=%s steps=%d" % (r["err"], r["steps"]))
print("读法：处理函数 0x41BEA 的前导与 wai_flg 0x41B0A 几乎逐字相同，但带超时判断；")
print("实测返回 -17（E_NOSPT）—— 这个内核没实现它，固件只能拿错误码继续走。")

# ---------------------------------------------------------------- H 结论
head("能报 / 不能报")
print("能报：")
print("  * 门字节 0x9F58D08F 共 19 处引用，唯一写点是 0x12F180（stb 1），")
print("    同一函数里紧跟 set_flg(3,0x200000)（0x12F1A8）")
print("  * 开门（置 1）后主循环走干活分支：+102 地址、调进 0x12E6F8、置 flag6/12/3")
print("  * 干活分支只跑一次就退回等待：下一道门是状态字 0x9F58D450 的位")
print("  * 任务 4 把 ROM 0x25F534 拷到 0x68010000，写到 0x6801DE84 —— 内核 RAM 需 128KB")
print("  * 全开场景 5 个任务（2/3/4/5+空闲）各自的停点地址与状态字")
print("  * 显示任务用的服务 -170 未实现（返回 E_NOSPT）")
print("不能报：")
print("  * 0x12F0xx 那段函数（门字节写者）的进入条件 —— 它自己也在等两个字节")
print("  * 状态字 0x9F58D450 的位含义（只知道 bit4/bit11 参与分支）")
print("  * 0x6801DE84 那块表的用途（内容从 ROM 0x25F534 拷来，未解语义）")
print("  * 「相机跑起来了」（真实外设事件源仍未建模）")
print()
print("⚠️ 口径：bgate.open() 是替 0x12F180 那一笔写，不是真的执行了那段代码；")
print("   ram-kernel 128KB 的依据是实测最高写入 0x6801DE84，不是固件声明的尺寸。")
