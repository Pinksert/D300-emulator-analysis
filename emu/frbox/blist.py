# -*- coding: utf-8 -*-
"""blist.py —— B 段的循环双链表原语（V33 实测，修正 V31/V32 两处说法）。

两个原语都在 ROM 里，都是**间接调用**（`ldi:32 r12,#addr` + `call @r12`）——
用 rel12 相对调用去扫会得到「0 个调用点」的假结论（V31 就这么错过了它们）。

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
    另外两张      head = 0x9F69CEF4 / 0x9F69CEEC —— 它们的 init 调用点（0x12115E /
                  0x12116C）在 0x121xxx 页，本场景**一次都没执行**，两个表头仍是 0
"""

LIST_INIT = 0x000124F46
LIST_INSERT = 0x000124F5A

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
