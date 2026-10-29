#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_task2.py —— 直接跑 A 段应用区（任务 1 入口 0x8133A）

要点：
  · 不在启动链上注入（启动链跑完必弹 PC=0），直接在**干净机器**上跳进应用区
  · 同时在**启动链跑完 BSS 清零后**（停在 0x80506）跳进去，对比两者差别
  · 统计：执行步数 / 不同 PC / EIT（μITRON 服务）次数 / 0x3F000 回调表是否被填
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
import fr_svc
from frbox.machine import create

NAMES = fr_svc.load_props()
TASK1 = 0x0008133A


def run_to(m, pcs, limit=400000):
    """跑到 pcs 里任一 PC 停下"""
    c = m.cpu
    for i in range(limit):
        if c.pc in pcs:
            return c.pc, i
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    return None, limit


def trial(tag, prime):
    m = create("d300-a")
    m.load()
    if prime:
        where, n = run_to(m, {0x00080506})
        print("  [铺垫] 跑到 0x%06X，用了 %s 步" % (where or 0, format(n, ",")))

    c = m.cpu
    c.pc = TASK1
    c.r[15] = 0x0004616C
    c.r[4] = 0
    c.halted = False

    cnt = {}
    orig = fr_emu.Fr.eit

    def patched(self, vec, level=None):
        cnt.setdefault(vec, []).append(self.pc)
        return orig(self, vec, level)

    fr_emu.Fr.eit = patched
    pcs = set()
    steps0 = m.step_count
    low = None
    try:
        for i in range(300000):
            p = c.pc
            pcs.add(p)
            if p < 0x1000:
                low = (p, i, "进低地址")
                break
            c.step()
            m.step_count += 1
            if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
                for d in m.devices:
                    d.tick(m.cycles_per_tick)
    finally:
        fr_emu.Fr.eit = orig

    print("=" * 74)
    print("%s" % tag)
    print("  执行 %s 步 / 不同 PC %s / 停 0x%06X%s" % (
        format(m.step_count - steps0, ","), format(len(pcs), ","),
        c.pc, ("  ⚠ " + low[2]) if low else ""))
    if cnt:
        print("  μITRON 服务调用（int #0x40）×%d:" % sum(len(v) for v in cnt.values()))
        for vec in sorted(cnt):
            sites = cnt[vec]
            print("    vec 0x%02X ×%-3d 首处 0x%06X 末处 0x%06X"
                  % (vec, len(sites), sites[0], sites[-1]))
    else:
        print("  μITRON 服务调用：0 次")
    # 回调表
    hit = sorted({a for a, *_ in m.bus.all_writes if 0x3F000 <= a <= 0x3F0FF})
    print("  0x3F000 回调表被写 %d 个槽%s" % (
        len(hit), ("：" + " ".join("0x%03X" % a for a in hit[:16])) if hit else ""))
    # 落点分布
    buckets = {}
    for p in pcs:
        k = p >> 12
        buckets[k] = buckets.get(k, 0) + 1
    top = sorted(buckets.items(), key=lambda x: -x[1])[:8]
    print("  PC 分布（按 4KB 页）:", " ".join("0x%02X000:%d" % (k, v) for k, v in top))
    return m, c, pcs


trial("试验A 干净机器直接跳任务1", prime=False)
print()
trial("试验B 启动链跑到 0x80506 后跳任务1", prime=True)
