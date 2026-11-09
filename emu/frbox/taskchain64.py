"""V64 纯静态：显示任务启动链 / 状态字写点归属 / 对象池布局。

只读 ROM，不建机器、不挂设备、不写内存。承接 V63 的 KI#243：
V62/V63 把显示链不闭合归到「中断状态位没给」，本设施从任务表和
`sta_tsk` 启动点把上游补全，并更正三处历史口径。

三处更正（详见 ``KNOWN_ISSUES.md`` 第三十二章）：
  * V63 的「12 个 handler 区外上游入口」不是独立上游函数，而是
    ``[0x1714EE, 0x172400)`` 这个**人为区间**切出来的同一模块首尾段；
    47 个写点只归 5 个 ``enclosing`` 函数，其中 39 个在 `enter 0x171184` 名下。
  * V62 的「15 个独立 handler」其实是同一个巨型函数
    `0x171184..0x1728B6` 内部的分支目标（2364 条指令、43 个 `ret`、
    区间内只有 1 个 `enter`）。`handler_bounds()` 是启发式切片，不等于函数边界。
  * `dispatch62.rel9_target()` 的 `rel9` 是 8 位有符号（V64 已修）。

所有常量都由真实 ROM 复核过，不要用常识改数字（硬规矩 37）。
"""

from collections import namedtuple, Counter
import struct

from frbox import dispatch62 as D

# ---- 任务表（V33 起用，记录 0 = 任务 1） ----
TASK_TABLE = 0x054C00
TASK_REC = 0x1C
TASK_COUNT = 29
TASK_ENTRY_OFF = 0x10
TASK_ID_OFF = 0x04
TASK_STACK_OFF = 0x14
DISP_LO, DISP_HI = 0x120000, 0x140000

# ---- 对象池（0x12E602 初始化循环） ----
POOL_BASE = 0x9F58D500
POOL_STRIDE = 12
POOL_COUNT = 0x46                 # `ldi:8 #0x46` → 70 槽
POOL_STATUS_OFF = 8              # `ldi:8 #8, r13` + `stb @(r5, r13)`
POOL_INIT_PC = 0x12E602
POOL_INIT_OFF_PC = 0x12E61A        # `ldi:8 #8, r13` 装状态字段偏移
POOL_INIT_STB = 0x12E61C           # `stb @(r5, r13)` 写状态字段
POOL_INIT_LOOP = 0x12E624
POOL_INIT_LOOP_REL9 = 0xFA
POOL_INIT_LOOP_TARGET = 0x12E61A
POOL_INIT_INT_PC = 0x12E630
DSP_FN = 0x12E6F8
DSP_FN_END = 0x12E750

# ---- 状态字写点归属 ----
STATUS_BASE = 0x65000000
WRITER_LO, WRITER_HI = 0x1714EE, 0x172400   # V63 人为区间，仅作对照
BIG_FN = 0x171184
BIG_FN_END = 0x1728B6
BIG_FN_INSNS = 2364
BIG_FN_RETS = 43
STA_TSK = -23

TaskRec = namedtuple("TaskRec", "tid rec entry idword stack tcb")
Launcher = namedtuple("Launcher", "pc tid entry owner delta display")


def u32(rom, base, addr):
    """大端 32 位读；越界给 0（任务表在 ROM 里，不会越界）。"""
    k = addr - base
    if not (0 <= k <= len(rom) - 4):
        return 0
    return struct.unpack(">I", rom[k:k + 4])[0]


def task_table(rom, base, table=TASK_TABLE, count=TASK_COUNT):
    """解析 `count` 条任务记录 → ``list[TaskRec]``。

    `+0x10` 是任务入口 PC（序言起点，`enter` 在其后 2/4 字节，
    硬规矩 26），`+0x14` 是任务栈顶，`+0x00` 是 TCB 地址。
    """
    out = []
    for n in range(count):
        a = table + n * TASK_REC
        out.append(TaskRec(
            tid=n + 1, rec=a,
            entry=u32(rom, base, a + TASK_ENTRY_OFF),
            idword=u32(rom, base, a + TASK_ID_OFF),
            stack=u32(rom, base, a + TASK_STACK_OFF),
            tcb=u32(rom, base, a)))
    return out


def display_tasks(recs, lo=DISP_LO, hi=DISP_HI):
    """入口落显示区的任务 → ``{tid: entry}``。"""
    return dict((r.tid, r.entry) for r in recs if lo <= r.entry < hi)


def launchers(sweep, recs, svc=STA_TSK):
    """`sta_tsk` 静态调用点 → ``list[Launcher]``。

    只取 ``Sweep.calls`` 里能读出任务 ID 的点；`owner` 用
    ``enclosing()``（粗粒度，硬规矩 30），`delta` 是「目标任务入口 − 启动点 PC」，
    用来识别「紧邻目标任务入口」的链式自启动。
    """
    tbl = dict((r.tid, r.entry) for r in recs)
    out = []
    for c in sweep.calls:
        if c.svc != svc and c.svc != (svc & 0xFF):
            continue
        tid = c.r4
        entry = tbl.get(tid, 0)
        out.append(Launcher(
            pc=c.addr, tid=tid, entry=entry,
            owner=sweep.enclosing(c.addr),
            delta=(entry - c.addr) if entry else None,
            display=bool(entry and DISP_LO <= entry < DISP_HI)))
    return sorted(out, key=lambda x: x.pc)


def pool_init(rom, base, lo=POOL_INIT_PC, hi=0x12E636):
    """对象池初始化循环的关键指令 → ``{pc: 助记符}`` 与布局常量核对。"""
    ins = D.decode_range(rom, base, lo, hi)
    by_pc = dict((x.pc, x) for x in ins)
    off = by_pc.get(POOL_INIT_OFF_PC)
    stb = by_pc.get(POOL_INIT_STB)
    loop = by_pc.get(POOL_INIT_LOOP)
    return {
        "insns": [x.mn for x in ins],
        # 偏移由 `ldi:8 #8, r13` 单独装载，不在 `stb` 的立即数字段里
        "stb_off": off.vals.get("i8") if off else None,
        "stb_mnem": stb.mn if stb else None,
        "loop_rel9": loop.vals.get("rel9") if loop else None,
        "loop_target": (D.rel9_target(POOL_INIT_LOOP, loop.vals["rel9"])
                        if loop and "rel9" in loop.vals else None),
        "stride": POOL_STRIDE,
        "count": POOL_COUNT,
        "status_off": POOL_STATUS_OFF,
        "span": POOL_STRIDE * POOL_COUNT,
    }


def writer_owners(sweep, addrs=None, lo=WRITER_LO, hi=WRITER_HI):
    """六状态字写点归属 → ``(rows, Counter(owner), 区外 rows)``。

    `rows` 元素为 ``(pc, addr, mnem, owner)``；「区外」只是相对 V63 的
    人为区间 `[lo, hi)`，不代表独立的上游函数。
    """
    if addrs is None:
        addrs = [STATUS_BASE + o for o in D.STATUS_OFFSETS]
    rows = []
    for a in addrs:
        for pc, mn in sweep.writers_of(a):
            rows.append((pc, a, mn, sweep.enclosing(pc)))
    rows.sort()
    owners = Counter(r[3] for r in rows)
    outside = [r for r in rows if not (lo <= r[0] < hi)]
    return rows, owners, outside


def big_function(rom, base, lo=BIG_FN, hi=BIG_FN_END, handlers=D.HANDLERS):
    """巨型函数的线性反汇编计数与 15 个分支目标归属。"""
    ins = D.decode_range(rom, base, lo, hi)
    return {
        "insns": len(ins),
        "enters": [x.pc for x in ins if x.mn == "enter"],
        "rets": [x.pc for x in ins if x.mn.startswith("ret")],
        "handlers_inside": sum(1 for h in handlers if lo <= h < hi),
        "handlers_in_range": [h for h in handlers if lo <= h < hi],
    }


def rel9_audit(rom, base, lo=0x17171E, hi=0x171834):
    """分派器内所有 rel9 分支：旧（无符号）与新（有符号）目标是否一致。"""
    out = []
    for x in D.decode_range(rom, base, lo, hi):
        v = x.vals.get("rel9")
        if v is None:
            continue
        old = (x.pc + 2 + v * 2) & 0xFFFFFFFF
        new = D.rel9_target(x.pc, v)
        out.append((x.pc, x.mn, v, old, new))
    return out
