# -*- coding: utf-8 -*-
"""函数级仿真运行器：让解释器真的走进子程序（而不是一律打桩）。

提供：
  · 调用树（进入时记 r0..r3 参数，返回时记 r0 返回值），带缩进打印
  · 递归深度上限，超过就退化成打桩，避免无限递归
  · 指定地址打桩（外设驱动、μITRON 服务等不想真跑的）
  · MMIO 访问计数、RAM 写入按地址区间统计、热点 PC
  · 死循环逃生：同一条指令连续执行超过 N 次即停
"""
import sys
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

from fr_emu import Fr, Mem, TABLE, M32, Unimpl
from fr_itron import ItronStub


class Node:
    def __init__(self, target, caller, ret, depth, args, label=None):
        self.target, self.caller, self.ret, self.depth = target, caller, ret, depth
        self.args = args
        self.label = label
        self.children = []
        self.ret_val = None
        self.stubbed = False

    def __repr__(self):
        return f"0x{self.target:08X}"


class Runner:
    SENTINEL = 0xFFFFFFFE                   # 顶层函数返回的哨兵地址

    def __init__(self, rom, rom_base, stack_top=0x0007F000, depth_limit=8, stubs=(), quiet_stubs=True):
        self.m = Mem()
        self.m.load_rom(rom_base, rom)
        self.cpu = Fr(self.m, TABLE)
        self.cpu.pc = rom_base
        self.cpu.r[15] = stack_top
        self.stack = []                 # Node 栈
        self.root = Node(0, 0, 0, -1, [])   # 虚拟根节点，各顶层调用挂在它下面
        self.depth_limit = depth_limit
        self.stubs = set(stubs)
        self.quiet = quiet_stubs
        self.stub_log = []
        self.ret_reason = ""
        self.pc_hits = {}
        self.svc = ItronStub(self)
        self.cpu.call_hook = self._on_call
        self.cpu.svc_hook = self._on_svc

    # ---------------- 调用/返回 ----------------
    def _on_call(self, cpu, target, pc):
        ret = cpu._dr(1)
        args = list(cpu.r[0:4])
        depth = len(self.stack)
        if target in self.stubs or depth >= self.depth_limit:
            self.stub_log.append((target, pc, args, "stub" if target in self.stubs else "depth"))
            n = Node(target, pc, ret, depth, args)
            n.stubbed = True
            (self.stack[-1] if self.stack else self.root).children.append(n)
            return True
        n = Node(target, pc, ret, depth, args)
        (self.stack[-1] if self.stack else self.root).children.append(n)
        self.stack.append(n)
        return False

    def _on_svc(self, cpu, num, pc):
        self.svc(cpu, num, pc)
        name = self.svc.log[-1][2]
        n = Node(0, pc, 0, len(self.stack), list(cpu.r[0:4]), label=name)
        (self.stack[-1] if self.stack else self.root).children.append(n)

    def _pop_if_returned(self):
        while self.stack and self.cpu.pc == self.stack[-1].ret:
            n = self.stack.pop()
            n.ret_val = self.cpu.r[0]

    # ---------------- 运行 ----------------
    def run(self, entry, max_steps=200000, stop_pcs=(), max_same_pc=200000):
        self.cpu.pc = entry
        self.cpu._set_dr(1, self.SENTINEL)      # 顶层函数 ret 时回到哨兵
        stop = set(stop_pcs)
        for _ in range(max_steps):
            if self.cpu.halted:
                self.ret_reason = self.cpu.halt_reason
                return self.cpu.steps
            if self.cpu.pc == self.SENTINEL:
                self.ret_reason = "顶层函数返回"
                return self.cpu.steps
            if self.cpu.pc in stop:
                self.ret_reason = f"到达停止点 0x{self.cpu.pc:08X}"
                return self.cpu.steps
            pc = self.cpu.pc
            self.pc_hits[pc] = self.pc_hits.get(pc, 0) + 1
            if self.pc_hits[pc] > max_same_pc:
                self.ret_reason = f"疑似死循环 @0x{pc:08X}"
                return self.cpu.steps
            try:
                self.cpu.step()
            except Unimpl as e:
                self.ret_reason = f"未实现指令 {e.args[0]} @0x{pc:08X}"
                return self.cpu.steps
            self._pop_if_returned()
        self.ret_reason = "达到步数上限"
        return self.cpu.steps

    # ---------------- 报表 ----------------
    def tree(self, node=None, indent=0, out=None, max_nodes=200):
        out = out if out is not None else []
        node = node or self.root
        if node is None or len(out) >= max_nodes:
            return out
        tag = "（打桩）" if node.stubbed else ""
        a = " ".join(f"{x:08X}" for x in node.args)
        r = "" if node.ret_val is None else f" → 0x{node.ret_val:08X}"
        head = node.label or f"0x{node.target:08X}"
        out.append("  " * indent + f"{head}{tag}  <- 0x{node.caller:08X}  "
                   f"r0..r3=[{a}]{r}")
        for c in node.children:
            self.tree(c, indent + 1, out, max_nodes)
        return out

    def ram_writes(self, lo=0, hi=0x100000):
        agg = {}
        for a, n, v, pc in self.m.all_writes:
            if lo <= a < hi:
                agg.setdefault(a, []).append((v, n, pc))
        return dict(sorted(agg.items()))


def load_a(path, base=0x00080000):
    return open(path, "rb").read(), base


if __name__ == "__main__":
    import sys as _s
    rom, base = load_a(os.path.join(FW_DIR, "A3000111.bin"))
    r = Runner(rom, base, depth_limit=6)
    r.run(0x000DB8F0, max_steps=200000)
    print("步数", r.cpu.steps, "退出：", r.ret_reason)
    print("\n".join(r.tree()[:40]))
