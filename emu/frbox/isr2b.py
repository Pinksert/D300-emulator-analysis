# -*- coding: utf-8 -*-
"""中断 0x2B：A 段的事件位分派器（V19）。

## 这一层解决的问题

V18 把 `0x80588` 叫「主循环」，结论是「它没有调用者、在模拟器里一次都没跑过，
所以两个回调槽永远等不到派发」。V19 顺着向量表查下去，发现它不是主循环：

    A 段 TBR = 0x000FFC00（即 ROM 文件偏移 0x7FC00）
    向量表第 0xD4 项 = 0x00080588          ← 索引 0xD4 → INT n = 255-0xD4 = 0x2B
    它的 ICR（0x440 + (0x2B-0x10) = 0x45B）= 0x1A   ← 固件自己配过这条线的级别

所以 `0x80588` 是**中断 INT 0x2B 的处理函数**。它落在 A 段那一片 ISR 区
（0x809BE–0x8129C 全是向量表里的 stub）的中间，开头是 `stm0 #0xFF / stm1 #0xFC`
压栈、结尾 `ldm1 / ldm0 / reti`，长度 0x80588–0x80968，共 378 条指令。

「静态上找不到调用者」是对的，但**它不是被 call 的，是被中断进来的**。

## 它干什么

一个纯位分派器：14 字节的待办标志块 `0x3F402`–`0x3F40F`，逐位测试，命中就
`ldi:20 r12,#处理函数; call @r12`。全表 **61 条位测试、14 个标志字节、57 个不同的
处理函数** —— 其中 54 条在主路径上（`0x80588`–`0x8095C`），另外 7 条在尾声子程序
`0x8096A` 里（专管 `0x3F403` 那一位段，`0x8095C` 的 `call` 进来，`ret:d` 出去）。

    0x3F404 bit1 → 0x8223A   槽 0 派发器（V18）
    0x3F40D bit2 → 0x822CE   槽 1 派发器（V18）

⚠️ 判定位掩码时要按 V15 那条铁律：`btstl #u4` 的立即数**是掩码**（`#2` 测 bit1），
`btsth #u4` 则是 `u4 << 4`（`#4` 测 bit6）。按「立即数就是位号」读会整体错位。

## 接上这条线之后

`raise_once()` = 请求一次 INT 0x2B 并跑到 `reti` 之后。一次就是 +299 个新执行地址，
而两个回调槽**不用再手动 tick 就会自己往前走**：

    第 24 次   0x8225E(计数 12) → 0x82278(计数 10)
    第 44 次   0x82278        → 0x82298(计数 8)
    第 60 次   0x82298        → 0x822B6(计数 30)
    第 120 次  计数减到 0、标志位 0x3F404 归零，链注销

节拍是「每两次中断减一格」—— 派发器会把 `0x3F404` bit1 清掉（`0x8224C` 的
`bandl`），再由 `0x81A1E` 的 `borl` 重新置上，而这个重新置位本身也受别的位门控。

## 新的一道墙

不管这条路径会卡在 `0x9C976`：

    0x9C974  borl  r0,#2          ; 置 0x174 的 bit1（请求）
    0x9C976  ldi:20 r0,#0x174     ; ← 回来重读
    0x9C97A  btsth @r0,#4         ; 等 0x174 的 bit6（应答）
    0x9C97C  beq   -4             ; 不置就一直转

`answer=True` 就是在 `0x9C97A` 那一条上替外设答话（往直接页 `0x174` 写 `0x40`）。
这不是固件行为，是**我们假装外设回了一个「好了」**，报告里必须写清楚。
"""

from fr_emu import TABLE

VEC = 0x2B                  # INT 号
TBR = 0x000FFC00            # A 段向量表基址（ROM 文件偏移 0x7FC00）
ENTRY = 0x00080588          # 处理函数入口 = 向量表第 0xD4 项
RETI = 0x00080968           # 处理函数末尾的 reti
TAIL = 0x0008096A           # 尾声子程序：专测 0x3F403 那 7 位（0x8095C 的 call 进来）
TAIL_END = 0x000809BA       # 它的 ret:d
FLAG_LO = 0x0003F402        # 待办标志块，14 字节（0x3F402–0x3F40F 全部用上）
FLAG_HI = 0x0003F40F
POLL_PC = 0x0009C97A        # 卡住的硬件轮询：btsth @r0,#4（等 0x174 的 bit6）
POLL_REG = 0x00000174       # 轮询的直接页寄存器
POLL_BIT = 0x40             # 等它的 bit6

__all__ = ["VEC", "TBR", "ENTRY", "RETI", "TAIL", "table", "vectors", "Irq2B",
           "attach", "POLL_REG", "POLL_BIT", "POLL_PC"]


def vectors(machine):
    """向量表里所有非兜底项：{INT 号: 处理函数地址}。"""
    out = {}
    for i in range(256):
        v = machine.bus.rd(TBR + i * 4, 4)
        if v and v != 0x000FE250:
            out[255 - i] = v
    return out


def _decode(machine, addr, n):
    rom = machine.bus.rom
    base = machine.bus.rom_base
    out = []
    a = addr
    while a < addr + n:
        ins = TABLE.decode(rom[a - base: a - base + 8], 0)
        if ins is None:
            a += 2
            continue
        mn, _base, vals, size, _w = ins
        out.append((a, mn, vals, size))
        a += size
    return out


def _scan(machine, lo, hi):
    """一段连续代码里的「测位 → 装函数 → 调」三元组。"""
    rows = []
    reg = {}
    ins = _decode(machine, lo, hi - lo)
    for i, (ad, mn, v, _size) in enumerate(ins):
        if mn == "ldi:20" and "ri" in v:
            reg[v["ri"]] = v.get("i20")
            continue
        if mn not in ("btstl", "btsth") or "ri" not in v:
            continue
        u4 = v.get("u4", 0)
        mask = u4 if mn == "btstl" else (u4 << 4)
        if not mask:
            continue
        target = call = None
        for ad2, mn2, v2, _s2 in ins[i + 1: i + 5]:
            if mn2 == "ldi:20" and v2.get("ri") == 12:
                target = v2.get("i20")
            if mn2 in ("call", "jmp") and target is not None:
                call = ad2
                break
        rows.append((reg.get(v["ri"]), mask, ad, call, target))
    return rows


def table(machine):
    """静态解出位分派表：[(标志字节, 掩码, btst 地址, call 地址, 目标)]。

    两段：主路径 `0x80588`–`0x8095C`（54 条）+ 尾声子程序 `0x8096A`–`0x809BA`
    （7 条，专管 `0x3F403`）。合计 61 条、14 个标志字节、57 个不同处理函数。
    """
    return (_scan(machine, ENTRY, RETI)
            + _scan(machine, TAIL, TAIL_END))


class Irq2B:
    """把 INT 0x2B 接上：请求一次 / 连请求 N 次 / 看状态。"""

    def __init__(self, machine):
        self.machine = machine
        self.count = 0            # 一共请求过几次
        self.answers = 0          # 一共替外设答过几次话
        self.history = []         # [(次数, 覆盖增量, 回调摘要)]

    # ------------------------------------------------------------ 观测
    def installed(self):
        return self.machine.vector(VEC) == ENTRY

    def flags(self):
        m = self.machine
        return {a: m.bus.rd(a, 1) for a in range(FLAG_LO, FLAG_HI + 1)}

    def pending(self):
        return {a: v for a, v in self.flags().items() if v}

    def state(self):
        m = self.machine
        st = {"count": self.count, "answers": self.answers,
              "entry": m.vector(VEC), "icr": m.irq_level(VEC),
              "flags": self.flags(),
              "cb": m.cb.summary() if getattr(m, "cb", None) else None}
        return st

    # ------------------------------------------------------------ 动作
    def raise_once(self, answer=True, max_steps=200000):
        """请求一次 INT 0x2B，跑到 reti 之后。

        `answer=True` 时在 `0x9C976` 那条轮询上替外设写 `0x174 |= 0x40`。
        `cut=True` 表示没走到 reti —— 要么卡在轮询上，要么路上交权给了调度器。
        """
        m = self.machine
        c = m.cpu
        cov0 = m.cov.words() if getattr(m, "cov", None) else 0
        svc0 = len(m.flow.calls) if getattr(m, "flow", None) else 0
        cb0 = [s.state()["cb"] for s in m.cb.slots] if getattr(m, "cb", None) else []
        cbs0 = m.cb.summary() if getattr(m, "cb", None) else ""
        self.count += 1
        m.raise_irq(VEC)
        steps = 0
        entered = False
        answered = 0
        cut = False
        while steps < max_steps:
            c.step()
            m.step_count += 1
            steps += 1
            if c.pc == ENTRY:
                entered = True
            if answer and c.pc == POLL_PC:
                m.bus.wr(POLL_REG, 1, POLL_BIT)
                answered += 1
                self.answers += 1
            if c.pc == RETI:
                c.step()
                m.step_count += 1
                steps += 1
                break
        else:
            cut = True
        cov1 = m.cov.words() if getattr(m, "cov", None) else 0
        svc1 = len(m.flow.calls) if getattr(m, "flow", None) else 0
        cb1 = [s.state()["cb"] for s in m.cb.slots] if getattr(m, "cb", None) else []
        cbs1 = m.cb.summary() if getattr(m, "cb", None) else ""
        out = {"n": self.count, "steps": steps, "entered": entered,
               "answered": answered, "cut": cut, "delta": cov1 - cov0,
               "svc": svc1 - svc0, "cb_before": cb0, "cb_after": cb1,
               "changed": cbs0 != cbs1,
               "cb": m.cb.summary() if getattr(m, "cb", None) else ""}
        self.history.append((self.count, out["delta"], out["cb"]))
        return out

    def burst(self, n, answer=True, max_steps=200000, on_event=None):
        """连请求 n 次；回调槽有变化（有站推进 / 注销）时记一条。"""
        events = []
        last = None
        for _ in range(n):
            r = self.raise_once(answer=answer, max_steps=max_steps)
            if r["changed"] or r["cut"] or r["svc"]:
                events.append(r)
                if on_event:
                    on_event(r)
            last = r
        return events, last

    def info(self):
        st = self.state()
        return ("irq2b: 请求 %d 次 / 答话 %d 次  entry=0x%05X ICR[0x2B]=0x%02X  "
                "待办标志 %s" % (st["count"], st["answers"], st["entry"],
                                 st["icr"],
                                 " ".join("0x%05X=%02X" % (a, v)
                                          for a, v in st["flags"].items() if v)
                                 or "（空）"))


def attach(machine):
    """挂上中断 0x2B 的驱动（`Machine.enable_isr2b()` 就是调这个）。"""
    h = Irq2B(machine)
    machine.isr2b = h
    return h
