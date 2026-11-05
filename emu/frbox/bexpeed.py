# -*- coding: utf-8 -*-
"""bexpeed.py —— B 段 ↔ EXPEED 位串行总线的抓包与解码（V27 首解）。

老线早就定性过：「B 段与 EXPEED 之间是位串行，不是显存」——
CS = `0x70000002` bit2，CLK = `0x7000000A` bit1，DATA = 同寄存器 bit2，
命令字节 = 寄存器号 | 读标志 0x80，**LSB 先**。
V27 第一次把它真的解成了字节：

    boot 阶段：8 次 16 位写（0x402FC–0x40350）= 端口方向/配置，不是数据
    装配阶段：3 帧
        帧 0（8 位）  0x86            → 读寄存器 6
        帧 1（40 位） 46 00 36 00 F6  → 读周期里主机驱动的位（应答位由 EXPEED 给）
        帧 2（16 位） 76 01           → 写寄存器 0x76 = 1

也就是说：**整场运行只跟显示控制器说了三句话**。这比「总线几乎不动」
更具体 —— 命令层面也确认了显示链路没被驱动。

用法：

    from frbox import bexpeed
    tap = bexpeed.ExpeedTap().attach(m)
    ... 跑机器 ...
    tap.detach()
    tap.commands()     # [{'kind':'read','reg':6,'raw':b'\\x86','bits':8}, ...]

边界：
  * 只解码**主机写出去**的位；读周期里 EXPEED 的应答还没建模（总线读回 0），
    所以帧 1 那 5 个字节是「主机在时钟上驱动的位」，不是器件返回的数据。
  * 「读/写标志 = bit7」沿用老线结论（0x80），没有在 1.11 上单独反证过。
"""

CS_ADDR = 0x70000002
CS_BIT = 0x04          # bit2
BUS_ADDR = 0x7000000A
CLK_BIT = 0x02         # bit1
DATA_BIT = 0x04        # bit2
READ_FLAG = 0x80


def decode_bits(bits):
    """位数组（LSB 先）→ 字节列表。不足 8 位的一截丢弃。"""
    out = []
    for i in range(0, len(bits) // 8 * 8, 8):
        v = 0
        for b in range(8):
            v |= (bits[i + b] & 1) << b
        out.append(v)
    return out


def classify(byte):
    """命令字节 → (kind, reg)。bit7 = 读标志。"""
    if byte & READ_FLAG:
        return "read", byte & 0x7F
    return "write", byte


class ExpeedTap:
    """挂在总线上，把 CS/CLK/DATA 还原成「帧 → 位 → 字节」。"""

    def __init__(self):
        self.frames = []        # 每帧是位数组
        self.cfg_writes = []    # 非位串行的窗口写（端口配置）
        self._cur = []
        self._data = 0
        self._cs = False
        self._m = None
        self._orig = None

    # ---------------- 挂/摘 ----------------
    def attach(self, m):
        self._m = m
        self._orig = m.bus.wr
        m.bus.wr = self._hook
        return self

    def detach(self):
        if self._m is not None and self._orig is not None:
            self._m.bus.wr = self._orig
        self._m = None
        return self

    # ---------------- 钩子 ----------------
    def _hook(self, a, sz, v, pc=None):
        if a == CS_ADDR:
            state = (v & CS_BIT) != 0
            if state and not self._cs:
                self._cs = True
                self._cur = []
            elif not state and self._cs:
                self._cs = False
                if self._cur:
                    self.frames.append(self._cur)
                    self._cur = []
        elif a == BUS_ADDR:
            if v & DATA_BIT:
                self._data = 1
            elif (v & (CLK_BIT | DATA_BIT)) == 0:
                self._data = 0
            if v & CLK_BIT:                  # CLK 上升沿采一位
                self._cur.append(self._data)
        elif 0x70000000 <= a < 0x70100000:
            self.cfg_writes.append((pc, a, sz, v))
        return self._orig(a, sz, v, pc)

    # ---------------- 视图 ----------------
    def bytes_per_frame(self):
        return [decode_bits(f) for f in self.frames]

    def commands(self):
        """按「读命令后面那帧是数据相位」的规则解释帧序列。

        读命令（bit7=1）之后主机还要打一段时钟把数据读回来，那段位流不是
        新命令 —— 这里标成 kind='data'。写命令允许带 payload。
        """
        out, expect_data = [], False
        for k, f in enumerate(self.frames):
            bs = decode_bits(f)
            if not bs:
                continue
            if expect_data:
                out.append({"idx": k, "bits": len(f), "raw": bytes(bs),
                            "kind": "data", "reg": None, "payload": bytes(bs)})
                expect_data = False
                continue
            kind, reg = classify(bs[0])
            out.append({"idx": k, "bits": len(f), "raw": bytes(bs),
                        "kind": kind, "reg": reg, "payload": bytes(bs[1:])})
            expect_data = (kind == "read")
        return out

    def summary(self):
        cmds = self.commands()
        return {"frames": len(self.frames),
                "bits": [len(f) for f in self.frames],
                "cmds": cmds,
                "cfg_writes": len(self.cfg_writes)}


def capture(m, runner):
    """便捷封装：attach → runner(m) → detach，返回 tap。"""
    tap = ExpeedTap().attach(m)
    try:
        runner(m)
    finally:
        tap.detach()
    return tap
