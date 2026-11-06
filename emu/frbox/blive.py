# -*- coding: utf-8 -*-
"""B 段整机自跑：派发不再靠补中断。

V39 之前，`0x44`（请求调度寄存器）一直是「不建模」的：写进去落进影子存储，
没人应答，于是所有任务切换都得靠 `brealos.request_dispatch()` 或
`bready.run_until()` **人工补一次 `vec 0x3F`**。V39 的报告里写得很清楚：
「补的是硬件应答，选谁仍由固件派发器决定」——那是个诚实的但难看的补丁。

V40 打开 `dispatch=True` 之后发现，整机**本来就能自己跑**：

    boot(191,520 步) → 内核第一次写 0x44（其实在 boot 里就写了两次）
    → vec 0x3F 派发 → 换到任务 2 → 任务 2 一个接一个把任务 3/4/5/6/7/9/10/11 挂起来
    → 任务 5 上 CPU → 0x12E006 显示初始化自己跑完（294,388 步）

全程没有一句 `raise_irq`。400,000 步里 24 次换手、9 个任务入口被执行过；
两次独立运行换手步数**逐个相同**（确定性）。

## 推迟是怎么做到的

`0x46F` 是 48 个 ICR（`0x440 + (vec − 0x10)`）里**唯一不被 boot 初始化循环写**的一个，
它属于 `vec 0x3F`（派发器）。三个写点：

    0x4086C  ← 0x10    boot 阶段设初值（0x40866 那个小函数：ldi:20 #0x440 / ldi:8 #0x10 / stb @(0x2F)）
    0x4069C  ← 0x1E    内核服务入口（0x40680 那段 stilm #0x1E 之后）
    0x406D4  ← 0x10    内核服务出口（reti 之前写回初值）

于是服务运行期间 `ICR(0x3F) = 0x1E`，而临界区 `ILM = 0x1E`，受理条件是 `level < ILM`
→ **派发被挡住**；`reti` 把 PS 还回任务上下文（`ILM = 0x1F`）之后才受理。
实测 130 组「请求 → 派发」全部符合这条：请求那一刻 `ICR=0x1E / ILM=0x1E`，
派发那一刻 `ILM=0x1F`。这就是 V39 留下的 KI#161「受理时机」的答案。

## 0x44 是锁存器，不是只写寄存器

V11 记过「固件里从头到尾没有任何一处读它」——**错**。300,000 步里它被读 64 次、写 64 次，
全在两个写点后面一条指令：`0x41886` 写 1 → `0x41888` 读回；`0x40EAC` 写 0 → `0x40EAE` 读回。
读回值随即被丢弃（下一条就是 `ret:d`），是**写后哑读**，不是读状态。

但写 0 是有意义的：`0x40EAC` 那处就是**撤销请求**。V39 的设备模型只置不清，
一条已经撤销的请求会一直躺在 `cpu.irq_pending` 里等下一次开中断，被当成真请求受理。
本版把 `DispatchRequest` 改成锁存语义（写 1 置位挂中断 / 写 0 清位并撤销待决项 /
读回锁存值），boot 末尾的待决项从 2 条降到 1 条，自跑结论不变。
"""

from . import bready, brealos

#: 直接页里的「请求调度」寄存器（写 1 置位、写 0 撤销）
DISPATCH_REG = 0x44
#: 派发器的向量，以及它那条 ICR（= 0x440 + (0x3F − 0x10)）
DISPATCH_VEC = 0x3F
DISPATCH_ICR = 0x46F
#: ICR 数组：48 条，按 vec − 0x10 线性排
ICR_BASE = 0x440
ICR_COUNT = 0x30
#: 内核区头部：当前 TCB / 就绪位图
CURRENT_TCB = 0x68000000
READY_MAP = 0x68000004
#: boot 到空闲任务的确定性步数（V38 实测）
BOOT_STEPS = 191520
#: 显示初始化（任务 5 的活）
DISPLAY_INIT = 0x0012E006
#: 显示初始化的第一笔副作用：队列哨兵自指（用它精确标出「跑起来的步数」）
DISPLAY_MARK = 0x9F58D500
#: 70 个槽位的基址（哨兵占 8 字节）；槽位 i 的 +8 在 SLOT_BASE + 8 + i*12
SLOT_BASE = 0x9F58D508


def boot(steps=BOOT_STEPS):
    """新建一台 `dispatch=True` 的 d300-b 并 boot 到空闲任务。

    与 `brealos.boot()` 的唯一差别是打开了 0x44 的请求锁存器 —— 这一句就是
    「从人工补中断到固件自己派发」的全部分歧。
    """
    from .machine import create
    m = create("d300-b", dispatch=True)
    m.load()
    m.reset()
    m.enable_cov()
    m.attach_flow()
    n = 0
    while not m.cpu.halted and n < steps:
        m.step(1)
        n += 1
    return m


def current(m):
    """当前任务的 TCB（内核区头部 +0）。"""
    return m.bus.rd(CURRENT_TCB, 4)


def name(m, tcb=None, index=None):
    """TCB → 「任务 N」/「空闲」。"""
    return bready.tcb_name(m, current(m) if tcb is None else tcb, index)


def icr(m, vec=DISPATCH_VEC):
    """读某条 ICR 的级别（低 5 位）。"""
    return m.bus.rd(ICR_BASE + ((vec - 0x10) & 0x3F), 1) & 0x1F


def pending_dispatch(m):
    """CPU 待决队列里有几条 vec 0x3F。"""
    return sum(1 for _lvl, vec in m.cpu.irq_pending if vec == DISPATCH_VEC)


class Watch(object):
    """挂上总线/中断钩子，记录自跑期间的四类事件。

    `events` 每条形如 `(步数, 类型, 说明)`，类型为 REQ / ACK / DISP / SW。
    """

    def __init__(self, m):
        self.m = m
        self.events = []
        self.swapped = []          # (步数, 新 TCB)
        self.init_at = None
        self._wr = None
        self._rd = None
        self._eit = None
        self._names = {r["tcb"]: "任务%d" % r["id"]
                       for r in brealos.records(m, include_idle=True)}
        self._cur = current(m)

    # ---- 安装 / 卸载 ----
    def attach(self):
        m, c = self.m, self.m.cpu
        self._wr, self._rd, self._eit = m.bus.wr, m.bus.rd, c.eit

        def wr(a, n, v, pc=0):
            if a == DISPATCH_REG:
                self.events.append((m.step_count, "REQ" if (v & 0xFF) else "ACK",
                                    "ICR=0x%02X ILM=0x%02X pc=0x%08X %s"
                                    % (icr(m), c.ilm, pc, self._tn(self._cur))))
            elif a == DISPLAY_MARK and self.init_at is None:
                self.init_at = m.step_count
                self.events.append((m.step_count, "INIT", "显示初始化第一笔写（哨兵 0x9F58D500）"))
            elif a == CURRENT_TCB:
                self.events.append((m.step_count, "SW", "%s → %s"
                                    % (self._tn(self._cur), self._tn(v))))
                self.swapped.append((m.step_count, v))
                self._cur = v
            return self._wr(a, n, v, pc)

        def rd(a, n, pc=0):
            if a == DISPATCH_REG:
                self.events.append((m.step_count, "RD", "→ 0x%X pc=0x%08X"
                                    % (self._rd(a, n, pc) & 0xFF, pc)))
            return self._rd(a, n, pc)

        def eit(vec, level=None):
            if vec == DISPATCH_VEC:
                self.events.append((m.step_count, "DISP", "level=%s ILM=0x%02X pc=0x%08X %s"
                                    % (("0x%02X" % level) if level is not None else "-",
                                       c.ilm, c.pc, self._tn(self._cur))))
            return self._eit(vec, level)

        m.bus.wr, m.bus.rd, c.eit = wr, rd, eit
        return self

    def detach(self):
        self.m.bus.wr, self.m.bus.rd, self.m.cpu.eit = self._wr, self._rd, self._eit
        return self

    def __enter__(self):
        return self.attach()

    def __exit__(self, *a):
        self.detach()
        return False

    # ---- 视图 ----
    def _tn(self, tcb):
        return self._names.get(tcb, "0x%08X" % tcb if isinstance(tcb, int) else str(tcb))

    def of(self, kind):
        return [e for e in self.events if e[1] == kind]

    def counts(self):
        return {k: len(self.of(k)) for k in ("REQ", "ACK", "DISP", "SW", "RD", "INIT")}

    def switches(self):
        """[(步数, 换到哪个 TCB)]，只记内核区 +0 的写，不看 pc 猜。"""
        return list(self.swapped)

    def relay_ok(self):
        """推迟铁律：每条 REQ 的 ICR 必是 0x1E，每条 DISP 的 ILM 必是 0x1F。

        返回 (符合的 REQ 数, 不符合的 REQ 数, 符合的 DISP 数, 不符合的 DISP 数)。
        """
        rok = rbad = dok = dbad = 0
        for _s, kind, txt in self.events:
            if kind == "REQ":
                if "ICR=0x1E" in txt and "ILM=0x1E" in txt:
                    rok += 1
                else:
                    rbad += 1
            elif kind == "DISP":
                if "ILM=0x1F" in txt:
                    dok += 1
                else:
                    dbad += 1
        return rok, rbad, dok, dbad


def run(m, steps=200000, chunk=20000):
    """跑一段，记录事件；返回汇总。

    `steps` 按机器步数算（不是 chunk 数）。返回的字典带 `events` / `counts` /
    `switches` / `tasks` / `display_init`（首次执行显示初始化时的步数，没跑到为 None）。
    """
    w = Watch(m).attach()
    done = 0
    while done < steps and not m.cpu.halted:
        k = min(chunk, steps - done)
        m.run(max_steps=k, spin_guard=False)
        done += k
    w.detach()
    init_at = w.init_at
    if init_at is None and m.cov is not None and m.cov.visited(DISPLAY_INIT):
        init_at = m.step_count  # 只在「写哨兵之前就进过函数」这种异常情况下兜底
    tasks = [r["id"] for r in brealos.records(m)
             if m.cov is not None and m.cov.visited(r["entry"])]
    return {"steps": m.step_count, "current": current(m), "current_name": name(m),
            "events": w.events, "counts": w.counts(), "switches": w.switches(),
            "tasks": tasks, "display_init": init_at,
            "relay": w.relay_ok(), "ready_map": m.bus.rd(READY_MAP, 4),
            "device": (m.dispatch.state() if getattr(m, "dispatch", None) else {})}


def audit(m, res=None):
    """体检：返回 [(项目, 结论)]。"""
    out = []
    out.append(("0x44 有没有建模", "有（dispatch=True）" if getattr(m, "dispatch", None)
                else "没有（写落进影子存储）"))
    out.append(("boot 末待决派发", "%d 条" % pending_dispatch(m)))
    out.append(("ICR 0x46F（vec 0x3F）", "0x%02X" % icr(m)))
    out.append(("ICR 数组", "0x%X..0x%X（%d 条）" % (ICR_BASE, ICR_BASE + ICR_COUNT - 1, ICR_COUNT)))
    out.append(("当前任务", name(m)))
    if res:
        rok, rbad, dok, dbad = res["relay"]
        out.append(("推迟铁律（REQ 的 ICR=0x1E）", "%d 对 / %d 不对" % (rok, rbad)))
        out.append(("推迟铁律（DISP 的 ILM=0x1F）", "%d 对 / %d 不对" % (dok, dbad)))
        out.append(("换手次数", "%d" % len(res["switches"])))
        out.append(("执行过的任务", ", ".join(str(t) for t in res["tasks"]) or "无"))
        out.append(("显示初始化", ("%d 步时执行" % res["display_init"])
                    if res["display_init"] else "没执行"))
    return out


def describe(m):
    """一行摘要。"""
    d = getattr(m, "dispatch", None)
    req = d.requests if d else 0
    out = "步=%-8d 当前=%-5s 位图=0x%08X 请求=%d" % (
        m.step_count, name(m), m.bus.rd(READY_MAP, 4), req)
    if d:
        out += " 撤销=%d 锁存=%d" % (d.acks, d.latch)
    return out


def dump(m, res=None):
    """多行文本视图（给探针/调试用）。"""
    lines = [describe(m)]
    lines += ["  %-26s %s" % (k, v) for k, v in audit(m, res)]
    if res:
        lines.append("  事件:")
        for s, kind, txt in res["events"][:40]:
            lines.append("    %8d %-4s %s" % (s, kind, txt))
        if len(res["events"]) > 40:
            lines.append("    …… 共 %d 条" % len(res["events"]))
    return "\n".join(lines)
