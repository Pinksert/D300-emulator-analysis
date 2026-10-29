#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_task1.py —— 手工把 A 段的应用任务拉起来

背景（V6 + 本轮）：
  · 复位链第三跳 0x804FA = 纯 ret（四个版本都一样）→ REALOS 调度器没启动
  · main 只有 BSS 清零 + 一条空 call（0x80520）→ 内核对象建好、状态 0
  · 任务表 @0xFE00C，任务 1 的入口 = 0x0008133A（第一句就是 stm1/enter 的标准序言）
  · 0x81338 的 bra -1 是入口前的停机占位，0x81336 的 ret 是 ISR 默认处理

本实验跳过 REALOS，直接构造任务 1 的初始上下文并跳进去，
量四件事：能跑多少步 / PC 覆盖多少 / 触发几次 int#0x40 / 会不会自己调 sys_sta_tsk
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

import fr_emu
from frbox.machine import create

TASK1 = 0x0008133A
USER_SP = 0x0004616C        # 复位入口 0xFC000 设定的用户栈（增长向下）

SVC = {}


def svc_name(n):
    if n in SVC:
        return SVC[n]
    return "%d" % n


def run_trial(tag, pc, sp, arg=0, max_steps=400000, watch_svc=True):
    m = create("d300-a")
    m.load()
    # 先让启动链跑到 main 里（BSS 清完、内核对象建好）
    m.run(44346)

    c = m.cpu
    c.pc = pc
    c.r[15] = sp
    c.r[4] = arg
    c.r[5] = 0
    c.halted = False

    cnt = {}
    orig_eit = fr_emu.Fr.eit

    def patched(self, vec, level=None):
        cnt[vec] = cnt.get(vec, 0) + 1
        return orig_eit(self, vec, level)

    fr_emu.Fr.eit = patched
    pcs = set()
    spin = None
    try:
        for i in range(max_steps):
            p = c.pc
            pcs.add(p)
            if c.halted:
                break
            # 自旋检测
            if len(pcs) and i % 4096 == 0:
                pass
            c.step()
            m.step_count += 1
            if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
                for d in m.devices:
                    d.tick(m.cycles_per_tick)
            if p < 0x1000:
                spin = ("jumped-low", p, i)
                break
    finally:
        fr_emu.Fr.eit = orig_eit

    print("=" * 74)
    print("%s  入口 0x%06X  SP=0x%06X  r4=0x%X" % (tag, pc, sp, arg))
    print("  执行 %s 步，不同 PC %s，停 0x%06X%s" % (
        format(m.step_count - 44346, ","), format(len(pcs), ","), c.pc,
        "  halted" if c.halted else ""))
    if spin:
        print("  ⚠ 跑飞到低地址: %s" % (spin,))
    print("  EIT:", dict(cnt) or "0 次")
    if cnt:
        for vec in sorted(cnt):
            print("    vec 0x%02X × %d" % (vec, cnt[vec]))
    # 覆盖率
    print("  写过 0x3F000 区:", any(0x3F000 <= a <= 0x3F0FF for a, *_ in m.bus.all_writes))
    hit = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    if hit:
        print("    命中槽:", " ".join("0x%03X" % a for a in hit[:20]))
    print("  未映射写 %d 次" % len(m.bus.unmapped_writes))
    return m, c, pcs


# --- 试验 1：直接跳任务 1 入口 ---
run_trial("试验1 直接跳任务1", TASK1, USER_SP)

# --- 试验 2：换几个 r4 参数值看行为差别 ---
for arg in (0, 1, 2, 0x10):
    run_trial("试验2 r4=%d" % arg, TASK1, USER_SP, arg=arg, max_steps=120000)

# --- 试验 3：看看 0x81302 是什么（任务1 的第一个 call 目标） ---
m = create("d300-a")
m.load()
print("=" * 74)
print("--- 0x81300 起 16 条（0x81302 是任务1 第一个 call 的目标）---")
print(m.disasm_at(0x00081300, 16))
