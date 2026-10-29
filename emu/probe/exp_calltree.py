# -*- coding: utf-8 -*-
"""exp_calltree.py —— A 段启动的完整调用树

做法：单步执行到停机，用解码表判 call / ret，维护调用栈，
      统计每个函数的 [首次 PC, 指令数, 子调用数, 写内存次数, int 次数]。

输出三块：
1. 调用树（缩进，标出指令数/写入数/int 数）
2. 按指令数排序的函数榜
3. 谁写过 0x3FBB0 / 0x45980 区 / 任务表 0x54C10（若 A 段有）
"""
import sys, os, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

import fr_emu
from frbox.machine import create

TABLE = fr_emu.TABLE


def decode_mn(m, pc):
    ins = TABLE.decode(m.bus.rom, pc - m.bus.rom_base)
    if ins is None:
        return None, 2
    mn, _, vals, sz, w = ins
    return mn, max(2, sz)


def main():
    m = create("d300-a")
    m.load()
    c = m.cpu

    nodes = {}                 # pc -> dict
    stack = []                 # [(entry_pc, from_pc)]
    order = []
    MAX = 200000
    int_seen = []

    # 记录全局 int
    eit_cnt = [0]
    orig_eit = fr_emu.Fr.eit

    def eit_counted(self, vec, level=None):
        eit_cnt[0] += 1
        int_seen.append((self.pc, vec))
        return orig_eit(self, vec, level)

    fr_emu.Fr.eit = eit_counted

    # 先跑到入口
    m.run(max_steps=2500)
    print("进入 0xFE426 前 步数=%d PC=0x%08X" % (m.step_count, c.pc))
    print()

    def touch(pc):
        n = nodes.get(pc)
        if n is None:
            n = dict(pc=pc, insns=0, calls=0, writes=0, ints=0, first_seen=True)
            nodes[pc] = n
        return n

    guard = 0
    while m.step_count < MAX:
        pc = c.pc
        if pc < 0x1000:
            print("跳飞：步 %d PC=0x%08X" % (m.step_count, pc))
            break
        mn, sz = decode_mn(m, pc)
        cur = nodes.get(pc)
        if cur is None and stack:
            cur = touch(stack[-1][0])
        if cur is not None:
            cur["insns"] += 1
        # 记录写内存（用 bus.all_writes 差量太重，这里靠 pc 归属，改成事后统计）

        if mn in ("call",) or (mn and mn.startswith("call")):
            ins = TABLE.decode(m.bus.rom, pc - m.bus.rom_base)
            # 目标 = 执行后 PC（延迟分支）
            c.step()
            m.step_count += 1
            if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
                for d in m.devices:
                    d.tick(m.cycles_per_tick)
            tgt = c.pc
            parent = stack[-1][0] if stack else None
            if parent is not None:
                nodes[parent]["calls"] += 1
            node = nodes.get(tgt)
            if node is None:
                node = dict(pc=tgt, insns=0, calls=0, writes=0, ints=0, first_seen=True)
                nodes[tgt] = node
            stack.append((tgt, pc))
            order.append(("call", pc, tgt))
            continue

        if mn in ("ret", "ret:d") or (mn and mn.startswith("ret")):
            if stack:
                stack.pop()
            c.step()
            m.step_count += 1
            if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
                for d in m.devices:
                    d.tick(m.cycles_per_tick)
            continue

        # 普通指令：统计写操作
        wb = len(m.bus.all_writes) if hasattr(m.bus, "all_writes") else 0
        c.step()
        m.step_count += 1
        if hasattr(m.bus, "all_writes"):
            wa = len(m.bus.all_writes)
            if wa > wb:
                node = nodes.get(stack[-1][0] if stack else pc)
                if node is not None:
                    node["writes"] += wa - wb
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    fr_emu.Fr.eit = orig_eit

    print("停机/结束：步数=%d PC=0x%08X int 总计=%d" % (m.step_count, c.pc, eit_cnt[0]))
    print("见到函数 %d 个" % len(nodes))
    print()

    # 调用树：从 0xFE426 起按 order 重建
    print("== 调用序列（首次出现顺序）==")
    seen = set()
    depth = 0
    for kind, f, t in order:
        if t in seen:
            continue
        seen.add(t)
        n = nodes[t]
        print("  0x%08X → 0x%08X  函数 0x%08X  %d 条指令  %d 次写" %
              (f, t, t, n["insns"], n["writes"]))
    print()

    print("== 按指令数排序（前 25）==")
    top = sorted(nodes.values(), key=lambda x: -x["insns"])[:25]
    print("%-11s %7s %6s %7s" % ("函数", "指令", "子调用", "写内存"))
    for n in top:
        print("0x%08X %7d %6d %7d" % (n["pc"], n["insns"], n["calls"], n["writes"]))
    print()

    # 谁写了关键地址
    print("== 关键地址的写者 ==")
    for target in (0x3FBB0, 0x45980, 0x461CB, 0x54C10):
        hits = [(pc, a, v) for a, v, sz, pc in m.bus.all_writes if a == target]
        print("  0x%06X: %d 次" % (target, len(hits)))
        for pc, a, v in hits[:6]:
            print("     写者 PC=0x%08X 值=0x%X" % (pc, v))


if __name__ == "__main__":
    main()
