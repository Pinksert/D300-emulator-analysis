#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_native_drive.py —— 用固件自己的内核服务注入事件，把停产的系统一路推下去

V12 的关键能力：**不碰外设，也能给内核送事件**。

做法不是改内存，而是「当场调一次固件的系统调用」：

    r12 = 服务号（32 位补码，-48）
    r4..r7 = 参数
    eit(0x40)              → 固件自己的陷阱入口 0xFE3C0 → 分发表 0xFE3B8 → 真实现
    reti 回到标记地址 0x81338（`bra -1`）→ 确认返回

这跟「按一下快门 / 插一张卡」在固件看来是同一件事：内核自己把等待的任务挂回
就绪队列、自己触发调度。差别只在于事件源是我们而不是硬件 —— 所以报告里必须写明
「注入」，不能写成「相机响应了按键」。

策略（贪心，可复现）：
  1. 跑到停机（空闲任务连续两轮、无新服务调用）；
  2. 读当前所有等待中的任务，挑「任务号最小」的那个；
  3. 如果它等的是事件标志，就 `set_flg(flag, waiptn)`；
  4. 回到 1，直到没有等待者 / 事件用完 / 步数用完。

判据：
  「推进了」= 注入后有新的 int #0x40，或又有任务变成激活（sta_tsk 真的跑了）。

用法：cd emu && python probe/exp_native_drive.py [--events N] [--budget N]
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import realos

IDLE_PC = 0x000FE510
SAFE_PC = 0x00081338          # `bra -1`，只当「系统调用已返回」的标记
CUR_TCB = realos.CUR_TCB


def ctx_ids(m):
    return [t["id"] for t in realos.states(m) if t["pc"]]


def rd(m, a, n=4):
    d = m.fetch_bytes(a, n)
    return int.from_bytes(d, "big") if len(d) == n else None


def kernel_call(m, svc, r4=0, r5=0, r6=0, r7=0, r13=0, max_steps=50000):
    """在固件自己的内核里执行一次系统调用（不经过任何半主机模型）。"""
    c = m.cpu
    saved = (list(c.r), list(c.dr), c.ps, c.pc, c.delay, c.halted)
    c.r[4], c.r[5], c.r[6], c.r[7], c.r[13] = r4, r5, r6, r7, r13
    c.r[12] = svc & 0xFFFFFFFF
    c.pc = SAFE_PC
    c.eit(0x40)
    n = 0
    while n < max_steps and not c.halted:
        c.step()
        n += 1
        m.step_count += 1
        if c.pc == SAFE_PC and n > 1:
            break
    r12 = c.r[12]
    c.r[:], c.dr[:], c.ps, c.pc, c.delay, c.halted = saved
    return {"steps": n, "err": r12 - 0x100000000 if r12 & 0x80000000 else r12}


def to_stall(m, budget=400000, chunk=5000, settle=3, quiet=60000):
    """跑到「没动静」为止，返回停下时的性质。

    V12 的教训：光等「空闲任务」不够 —— 任务 10 被拉起后会长时间在做真实工作
    （对 0x534 握手 2,129 次），永远不回空闲，但它不是死锁。所以判据是二选一：

        'idle'   空闲任务连续 settle 轮 + 无新服务调用   → 所有任务都阻塞了
        'quiet'  连续 quiet 步没有新的 int #0x40        → 还在跑，但不再进内核
        'budget' 步数用完
    """
    n0 = m.cpu.int40
    idle = 0
    last_svc_step = m.step_count
    while m.step_count < budget:
        m.run(max_steps=chunk, spin_guard=False)
        if m.cpu.halted or m.fault:
            return "halt"
        if m.cpu.int40 != n0:
            n0 = m.cpu.int40
            last_svc_step = m.step_count
            idle = 0
            continue
        if m.cpu.pc == IDLE_PC:
            idle += 1
            if idle >= settle:
                return "idle"
        else:
            idle = 0
            if m.step_count - last_svc_step >= quiet:
                return "quiet"
    return "budget"


def main():
    events = 40
    budget = 4000000
    if "--events" in sys.argv:
        events = int(sys.argv[sys.argv.index("--events") + 1])
    if "--budget" in sys.argv:
        budget = int(sys.argv[sys.argv.index("--budget") + 1])

    m = create("d300-a")
    m.load()
    m.boot_realos(1)
    to_stall(m)
    print("初始死锁：%d 步，PC=0x%08X，服务调用 %d 次，有上下文的任务 %s"
          % (m.step_count, m.cpu.pc, m.cpu.int40, ctx_ids(m)))
    print("  在等：%s" % [(b["task"], "flag%d/0x%X" % (b["flag"], b["wants"] & 0xFFFFFFFF)
                         if b["kind"] == "flag" else "对象 0x%05X" % b["obj"])
                        for b in realos.blockers(m)])

    n0 = m.cpu.int40
    log = []
    served = {}                    # (task, flag, mask) -> 注入次数：同一个等待最多喂两次
    pages = set()
    pcs = {}                       # 每个任务被看到过的 PC（判断它到底跑没跑）
    ever = {}                      # 任务 -> 是否曾经有过现场（含后来退出的）
    for i in range(events):
        if m.step_count > budget:
            break
        bl = [b for b in realos.blockers(m) if b["kind"] == "flag"]
        bl = [b for b in bl
              if served.get((b["task"], b["flag"], b["wants"] & 0xFFFFFFFF), 0) < 2]
        if not bl:
            print("\n没有还需要喂的 flag 等待了")
            break
        b = sorted(bl, key=lambda x: x["task"])[0]
        key = (b["task"], b["flag"], b["wants"] & 0xFFFFFFFF)
        served[key] = served.get(key, 0) + 1
        wants = b["wants"] & 0xFFFFFFFF
        r = realos.set_flag(m, b["flag"], wants)
        before_svc, before_ctx = m.cpu.int40, ctx_ids(m)
        got = to_stall(m, budget=min(budget, m.step_count + 300000))
        pages.add(m.cpu.pc & 0xFFFFF000)
        for t in realos.states(m):
            if t["pc"]:
                pcs.setdefault(t["id"], set()).add(t["pc"])
                ever[t["id"]] = True
        after_ctx = ctx_ids(m)
        new = [t for t in after_ctx if t not in before_ctx]
        svc = m.cpu.int40 - before_svc
        log.append((i + 1, b["task"], b["flag"], wants, r["err"], svc, new,
                    m.cpu.pc, m.step_count))
        print("  #%-2d set_flg(flg%d, 0x%X) 给任务 %d → err=%d，新增服务 %d 次，"
              "新激活 %s，步数 %d%s"
              % (i + 1, b["flag"], wants, b["task"], r["err"], svc,
                 new or "-", m.step_count, "" if got == "idle" else "（%s）" % got))
        if m.cpu.halted or m.fault:
            print("    停机/故障：", m.cpu.halted, m.fault)
            break
        if got == "quiet" and not new:
            # 系统在跑但不再进内核：再多推也没意义，除非还有别的等待者
            pass

    print("\n推进结果：共注入 %d 次事件，累计 %d 步、%d 次服务调用"
          % (len(log), m.step_count, m.cpu.int40))
    st = realos.states(m)
    print("  曾有现场的任务（含中途退出的）：%s" % sorted(ever))
    print("  现在还有现场：%s" % [t["id"] for t in st if t["pc"]])
    print("  真跑过的任务（见过 ≥2 个不同 PC）：%s"
          % sorted(t for t, s2 in pcs.items() if len(s2) >= 2))
    print("  休眠态（状态字低字节 0x10，含「从未启动」与「已退出」）：%s"
          % [t["id"] for t in st if t["dormant"]])
    print("  当前在跑：%s" % [t["id"] for t in st if t["current"]])
    if m.xfer is not None:
        print("  握手寄存器：启动 %d 次 / 完成 %d 次" % (m.xfer.starts, m.xfer.completions))
    print("  仍在等待：%s" % [(b["task"], "flag%d/0x%X" % (b["flag"], b["wants"] & 0xFFFFFFFF)
                          if b["kind"] == "flag" else "对象 0x%05X" % b["obj"])
                         for b in realos.blockers(m)])
    print("  任务各自的 PC 页：%s" % " ".join(
        sorted({"0x%05X" % (t["pc"] & 0xFFFFF000) for t in st if t["pc"]})))
    print("  注入时正在执行的页：%s" % " ".join("0x%05X" % p for p in sorted(pages)))


if __name__ == "__main__":
    main()
