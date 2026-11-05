# -*- coding: utf-8 -*-
"""bdesc.py —— 显示队列的「描述符表」：70 条，索引就是槽位号（V36 实测）。

V35 只在 0x12E26A 看到一条 `ldi:32 #0x1BC020` + `lsl #4` + `add`，把它记成
「16 字节/条的描述符表，字段未定」。V36 用三个动作把它拆开了：

  ① 静态：raw dump 70 条，四条字段的形状一眼可辨（指针 / 小整数 / 0-1-2 / 代码地址）；
  ② 动态：直接调 0x12E1EC（配好游标 82=槽位号），看它把哪条描述符的地址写进
     0x9F58D858 —— **1..69 号槽位逐一配对（69/69）**；0 号槽位是「空/无效」标记，
     只把游标 82 清 0 就跳走，不取描述符、也不更新那个指针；
  ③ 反向：用哨兵 0x55 判「0x9F58DA81 到底被写没被写」，得出它的取值规则。

一、表结构

    0x001BC020 + 槽位号 × 0x10，共 70 条（0x1BC020..0x1BC47F；0x1BC480 起换成别的数据）

    +0x00  状态字节指针 —— 65 条非 0，全部落在 0x9F58CE64..0x9F58D4FB 这一段，
           65 个互不重复；5 条为 0（槽位 0 / 15 / 29 / 63 / 68，没有状态字节）
    +0x04  数值（0..255）—— 多数是 2 的幂或小整数
    +0x08  方式（0 / 1 / 2）—— 实测 41 / 24 / 5 条
    +0x0C  处理函数 —— 44 个不同地址，全部落在 0x12E752..0x12F76C；
           其中 27 条指向 0x12E752，而那一条**就是单个 `ret`**（空处理）

    表在 ROM：往里写 0xDEADBEEF 读回来还是原值（写被丢弃），boot 全程对它 0 笔写。
    所以它是**静态描述表**，不是运行期填的。

二、索引 = 槽位号

    0x12E1EC 里 `0x1BC020 + (游标82) << 4` 算出条目地址，再 `st r0,@0x9F58D858`。
    实测：82=1 → 0x1BC030；82=5 → 0x1BC070；82=3 → 0x1BC050 …… 1..69 全对（69/69）。
    **0x9F58D858 = 当前槽位的描述符指针**（唯一写点 0x12E276）。
    82=0 是例外：函数只把 82 清 0 就跳走，指针保留上一次的值（0 号槽位 = 空标记）。

三、0x9F58DA81 是什么（V35 记的「未定」）

    哨兵法（先写 0x55 再调函数）量出来的规则：

        +8 = 1（24 条）→ 81 := +4        11/11 样本命中
        +8 = 0（41 条）→ 81 := 0         14/14 样本命中
        +8 = 2（ 5 条）→ 不成立           3 种结果（0x00 / 0x80 / 0x8A）+ 1 条保持哨兵

    所以 81 是「本次要施加的位模式」。+8=0 那 41 条为什么是 0、+8=2 那 5 条为什么散，
    本版没有定论（记 OPEN）。可报的现象：**同值成对出现**（1 号 3/1 与 2 号 6/0、
    5 号 33/1 与 6 号 33/0、7 号 18/1 与 8 号 18/0 ……），像是「置位 / 清零」两种极性。

四、取首（0x12E63A）的完整语义 —— 补 V35 的缺

    V35 只报了「它读首节点 +8」。V36 把整段跑通并核对寄存器：

        wai_sem(17) → is_empty(head)  （判据：[head] == head，即哨兵自指）
          ├ 空   → 返回 0
          └ 非空 → r4 = byte[[head] + 8] → sig_sem(17) → ret（**返回值在 R4**）

    实测：给槽位 0 的 +8 写 0x5A、+9 写 0xA5，取首返回 r4 = 0x5A（+9 一次都没被读）。
    这同时印证 V35 的「+8 是载荷、+9 是状态位」不是猜的。

五、能报 / 不能报

    能报：条数 70、索引 = 槽位号、四个字段的形状与分布、表在 ROM、当前描述符指针、
          81 的取值规则（+8=1/0 两档）、取首的返回值寄存器与判空条件、空处理函数
    不能报：+8=2 那 5 条的语义、各处理函数内部做什么、「显示模块在真机上跑过」、
          任何画面
"""
from . import blist

DESC_TABLE = 0x001BC020
DESC_STRIDE = 0x10
DESC_COUNT = 70

F_STATUS = 0x00        # u32 状态字节指针
F_VALUE = 0x04         # u32 数值
F_MODE = 0x08          # u32 方式 0/1/2
F_HANDLER = 0x0C       # u32 处理函数

MODE_CLEAR = 0         # 41 条
MODE_SET = 1           # 24 条（81 := +4）
MODE_OTHER = 2         # 5 条（规律未定）

NOOP_HANDLER = 0x0012E752     # 单条 `ret`
CURRENT_DESC = 0x9F58D858     # 当前槽位的描述符指针（唯一写点 0x12E276）
EVENT_MASK = 0x9F58DA81       # 本次要施加的位模式
APPLY_FN = 0x0012E1EC         # 「按槽位号取描述符 + 施加位模式」的函数

STATUS_MIN = 0x9F58CE64       # 65 个状态字节的地址范围
STATUS_MAX = 0x9F58D4FB
STATUS_AREA = (0x9F580000, 0x9F590000)

HEAD = blist.DISPLAY_QUEUE_HEAD


def entry_addr(i):
    """第 i 条描述符的地址（i 就是槽位号，0..69）。"""
    return DESC_TABLE + i * DESC_STRIDE


def entry(m, i):
    """读一条描述符。"""
    a = entry_addr(i)
    return {"index": i, "addr": a,
            "status": m.bus.rd(a + F_STATUS, 4),
            "value": m.bus.rd(a + F_VALUE, 4),
            "mode": m.bus.rd(a + F_MODE, 4),
            "handler": m.bus.rd(a + F_HANDLER, 4)}


def entries(m, count=DESC_COUNT):
    return [entry(m, i) for i in range(count)]


def status_byte(m, e):
    """这条描述符盯着的状态字节；没有（指针为 0）就返回 None。"""
    return m.bus.rd(e["status"], 1) if e["status"] else None


def current(m):
    """0x9F58D858 里的描述符指针（没跑过就是 0）。"""
    return m.bus.rd(CURRENT_DESC, 4)


def current_index(m):
    """当前描述符对应的槽位号；没跑过或不像描述符地址就返回 None。"""
    p = current(m)
    d = p - DESC_TABLE
    if p == 0 or d < 0 or d % DESC_STRIDE:
        return None
    i = d // DESC_STRIDE
    return i if 0 <= i < DESC_COUNT else None


def event_mask(m):
    """0x9F58DA81：本次要施加的位模式。"""
    return m.bus.rd(EVENT_MASK, 1)


def expected_mask(e):
    """按「+8=1 → +4；+8=0 → 0」预测 81；+8=2 返回 None（规律未定）。"""
    if e["mode"] == MODE_SET:
        return e["value"] & 0xFF
    if e["mode"] == MODE_CLEAR:
        return 0
    return None


def is_noop(m, e):
    """处理函数是不是那条空 `ret`。"""
    return e["handler"] == NOOP_HANDLER


def noop_is_ret(m):
    """核对 0x12E752 真的只是 `ret`（6 字节指令里就一个 0x9720）。"""
    return m.fetch_bytes(NOOP_HANDLER, 2) == b"\x97\x20"


def handler_groups(m):
    """处理函数 → 槽位号列表。"""
    g = {}
    for e in entries(m):
        g.setdefault(e["handler"], []).append(e["index"])
    return g


def audit(m):
    """体检：条数 / 指针唯一性 / 分布 / 当前指针 / 81 与规则。"""
    es = entries(m)
    ptrs = [e["status"] for e in es if e["status"]]
    modes = {}
    for e in es:
        modes[e["mode"]] = modes.get(e["mode"], 0) + 1
    bad_shape = [e["index"] for e in es
                 if (e["status"] and not (STATUS_AREA[0] <= e["status"] < STATUS_AREA[1]))
                 or e["mode"] not in (0, 1, 2)
                 or not (0x0012E000 <= e["handler"] < 0x00140000)]
    return {
        "count": len(es),
        "with_status": len(ptrs),
        "no_status": [e["index"] for e in es if not e["status"]],
        "unique_status": len(set(ptrs)),
        "status_min": min(ptrs) if ptrs else 0,
        "status_max": max(ptrs) if ptrs else 0,
        "modes": dict(sorted(modes.items())),
        "handlers": len({e["handler"] for e in es}),
        "noop": [e["index"] for e in es if e["handler"] == NOOP_HANDLER],
        "bad_shape": bad_shape,
        "current": current(m),
        "current_index": current_index(m),
        "mask": event_mask(m),
        "noop_is_ret": noop_is_ret(m),
        "expected": {e["index"]: expected_mask(e) for e in es},
    }


def mask_rule_check(m, samples=None):
    """已经调过 APPLY_FN 之后，核对 81 与规则的偏差（只对 +8=0/1 的条目）。

    返回 (命中数, 明细)。samples 是 [(index, observed_mask)]。
    """
    hit, miss = 0, []
    for i, obs in (samples or []):
        e = entry(m, i)
        exp = expected_mask(e)
        if exp is None:
            continue
        if obs == exp:
            hit += 1
        else:
            miss.append((i, obs, exp))
    return hit, miss


def dump(m, count=DESC_COUNT):
    """打印用。"""
    lines = ["描述符表 0x%08X 起 ×%d（步长 0x%02X）→ 止于 0x%08X"
             % (DESC_TABLE, DESC_COUNT, DESC_STRIDE, entry_addr(DESC_COUNT)),
             "  槽  状态字节   +4   +8  处理函数      备注"]
    for e in entries(m, count):
        note = "空处理" if e["handler"] == NOOP_HANDLER else ""
        if not e["status"]:
            note = (note + " 无状态字节").strip()
        lines.append("  %2d  %08X  %3d  %2d  %08X   %s"
                     % (e["index"], e["status"], e["value"], e["mode"],
                        e["handler"], note))
    return "\n".join(lines)


def describe(m):
    """一句话现状。"""
    i = current_index(m)
    return ("描述符 %d 条；当前指针 0x%08X%s；81 掩码 = 0x%02X"
            % (DESC_COUNT, current(m),
               ("（= 槽位 %d）" % i) if i is not None else "（没跑过）",
               event_mask(m)))


def describe_consts():
    """一张小抄（打印用）。"""
    return "\n".join([
        "描述符表 0x%08X ×%d ×0x%02X（索引 = 槽位号）" % (DESC_TABLE, DESC_COUNT, DESC_STRIDE),
        "  +0 状态字节指针（65 条非 0，0x%08X..0x%08X，互不重复）" % (STATUS_MIN, STATUS_MAX),
        "  +4 数值 / +8 方式（0=41 条 1=24 条 2=5 条）/ +0xC 处理函数（44 个，27 个是空 ret）",
        "  当前描述符指针 0x%08X（唯一写点 0x12E276）；事件掩码 0x%08X" % (CURRENT_DESC, EVENT_MASK),
        "  施加函数 0x%08X；空处理 0x%08X = `ret`" % (APPLY_FN, NOOP_HANDLER),
    ])
