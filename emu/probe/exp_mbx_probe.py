# -*- coding: utf-8 -*-
"""exp_mbx_probe.py —— 信箱控制块结构 + 用固件内核代投消息（V17 第 3 步）

现场（exp_mbx_live.py 抓到的）：

    步 43622  0x0A6324  rcv_msg  r4=0x000466E4  r5=0x00000001  tcb=0x45AAC(任务 5)
    步 44433  0x08179C  snd_msg  r4=0x00000001  r5=0x000417DC  tcb=0x459FC(任务 1)
    步 51963  0x0A6324  rcv_msg  r4=0x000466E4  r5=0x00000001  tcb=0x45AAC(任务 5)

也就说：任务 5 **收到过一条**（步 43622 发的那个请求被任务 1 在 44433 应答），
处理完回到 51963 再等第二条，从此不再有第二条 —— 它不是「卡住」，是「没人再发」。

于是问题从「任务 5 为什么死」变成「怎样从外部产生一条消息」。本脚本先摸清
0x466E4 这块控制块的结构，再试 `realos.kernel_call(m, -63, ...)` 让**固件自己的
内核**代为投递 —— 全程不碰内核代码，只当一个发消息的设备。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create            # noqa: E402
from frbox import realos                    # noqa: E402

STEPS, BATCH = 200000, 5000
MBX = 0x000466E4


def base(boot_all=False):
    from frbox import taskboot
    m = create("d300-a")
    m.load()
    if boot_all:
        taskboot.activate(m, "dormant")
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m, cov


def spin(m, steps=STEPS):
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break


def dump(m, addr, n=6, title=""):
    if title:
        print("  %s @0x%05X" % (title, addr))
    for i in range(n):
        a = addr + i * 16
        row = [m.bus.rd(a + j, 1) for j in range(16)]
        print("    0x%05X  %s  |%s|"
              % (a, " ".join("%02X" % b for b in row),
                 "".join(chr(b) if 32 <= b < 127 else "." for b in row)))


def show(m, tag):
    st = {t["id"]: t for t in realos.states(m)}
    t5 = st[5]
    print("  [%s] 任务5 kind=%-6s pc=0x%08X state=0x%08X  |  队列对象 0x%05X"
          % (tag, t5["wait_kind"], t5["pc"], t5["wait_state"], t5["tcb"]))
    dump(m, MBX, 3, "mbx 0x466E4")
    return t5


def _t5(m):
    """states() 按任务表顺序返回，空闲任务 0 在最后 —— 必须按 id 查。"""
    for t in realos.states(m):
        if t["id"] == 5:
            return t
    raise KeyError(5)


def main():
    m, cov = base()
    spin(m)
    print("=" * 78)
    print("阶段 1：跑完 20 万步，看等待现场")
    show(m, "停住")
    print("  0x466E4 … 0x46700 到底是谁的地盘？查任务表栈顶")
    for r in realos.records(m):
        print("    任务 %-2d entry=0x%08X sp=0x%05X tcb=0x%05X"
              % (r["id"], r["entry"], r["sp"], r["tcb"]))

    print("\n" + "=" * 78)
    print("阶段 2：用固件内核代投一条消息 snd_msg(mbx=1, msg)")
    # 消息体：随便给一块 RAM（0x3FF00 是未用的回调槽尾），内容填一个可识别的模式
    MSG = 0x0003F900
    for i in range(0x20):
        m.bus.wr(MSG + i, 1, (0xA0 + i) & 0xFF)
    print("  消息体 @0x%05X：" % MSG)
    dump(m, MSG, 2)

    for r4, label in ((1, "id=1"), (MBX, "addr=0x466E4")):
        before_pc = _t5(m)["pc"]
        res = realos.kernel_call(m, -63, r4=r4, r5=MSG, max_steps=200000)
        print("  snd_msg(%s) → %r   （任务5 pc 0x%08X → 0x%08X）"
              % (label, res, before_pc, _t5(m)["pc"]))
        if res == 0:
            break
    show(m, "投递后")

    print("\n" + "=" * 78)
    print("阶段 3：投递后再跑，看任务 5 动不动")
    for n in range(4):
        spin(m, 20000)
        t5 = _t5(m)
        print("  第 %d 轮  步 %-8d 任务5 kind=%-6s pc=0x%08X 执行地址 %d"
              % (n + 1, m.step_count, t5["wait_kind"], t5["pc"], cov.words()))
    show(m, "再跑 8 万步")


if __name__ == "__main__":
    main()
