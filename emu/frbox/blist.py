# -*- coding: utf-8 -*-
"""blist.py —— B 段的循环双链表原语（V33 实测，修正 V31/V32 两处说法）。

两个原语都在 ROM 里，都是**间接调用**（`ldi:32 r12,#addr` + `call @r12`）——
用 rel12 相对调用去扫会得到「0 个调用点」的假结论（V31 就这么错过了它们）。

零、四件套（V34 补全，全部单元调用验证过）
    0x124F46  init(head)            [head] = head; [head+4] = head
    0x124F4E  is_empty(head)        返回 1 = 空、0 = 非空；**不改内存**
    0x124F5A  insert(head, node)    插到哨兵之后
    0x124F7A  unlink(node)          r4 = 节点本身，把前后指针互接
    ⚠️ V32 把 0x124F46 当成「摘 head」——真正的摘链是 0x124F7A。

一、0x124F46 = 初始化空表（哨兵自指）
    单元调用实测（r4 = 表头地址 HEAD）：
        [HEAD+0] = HEAD
        [HEAD+4] = HEAD
    → 空表 = 哨兵指向自己。V32 把它当成「摘 head」是错的。

二、0x124F5A = 插到哨兵之后（头插）
    单元调用实测（r4 = HEAD，r5 = NODE，HEAD 未初始化时 [HEAD] = 0xA0A1A2A3）：
        [NODE+4] = HEAD              node->prev = 哨兵
        [NODE+0] = [HEAD+0]          node->next = 旧首节点
        [HEAD+0] = NODE              head->next = node
        [旧首节点 + 4] = NODE        旧首->prev = node
    先 init 再 insert 的结果：head 区 = (NODE, NODE)、node 区 = (HEAD, HEAD)
    —— 单元素循环表，形状完全对得上。

三、节点布局
    +0x00  next
    +0x04  prev（V31 看到的「+4 也是节点地址」其实就是 prev：空表时是哨兵地址）
    +0x08  与链表无关（显示队列里是别的字段）
    +0x09  标志字节（显示队列里由生产端 0x12E740 置 0x80/0xC0）

四、已知的表
    显示请求队列  head = 0x9F58D500（**本场景没被 init 过**，初始 [head] = 0）
                  它唯一的 init 调用点在「清空队列」函数 0x12E610 里，那个函数没执行
    另外两张      head = 0x9F69CEF4 / 0x9F69CEEC —— 它们的 init 调用点（0x12115E /
                  0x12116C）在 0x121xxx 页，本场景**一次都没执行**，两个表头仍是 0
"""

LIST_INIT = 0x000124F46
LIST_IS_EMPTY = 0x000124F4E
LIST_INSERT = 0x000124F5A
LIST_UNLINK = 0x000124F7A

N_NEXT = 0x00
N_PREV = 0x04
N_FLAG = 0x09

DISPLAY_QUEUE_HEAD = 0x9F58D500
OTHER_HEADS = (0x9F69CEF4, 0x9F69CEEC)


def is_empty(m, head):
    """空表判定：哨兵的两个指针都指向自己。"""
    return (m.bus.rd(head, 4) == head) and (m.bus.rd(head + 4, 4) == head)


def first(m, head):
    """第一个节点（空表时返回 None）。"""
    n = m.bus.rd(head, 4)
    return None if n in (0, head) else n


def walk(m, head, limit=256):
    """从哨兵往后走一圈，返回节点地址列表（带环路保护）。"""
    out, seen = [], set()
    n = m.bus.rd(head, 4)
    while n not in (0, head) and n not in seen and len(out) < limit:
        out.append(n)
        seen.add(n)
        n = m.bus.rd(n + N_NEXT, 4)
    return out


def back_walk(m, head, limit=256):
    """从哨兵往前走一圈（prev 链）。"""
    out, seen = [], set()
    n = m.bus.rd(head + 4, 4)
    while n not in (0, head) and n not in seen and len(out) < limit:
        out.append(n)
        seen.add(n)
        n = m.bus.rd(n + N_PREV, 4)
    return out


def is_empty_rom(m, head):
    """跑 ROM 里的判空例程（0x124F4E），按它的返回值判空。

    注意：这是**执行固件代码**（把 PC 指过去跑几条），与 is_empty() 的纯读法不同。
    实测：空表返回 1、非空返回 0，且不改内存。
    """
    c = m.cpu
    keep = (list(c.r), list(c.dr), c.ps, c.pc, c.delay, c.halted)
    c.r[4] = head
    c.pc = LIST_IS_EMPTY
    n = 0
    while n < 16 and not c.halted and c.pc != keep[3]:
        m.step(1)
        n += 1
    out = c.r[4]
    c.r[:], c.dr[:], c.ps, c.pc, c.delay, c.halted = keep
    return out


def init_model(m, head):
    """**按实测语义实现**的空表初始化（模型，不执行固件代码）。"""
    m.bus.wr(head + N_NEXT, 4, head)
    m.bus.wr(head + N_PREV, 4, head)
    return head


def insert_model(m, head, node):
    """**按实测语义实现**的「插到哨兵之后」（模型，不执行固件代码）。"""
    old = m.bus.rd(head + N_NEXT, 4)
    m.bus.wr(node + N_PREV, 4, head)
    m.bus.wr(node + N_NEXT, 4, old)
    m.bus.wr(head + N_NEXT, 4, node)
    if old:
        # ROM 是无条件写 [old+4]；old == head（空表）时这一笔正好补上 head->prev。
        # old == 0 时固件会写到地址 4（野写），模型不复制这个行为。
        m.bus.wr(old + N_PREV, 4, node)
    return node


def unlink_model(m, node):
    """**按实测语义实现**的摘链（模型）：r4 = 节点本身。

    实测 0x124F7A：3 节点链 C→B→A 摘掉 B 之后 walk 变成 [C, A]。
    ⚠️ 传哨兵进去会破坏链（固件不检查参数）。
    """
    nxt = m.bus.rd(node + N_NEXT, 4)
    prv = m.bus.rd(node + N_PREV, 4)
    m.bus.wr(nxt + N_PREV, 4, prv)
    m.bus.wr(prv + N_NEXT, 4, nxt)
    return node


def clear_model(m, head, slots=70, stride=0x0C, flag_off=0x09):
    """**按 0x12E602 实测语义实现**的「清空队列」：init 哨兵 + 清每个槽位的标志字节。

    真实函数还多做一件事：清完之后 set_flg(3, 0x80) 通知主循环。
    """
    init_model(m, head)
    for i in range(slots):
        m.bus.wr(head + 8 + i * stride + flag_off, 1, 0)
    return head


def describe(m, head):
    """一句话描述某条链的现状（打印用）。"""
    if is_empty(m, head):
        return "head %08X：空表（哨兵自指）" % head
    fwd = walk(m, head)
    back = back_walk(m, head)
    return "head %08X：%d 个节点（前向 %s%s，反向 %d 个%s）" % (
        head, len(fwd), " ".join("%X" % x for x in fwd[:4]),
        " …" if len(fwd) > 4 else "", len(back),
        "" if len(fwd) == len(back) else " ← 两向数量不一致，链不完整")
