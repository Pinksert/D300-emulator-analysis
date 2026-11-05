# -*- coding: utf-8 -*-
"""bexpeed.py —— B 段 ↔ EXPEED 位串行总线的抓包与解码（V27 首解）。

老线早就定性过：「B 段与 EXPEED 之间是位串行，不是显存」——
CS = `0x70000002` bit2，CLK = `0x7000000A` bit1，DATA = 同寄存器 bit2，
命令字节 = 寄存器号 | 读标志 0x80，**LSB 先**。
V27 第一次把它真的解成了字节；V28 修正了帧边界（见下）并补了器件侧模型
（`dev_expeed.py`）。

    boot 阶段：8 次 16 位写（0x402FC–0x40350）= 端口方向/配置，不是数据
    装配阶段（V28 重测，按 CS 上升沿分帧）：4 个 CS 窗口
        窗口 0（  1 位）—             CS 拉高前的一个散时钟
        窗口 1（ 16 位）86 00         命令 0x86（读寄存器 6）+ 8 位周转
        窗口 2（ 48 位）46 00 36 00 F6 00   读数据相位（主机打的时钟）
        窗口 3（ 16 位）76 01         命令 0x76（写）+ 载荷 0x01

也就是说：**整场运行只跟显示控制器说了三句话**（读 6 / 读回 / 写 0x76）。

⚠️ V27 报的「整场 2 帧、0 条写命令」是抓包器 bug：它在 CS **下降沿**收帧，
把窗口 2 与窗口 3 并成了一帧并当成数据相位，于是漏掉了 `76 01` 这条真写命令。
V28 已改成 CS 上升沿起帧。

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
        self._clk = False
        self._m = None
        self._orig = None

    # ---------------- 挂/摘 ----------------
    def attach(self, m):
        self._m = m
        self._orig = m.bus.wr
        m.bus.wr = self._hook
        return self

    def flush(self):
        """把最后一个还没被下一个 CS 上升沿关掉的帧收进来。"""
        if self._cur:
            self.frames.append(self._cur)
            self._cur = []

    def detach(self):
        self.flush()
        if self._m is not None and self._orig is not None:
            self._m.bus.wr = self._orig
        self._m = None
        return self

    # ---------------- 钩子 ----------------
    def _hook(self, a, sz, v, pc=None):
        if a == CS_ADDR:
            state = (v & CS_BIT) != 0
            # V28 修正：帧边界在 **CS 上升沿**（一次选中 = 一帧）。
            # V27 在 CS 下降沿收帧，把「数据相位那一帧」和后面的写命令吞掉了，
            # 于是少报一帧、也漏掉了真实的写命令 0x76。
            if state and not self._cs:
                if self._cur:
                    self.frames.append(self._cur)
                    self._cur = []
            self._cs = state
        elif a == BUS_ADDR:
            # ⚠️ 这条规则只在**没有器件应答**时成立（读回恒 0）。接了
            # dev_expeed 之后主机读-改-写会带回 din，这里会把应答位误当
            # 主机数据 —— 那种场景请用 m.expeed.summary()（器件侧命令日志）。
            if v & DATA_BIT:
                self._data = 1
            elif (v & (CLK_BIT | DATA_BIT)) == 0:
                self._data = 0
            # 只在 **CLK 上升沿**采一位：主机的读-改-写会把 CLK=1 写多次，
            # 按「写了 bit1 就算一位」会重复计数（V28 实测 19/54 vs 真值 16/48）。
            clk = (v & CLK_BIT) != 0
            if clk and not self._clk:
                self._cur.append(self._data)
            self._clk = clk
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
        self.flush()
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
