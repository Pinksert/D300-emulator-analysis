# -*- coding: utf-8 -*-
"""bqueue.py —— 显示请求队列的「槽位」与「游标」（V35 实测）。

V31–V34 解完了队列的链（哨兵 0x9F58D500、节点 = 槽位、容量 70、四件套、sem 17），
还剩三个空：槽位里除 +0/+4/+9 之外那个 **+8 字节**是什么、谁填它、
0x9F58DA82 那张「索引表」是什么。V35 用「静态引用 + 全程写入记账」两把尺子量出来：

一、槽位布局（12 字节 × 70）

    +0x00 next  / +0x04 prev   —— 循环双链表指针（V33/V34）
    +0x08 **载荷字节**          —— 取首函数 0x12E63A 唯一读出来的东西
    +0x09 状态位                —— 0x80 = 已挂链（0x12E72E）、0xC0 = 0x80|0x40（0x12E740）

    ⚠️ 载荷字节在本模拟里**从未被写成非 0**：
      * 静态：整片固件只有 4 条指令拿得到槽位基址 0x9F58D508，其中写 +8 的一个都没有；
      * 动态：boot 到空闲（191,520 步）里，槽位 +8 唯一的写者是 0x40384 的清零循环
        （70 次写 0，见 probe 输出）；开门场景跑到提交 0x12E6F8，它只改 +0/+4/+9。
      → 所以「取首」在现在的模拟里恒返回 0。填载荷的代码在没执行的页里（显示初始化
        0x12E006 一族 + 描述符表 0x1BC020）。

二、槽位 +8 的环形分配由「游标块」管：0x9F58DA81..0x9F58DA84

    它不是一个字节，是**连续四个字节的四元组**（0x12E1EC 函数一次全取）：
      0x9F58DA81  +   —— 只在 0x12E1F4 / 0x12E330 / 0x12F832 / 0x12FBCC / 0x12FCFC 出现
      0x9F58DA82  当前槽位号  —— 重挂函数 0x12E676 拿它算槽位：0x9F58D508 + b*0x0C
      0x9F58DA83  计数        —— 与 70（0x46）比较，到顶就回绕（0x12E21E-0x12E22A）
      0x9F58DA84  下一个槽位号 —— 0x12E240 读它、0x12E242 写进 0x82

    boot 后四个字节全是 0 —— 说明这条「游标 + 60 槽环形」的路一次都没走起来。
    与之配套的是**描述符表 @0x1BC020，16 字节/条**（0x12E26A-0x12E274：
    0x1BC020 + (游标<<4)，把 r4 存进去）—— 这是「一条记录 = 16 字节」的第二张表。

三、队列的四个入口（V34 已解，这里补上「谁调」）

    0x12E602 清空  ← 整片固件只有 1 个调用点：0x12E01E（显示初始化 0x12E006 的开头）
    0x12E63A 取首  ← 无调用点（立即数与 rel12 都是 0）
    0x12E672 重挂  ← 无调用点（立即数与 rel12 都是 0）
    0x12E6F8 提交  ← 40 处 32 位立即数 + 若干 rel12

    ⚠️ 判「谁调」时 rel12 线性扫描会撞进 `ldi:32` 的立即数中间产生假阳性
    （例：0x12E1F2 落在 0x12E1EE 的 6 字节指令里），必须校验指令边界（见 KI#134）。

四、能报 / 不能报

    能报：+8 是载荷、+9 是状态位、载荷从未被填、游标块四个字节的分工、清空的唯一调用点
    不能报：+8 到底装什么业务值（界面元素 id？）、描述符表 0x1BC020 的字段、
            0x9F58DA81 的用途、队列在实际相机里是否真的跑过
"""
from . import blist

HEAD = blist.DISPLAY_QUEUE_HEAD          # 0x9F58D500
SLOT_BASE = 0x9F58D508                   # 槽位 0 的地址
SLOT_STRIDE = 0x0C
SLOT_COUNT = 70                          # ldi:8 r0,#0x46 @0x12E620

S_NEXT = 0x00
S_PREV = 0x04
S_PAYLOAD = 0x08
S_FLAG = 0x09

FLAG_LINKED = 0x80                       # 0x12E72E borh #0x80
FLAG_EXTRA = 0x40                        # 0x12E740 borh #0x40 → 0xC0

CURSOR_BASE = 0x9F58DA81                 # 四元组 81/82/83/84
CURSOR_SLOT = 0x9F58DA82                 # 当前槽位号（重挂拿它算地址）
CURSOR_COUNT = 0x9F58DA83                # 与 70 比较的计数
CURSOR_NEXT = 0x9F58DA84                 # 下一个槽位号

DESC_TABLE = 0x001BC020                  # 16 字节/条的描述符表（0x12E26A）
DESC_STRIDE = 0x10

SUBMIT = 0x0012E6F8                      # 生产：wai_sem(17) → insert → sig_sem(17)
CLEAR = 0x0012E602                       # 清空：init + 清 70 槽 + set_flg(3,0x80)
PEEK = 0x0012E63A                        # 取首：只读首节点 +8，不摘链
REHANG = 0x0012E672                      # 重挂：unlink 后立刻 insert

CLEAR_CALLER = 0x0012E01E                # 清空的唯一调用点（在 0x12E006 里）
DISPLAY_INIT = 0x0012E006                # 显示初始化（写 0x5C000000 / 0x5C003008）
CURSOR_FN = 0x0012E1EC                   # 用游标块分配槽位的那个函数
ZERO_LOOP = 0x00040384                   # boot 里把 70 个 +8 清 0 的那条 st


def slot_addr(i):
    """第 i 个槽位（i 从 0 数起，0..69）。"""
    return SLOT_BASE + i * SLOT_STRIDE


def slot(m, i):
    """整条槽位：next / prev / payload / flag。"""
    a = slot_addr(i)
    return {"index": i, "addr": a,
            "next": m.bus.rd(a + S_NEXT, 4),
            "prev": m.bus.rd(a + S_PREV, 4),
            "payload": m.bus.rd(a + S_PAYLOAD, 1),
            "flag": m.bus.rd(a + S_FLAG, 1)}


def slots(m, count=SLOT_COUNT):
    return [slot(m, i) for i in range(count)]


def payload(m, i):
    """取首函数唯一读出来的那个字节。"""
    return m.bus.rd(slot_addr(i) + S_PAYLOAD, 1)


def flag(m, i):
    return m.bus.rd(slot_addr(i) + S_FLAG, 1)


def index_of(addr):
    """节点地址 → 槽位号（不是槽位地址就返回 None）。"""
    d = addr - SLOT_BASE
    if d < 0 or d % SLOT_STRIDE:
        return None
    i = d // SLOT_STRIDE
    return i if 0 <= i < SLOT_COUNT else None


def cursor(m):
    """游标四元组（81/82/83/84）。"""
    b = [m.bus.rd(CURSOR_BASE + k, 1) for k in range(4)]
    return {"a81": b[0], "slot": b[1], "count": b[2], "next": b[3], "raw": bytes(b)}


def set_cursor(m, slot_no=None, count=None, next_no=None):
    """替固件写游标（探针用，不执行代码）。"""
    if slot_no is not None:
        m.bus.wr(CURSOR_SLOT, 1, slot_no & 0xFF)
    if count is not None:
        m.bus.wr(CURSOR_COUNT, 1, count & 0xFF)
    if next_no is not None:
        m.bus.wr(CURSOR_NEXT, 1, next_no & 0xFF)
    return cursor(m)


def desc_addr(i):
    """描述符表第 i 条（16 字节/条，0x1BC020 起）。"""
    return DESC_TABLE + i * DESC_STRIDE


def peek(m):
    """按 0x12E63A 的语义取值（模型）：空 → 0，否则首节点的 +8。"""
    n = blist.first(m, HEAD)
    return 0 if n is None else m.bus.rd(n + S_PAYLOAD, 1)


def submit_model(m, i, flag_extra=False):
    """按 0x12E6F8 的语义挂链（模型）：insert(head, slot) + 置 +9。"""
    node = slot_addr(i)
    blist.insert_model(m, HEAD, node)
    v = FLAG_LINKED | (FLAG_EXTRA if flag_extra else 0)
    m.bus.wr(node + S_FLAG, 1, m.bus.rd(node + S_FLAG, 1) | v)
    return node


def dump(m, count=8):
    """前 count 条槽位（打印用）。"""
    lines = ["槽位 0x%08X 起 ×%d（步长 0x%02X）→ 止于 0x%08X"
             % (SLOT_BASE, SLOT_COUNT, SLOT_STRIDE, slot_addr(SLOT_COUNT)),
             "  idx  地址        next       prev      载荷 状态"]
    for s in slots(m, min(count, SLOT_COUNT)):
        lines.append("  %3d  0x%08X  %08X   %08X  %02X   %02X"
                     % (s["index"], s["addr"], s["next"], s["prev"],
                        s["payload"], s["flag"]))
    return "\n".join(lines)


def describe(m):
    """一句话现状：链 + 游标。"""
    c = cursor(m)
    return ("链：%s\n游标：slot=%d count=%d next=%d a81=%d"
            % (blist.describe(m, HEAD), c["slot"], c["count"], c["next"], c["a81"]))


def describe_consts():
    """一张小抄（打印用）。"""
    return "\n".join([
        "队列：哨兵 0x%08X / 槽位 0x%08X ×%d ×0x%02X / 锁 sem 17" % (HEAD, SLOT_BASE, SLOT_COUNT, SLOT_STRIDE),
        "  槽位：+0 next / +4 prev / +8 载荷 / +9 状态（0x80 挂链、0x40 另一含义）",
        "  游标：0x%08X 起四字节 = a81 / 当前槽位 / 计数(上限 70) / 下一个槽位" % CURSOR_BASE,
        "  描述符表：0x%08X 起 16 字节/条（0x12E26A 用游标<<4 索引）" % DESC_TABLE,
        "  入口：提交 0x%08X / 清空 0x%08X（唯一调用点 0x%08X）/ 取首 0x%08X / 重挂 0x%08X"
        % (SUBMIT, CLEAR, CLEAR_CALLER, PEEK, REHANG),
    ])
