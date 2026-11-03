# -*- coding: utf-8 -*-
"""exp_msg_slot.py —— 任务 5 收到消息后往哪儿写？（V17 第 6 步）

反汇编已经指出任务 5 是**回调注册器**而不是数据处理者：

    0x0A6304  r8  = 0x3FE48
    0x0A6308  r9  = 0x3FF99
    0x0A630C  r10 = 0x3F054      ← 回调槽区（0x3F01C + (vec-0x10)*4）
    0x0A6310  r11 = 0x3F074
    0x0A631A  r12 = -61          ← rcv_msg 阻塞等消息
    0x0A6326  … 一连串 ld/lduh/sth：把消息字段搬到固定地址
    0x0A6352  r12 = -46          ← 搬完就去 wai_flg

发送侧（0x081760）则是一条消息构造流水线：`clr_flg` → 填 msg 的 +0x08/+0x0A/
+0x0C/+0x0E 四个半字 → `snd_msg(1, msg)`，而 msg+0x10 存的是一个**代码指针**
（实测 0x0008225E）。

所以假设是：**消息是「回调注册」的载体，任务 5 收到后把函数指针装进回调槽。**

本脚本用固件自己那条消息做样本投回去，再对比 0x3F000–0x3F100 的前后快照 ——
哪个槽变了、变成什么，一目了然。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

WARM, BATCH = 200000, 5000
MBX_ID = 1
MSG_BUF = 0x0003F900
SAMPLE = 0x000417DC          # 固件自己造的那条消息的常驻地址
SLOT_LO, SLOT_HI = 0x3F000, 0x3F100


def base():
    m = create("d300-a")
    m.load()
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m, cov


def spin(m, steps):
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break


def t5(m):
    for t in realos.states(m):
        if t["id"] == 5:
            return t
    raise KeyError(5)


def snap(m):
    return {a: m.bus.rd(a, 4) for a in range(SLOT_LO, SLOT_HI, 4)}


def diff(a, b, title):
    changed = [(k, a[k], b[k]) for k in a if a[k] != b[k]]
    print("  %s：%d 处变化" % (title, len(changed)))
    for k, x, y in changed:
        vec = None
        if 0x3F01C <= k < 0x3F01C + 48 * 4:
            vec = 0x10 + (k - 0x3F01C) // 4
        print("    0x%05X  0x%08X → 0x%08X%s"
              % (k, x, y, ("   (vec 0x%02X 槽)" % vec) if vec else ""))
    return changed


def main():
    m, cov = base()
    spin(m, WARM)
    print("=" * 78)
    print("阶段 0：预热 %d 步后任务 5 状态 %s pc=0x%08X"
          % (m.step_count, t5(m)["wait_kind"], t5(m)["pc"]))
    print("  执行地址 %d / 服务 %d" % (cov.words(), len(m.flow.calls)))

    print("\n  0x3F000–0x3F100 现值（回调槽区）")
    s0 = snap(m)
    for a in sorted(s0):
        if s0[a]:
            print("    0x%05X  0x%08X" % (a, s0[a]))

    print("\n" + "=" * 78)
    print("阶段 1：把固件自己那条消息（0x%05X）抄到 0x%05X 再投出去"
          % (SAMPLE, MSG_BUF))
    for i in range(0x20):
        m.bus.wr(MSG_BUF + i, 1, m.bus.rd(SAMPLE + i, 1))
    print("  抄好的样本：")
    for i in range(2):
        a = MSG_BUF + i * 16
        print("    0x%05X  %s" % (a, " ".join("%02X" % m.bus.rd(a + j, 1)
                                              for j in range(16))))

    n_calls_before = len(m.flow.calls)
    n_words_before = cov.words()
    res = realos.kernel_call(m, -63, r4=MBX_ID, r5=MSG_BUF, max_steps=200000)
    print("\n  snd_msg(1, 0x%05X) → %r，任务 5 变 %s"
          % (MSG_BUF, res, t5(m)["wait_kind"]))

    print("\n  投递后跑 2 万步，让它处理完这条消息")
    spin(m, 20000)
    t = t5(m)
    print("  任务 5 现在 %s pc=0x%08X" % (t["wait_kind"], t["pc"]))
    print("  新增服务调用 %d 次，执行地址 %d → %d（+%d）"
          % (len(m.flow.calls) - n_calls_before, n_words_before, cov.words(),
             cov.words() - n_words_before))

    s1 = snap(m)
    print()
    diff(s0, s1, "回调槽区 0x3F000–0x3F100")

    print("\n" + "=" * 78)
    print("阶段 2：新出现的服务调用（消息处理路径）")
    for step, pc, svc, r4, r5, r6, r7, r13, tcb in m.flow.calls[n_calls_before:][:40]:
        print("  步 %-7d 0x%08X svc=%-4d r4=0x%X r5=0x%05X r6=0x%X r7=0x%X tcb=0x%05X"
              % (step, pc, svc, r4, r5, r6, r7, tcb))

    print("\n" + "=" * 78)
    print("阶段 3：0x3FE48 / 0x3FF99（任务 5 的另外两个固定地址）")
    for a in (0x3FE48, 0x3FF99):
        print("  0x%05X  %s" % (a, " ".join("%02X" % m.bus.rd(a + j, 1)
                                             for j in range(16))))


if __name__ == "__main__":
    main()
