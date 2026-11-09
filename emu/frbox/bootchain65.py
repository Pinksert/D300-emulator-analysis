# -*- coding: utf-8 -*-
"""frbox.bootchain65 —— V65：任务 2 开机装配链与 ROM 表扫描循环（纯静态）。

承接 KI#248。V61–V64 四版一直把末 PC ``0x1A4E58`` 当成「整机止点」，
V65 反查任务 2 入口 ``0x1200AC`` 的动态轨迹后确认：那是开机装配函数
第一条 ``call @r12``（``0x1200D0`` → ``0x1A4DD2``）内部的一段
**确定性有限 ROM 表扫描循环**，不是等待外部输入的忙等假墙。

本模块只读 ROM，不建机器、不挂设备、不写内存。三条口径纪律：

* ``add2`` 的 ``m4`` 是 **4 位负立即数**：``fr_emu.py:454`` 执行
  ``(m4 - 16)``，所以 ``add2 #0xC`` 是 ``-4``，不是 ``+12``。
  读反汇编文本判循环方向/长度前必须回执行式。
* ``rel9`` 是 **8 位有符号**（硬规矩 44），``0xFD`` 是 ``-3``。
* 带 ``:d`` 的分支后面一句是延迟槽（硬规矩 19），``bgt:d`` 的
  ``st`` 每轮都执行，不因跳转而跳过。

另：``bgt`` 判的是 CCR（``fr_emu.py:815`` 的
``gt = not ((v ^ n) or z)``），而 ``ld`` 不更新 CCR
（``fr_emu.py:680`` 只写目标寄存器）。所以这段循环的条件来自
``add2`` 的结果，**与 ``ld`` 读到的 ROM 值无关**。
"""

from . import dispatch62 as D

# ---------------------------------------------------------------- 常量

#: 任务 2 开机装配函数（任务表 0x054C00 的 +0x10 字段）
BOOT_TASK2 = 0x1200AC
BOOT_ENTER = 0x1200B0
BOOT_RET = 0x12046E
BOOT_INSNS = 353
BOOT_CALLS = 32
BOOT_SVC = 26

#: 入口后第一道门：读 0x9F58D08F，非零才继续，否则 jmp 0x120302
BOOT_GATE_LOAD = 0x120170
BOOT_GATE_CMP = 0x120178
BOOT_GATE_BRANCH = 0x12017A
BOOT_GATE_JMP = 0x120182
BOOT_GATE_BYTE = 0x9F58D08F

#: 第一条 call 及其目标
FIRST_CALL_PC = 0x1200D0
FIRST_CALLEE = 0x1A4DD2

#: ROM 表扫描循环（0x1A4E50 装 r13 初值，0x1A4E54 起四指令循环）
POLL_INIT = 0x1A4E50
POLL_PCS = (0x1A4E54, 0x1A4E56, 0x1A4E58, 0x1A4E5A)
POLL_ADD2 = 0x1A4E54
POLL_LD = 0x1A4E56
POLL_BRANCH = 0x1A4E58
POLL_DELAY_ST = 0x1A4E5A
POLL_EXIT = 0x1A4E5C
POLL_REL9 = 0xFD
POLL_TARGET = 0x1A4E54

#: 循环参数（动态寄存器轨迹实证，见 results/frbox/boot65.txt）
M4_FIELD = 0x0C
M4_DELTA = -4
R13_INIT = 0x6B1C
R6_BASE = 0x001D7124
SCAN_TOP = 0x001DDC3C
LOOP_ROUNDS = 6855          # 0x6B1C / 4
LOOP_STEPS = 27420          # 6855 轮 × 4 条指令

#: 同一子函数里另外两段同构轮询（V65 基线窗口内都没跑到）
SIBLING_POLLS = ((0x1A4E46, 0x1A4E48, 0x1A4E4A),
                 (0x1A4E6A, 0x1A4E6C, 0x1A4E6E))

#: 任务 2 装配函数里三道 wai_flg 与一道 dly_tsk
WAIT_PCS = (0x12027E, 0x1202F4, 0x120364, 0x1203BE)


# ---------------------------------------------------------------- 纯函数

def m4_delta(m4):
    """``add2/addn2/sub2`` 的 4 位立即数 → 有符号增量。

    与 ``fr_emu.py:454/463/487`` 的 ``(m4 - 16)`` 逐字一致。
    ``m4=0x0`` 是 ``-16``、``m4=0xF`` 是 ``-1``，恒为负。
    """
    return (m4 & 0xF) - 16


def scan_window(r13_init=R13_INIT, delta=M4_DELTA, r6=R6_BASE,
                elem=4, insns_per_round=4):
    """按寄存器轨迹推算扫描首地址、轮数与所需步数。

    ``ld @(R13,Rj)`` 的地址是 ``r13 + r[rj]``（``fr_emu.py:654-655``），
    首轮执行完 ``add2`` 后 r13 已变，所以首地址是
    ``r13_init + delta + r6``。退出条件是 ``add2`` 结果归零使 ``z=1``，
    即 r13 递减到 0。
    """
    rounds = r13_init // (-delta)
    first = (r13_init + delta + r6) & 0xFFFFFFFF
    # 末轮 r13 递减到 0，地址就是 r6 本身（不是 r6 + delta*rounds）。
    last = r6 & 0xFFFFFFFF
    return {
        "first": first,
        "last": last,
        "rounds": rounds,
        "span": (first - last) if first >= last else 0,
        "steps": rounds * insns_per_round,
    }


def rel9_target(pc, rel9):
    """委托 ``dispatch62``（V64 已修成与 CPU 逐字一致的 8 位有符号）。"""
    return D.rel9_target(pc, rel9)


# ---------------------------------------------------------------- ROM 解析

def poll_loop(rom, base, lo=POLL_ADD2, hi=POLL_EXIT):
    """解析四指令扫描循环，返回形态、位移与回跳目标。"""
    ins = D.decode_range(rom, base, lo, hi)
    by_pc = dict((x.pc, x) for x in ins)
    add2 = by_pc.get(POLL_ADD2)
    ld = by_pc.get(POLL_LD)
    br = by_pc.get(POLL_BRANCH)
    st = by_pc.get(POLL_DELAY_ST)
    m4 = add2.vals.get("m4") if add2 else None
    rel9 = br.vals.get("rel9") if br else None
    win = scan_window(delta=m4_delta(m4)) if m4 is not None else {}
    return {
        "insns": [x.mn for x in ins],
        "add2_m4": m4,
        "add2_delta": m4_delta(m4) if m4 is not None else None,
        "ld_form": "r13 + r[rj]" if ld else None,
        "ld_rj": ld.vals.get("rj") if ld else None,
        "branch_rel9": rel9,
        "branch_target": rel9_target(POLL_BRANCH, rel9) if rel9 is not None else None,
        "delay_slot": st.mn if st else None,
        "window": win,
    }


def boot_assembly(rom, base, lo=BOOT_TASK2, hi=BOOT_RET + 2):
    """线性反汇编任务 2 开机装配函数，返回指令、调用与服务点。"""
    ins = D.decode_range(rom, base, lo, hi)
    calls = D.call_targets(ins)
    return {
        "insns": [x.mn for x in ins],
        "pcs": [x.pc for x in ins],
        "calls": calls,
        "svc_pcs": [x.pc for x in ins if x.mn == "int"],
        "enters": [x.pc for x in ins if x.mn == "enter"],
        "rets": [x.pc for x in ins if x.mn.startswith("ret")],
        "first_calls": [c for c in calls if c.pc < 0x120154],
    }


def launch_map(calls, launchers, window=0x80):
    """把 ``call`` 目标映射到其后 ``window`` 字节内最近的 ``sta_tsk`` 点。

    V64 只看 ``enclosing()`` 的分散度，得出「链式自启动、无集中总装机」；
    V65 从调用者一侧看，发现这些启动器由任务 2 装配函数逐个调用。
    """
    out = []
    for c in calls:
        near = [l for l in launchers if c.target <= l.pc < c.target + window]
        if near:
            l = min(near, key=lambda x: x.pc)
            out.append((c.pc, c.target, l.pc, l.tid, l.pc - c.target))
    return out


def wait_points(sweep, lo=BOOT_TASK2, hi=BOOT_RET):
    """区间内的服务调用（含三道 wai_flg 与一道 dly_tsk）。"""
    return [c for c in sweep.calls if lo <= c.addr < hi]
