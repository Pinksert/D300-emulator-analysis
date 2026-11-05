# -*- coding: utf-8 -*-
"""bflag.py —— B 段 REALOS 的事件标志对象与 wai/set/clr 语义（V24 实测）。

V23 收尾时留了两条边界：「flag 3 位 0x200000 的置位者未证」和
「wai_flg mode=2 立即返回 E_OK 的语义未证」。V24 把这两条都落到了代码上：

一、flag 对象的真实布局（0x680008B8 起，步长 0x10）
    +0x00 自指指针（= 本对象地址）
    +0x04 等待中的 TCB（没人等时为 0 或自指）
    +0x08 32 位位模式（set_flg 置位、clr_flg 清位、wai_flg 读它判条件）
    +0x0C 背指对象表 0x551B0 的对应表项
    对象表 @0x551B0，每项 16 字节，第 N 项对应 flag N：
      +0x0  第一项（flag 1）是 0x00110010，其余项为 0 —— 含义未证
      +0x4  对象指针（本模块用它索引）
      +0x8  高 16 位 = flag 号（0x00030000 即 flag 3）
    例：flag 3 → 表项 0x551D0（对象指针在 0x551D4）→ 对象 0x680008D8
        → 位模式 @0x680008E0。

二、wfmode 的真语义（与教科书的 μITRON 相反，别照抄）
    mode 取值 0..3（>3 走 E_PAR 分支）
      bit0 (1) = 满足后清位（把 waiptn 里等到的位从位模式里清掉）
      bit1 (2) = OR 等待（任一位成立即可）；否则是 AND 等待（全都要成立）
    mode=0 AND 不清 / 1 AND 清 / 2 OR 不清 / 3 OR 清。
    实测：mode=1、3 跑完后位模式确实被清；AND 不满足时处理函数会走
    「登记等待 → 请求一次重调度 → 复查」的兜底路径（多约 50 步）。

三、所谓「立即返回 E_OK」的真相
    wai_flg 在条件不满足时不会无限阻塞：它把等待位登记到 TCB，
    请求一次重调度；如果没有别的任务可跑，内核原地把 CPU 还给调用者，
    处理函数复查后按「没等到」返回。调用方（任务 2 的主事件循环）
    因此是**忙轮询**：clr_flg → wai_flg → 下一圈。
    所以 r12 = 0 只代表「内核受理了这次调用」，不代表「等到了」——
    V23 报告里写的「立即返回 E_OK」就是这个意思，V24 把它说准。

边界（写报告必须带上）：
  * 一切 set/clr/wait 都是**替固件调内核**，不是硬件或别的任务真的产生了事件；
  * 对象布局是 1.11 实测，别的版本未验证；
  * 位模式是 32 位，但静态扫描里出现的常数都只用低 22 位。
"""

from . import brealos

FLAG_TABLE = 0x000551B0     # 事件标志对象表（每项 16 字节，首字 = 对象指针）
FLAG_TABLE_STRIDE = 0x10
FLAG_BASE = 0x680008B8      # 对象区起点（flag 1）；flag N = BASE + (N-1)*0x10
FLAG_STRIDE = 0x10
FLAG_COUNT = 17

F_SELF = 0x00               # 自指指针
F_WAITER = 0x04             # 等待中的 TCB
F_PATTERN = 0x08            # 32 位位模式
F_BACKPTR = 0x0C            # 背指对象表项的「对象指针字段」（表项 +4）

T_OBJ = 0x04                # 表项内对象指针的偏移
T_IDWORD = 0x08             # 表项内「id << 16」的偏移

# wfmode 位（实测，见模块注释）
WF_ANDW = 0x00              # 全都要成立
WF_ORW = 0x02               # 任一位成立（注意不是教科书的 0x01）
WF_CLR = 0x01               # 满足后清位（注意不是教科书的 0x02）

SVC_SET_FLG = -48
SVC_CLR_FLG = -47
SVC_WAI_FLG = -46


def object_addr(flgid):
    """flag N 的对象地址。"""
    return FLAG_BASE + (flgid - 1) * FLAG_STRIDE


def table_entry(m, flgid):
    """从对象表里读第 flgid 项存的对象指针（固件自己的索引路径）。"""
    return m.bus.rd(FLAG_TABLE + (flgid - 1) * FLAG_TABLE_STRIDE + T_OBJ, 4)


def table_entry_addr(flgid):
    """第 flgid 项的表项基址（对象 +0xC 背指的就是它的 +4）。"""
    return FLAG_TABLE + (flgid - 1) * FLAG_TABLE_STRIDE


def table_idword(m, flgid):
    """表项里那个「id << 16」的字（核对表项归属用）。"""
    return m.bus.rd(table_entry_addr(flgid) + T_IDWORD, 4)


def pattern(m, flgid):
    """读位模式（32 位）。"""
    return m.bus.rd(object_addr(flgid) + F_PATTERN, 4)


def set_pattern(m, flgid, ptn):
    """直接改位模式 —— 只用于搭场景，正式语义请用 set()。"""
    m.bus.wr(object_addr(flgid) + F_PATTERN, 4, ptn & 0xFFFFFFFF)


def waiter(m, flgid):
    """当前在等这个 flag 的 TCB（没人等时通常是自指或 0）。"""
    return m.bus.rd(object_addr(flgid) + F_WAITER, 4)


def objects(m):
    """17 个 flag 对象的快照：id / 对象地址 / 表项指针 / 等待 TCB / 位模式。"""
    out = []
    for i in range(1, FLAG_COUNT + 1):
        a = object_addr(i)
        out.append({"id": i, "addr": a, "entry": table_entry(m, i),
                    "self": m.bus.rd(a + F_SELF, 4),
                    "waiter": m.bus.rd(a + F_WAITER, 4),
                    "pattern": m.bus.rd(a + F_PATTERN, 4),
                    "back": m.bus.rd(a + F_BACKPTR, 4)})
    return out


def set(m, flgid, ptn):
    """走固件内核 set_flg(flgid, ptn)：位模式或上 ptn，并唤醒等待者。"""
    return brealos.kernel_call(m, SVC_SET_FLG, r4=flgid, r5=ptn)


def clr(m, flgid, ptn):
    """走固件内核 clr_flg(flgid, ptn)：位模式与上 ptn（通常传 ~位 的掩码）。"""
    return brealos.kernel_call(m, SVC_CLR_FLG, r4=flgid, r5=ptn)


def wait(m, flgid, ptn, mode=WF_ANDW, result_ptr=0x9F58D0F0, max_steps=4000):
    """走固件内核 wai_flg(flgid, waiptn, wfmode)。

    返回 {'err', 'steps', 'satisfied'}：
      * err       = 内核返回码（0 = 受理；-69 之类是参数/对象错误）
      * steps     = 这次调用走了多少步（满足 ≈100 步、不满足 ≈150 步，
                    多出来的那段是「登记等待 + 请求一次重调度」的兜底路径）
      * satisfied = 调用前后位模式是否仍覆盖 waiptn（AND 或 OR 口径按 mode 判）

    ⚠️ err=0 不等于「等到了」：条件不满足时内核原地把 CPU 还给调用者，
    返回码只是「受理」。要判断有没有等到，看 satisfied。
    """
    before = pattern(m, flgid)
    r = brealos.kernel_call(m, SVC_WAI_FLG, r4=result_ptr, r5=flgid,
                            r6=ptn, r7=mode, max_steps=max_steps)
    after = pattern(m, flgid)
    if mode & WF_ORW:
        sat = (before & ptn) != 0
    else:
        sat = (before & ptn) == ptn
    return {"err": r["err"], "steps": r["steps"], "satisfied": sat,
            "before": before, "after": after}


def describe(m, flgid=3):
    """一句话状态：谁在等、等哪一位、当前位模式。"""
    a = object_addr(flgid)
    return {"id": flgid, "addr": a, "pattern": pattern(m, flgid),
            "waiter_tcb": waiter(m, flgid),
            "entry": table_entry(m, flgid)}
