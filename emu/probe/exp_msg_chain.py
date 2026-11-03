# -*- coding: utf-8 -*-
"""exp_msg_chain.py —— 消息链：一条消息能拉起多少人（V17 主实验）

前四步把路铺完了：

  exp_msg_static   A 段只有 1 处 `rcv_msg`（0x0A6324，任务 5），`snd_msg` 15 处、R4 全是 1；
  exp_mbx_live     任务 5 的 mbx id 实测是控制块 0x466E4；它在步 43622 等、任务 1 在
                   44433 发、步 51963 又等 —— 不是死锁，是没人再发；
  exp_mbx_probe    `realos.kernel_call(-63, r4=1, r5=msg)` 能用**固件自己的内核**投递成功；
  exp_msg_slot     投递后任务 5 走完整处理链，末了 `set_flg(2,0x4000)` + `set_flg(3,0x2000)`
                   再回到 rcv_msg —— 它是个「消息→标志」的转发器。

于是本实验只问一件事：**同一条消息，投 N 次要多少步才能把覆盖推满，能不能拉起别的任务。**

四个场景（都以 20 万步为基本节拍，与 V15/V16 口径一致）：

    A  不投递（基线，= V15 的 D 场景）
    B  投 1 条
    C  每 2 万步投 1 条 × 8
    D  C 之后再等 20 万步（看有没有延迟唤醒）
    E  全开 dormant + 每 2 万步投 1 条 × 8（与 V16 的 B 场景叠加）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create                       # noqa: E402
from frbox import realos, taskboot                     # noqa: E402

WARM, BATCH = 200000, 5000
MBX_ID = 1
MSG_BUF = 0x0003F900
SAMPLE = 0x000417DC           # 固件自己造的那条消息（exp_msg_frame.py 抄下来的）
ALL = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]


def base(boot_all=False):
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


def spin(m, steps, mail=None):
    """跑 steps 步；给了 mail 就每 2 万步投一条消息。"""
    left = steps
    while left > 0:
        n = min(BATCH, left)
        m.run(max_steps=n, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        for v in (0x13, 0x19):
            m.raise_irq(v)
        left -= n
        if m.cpu.halted or m.fault:
            break
        if mail is not None and m.step_count % 20000 < BATCH:
            mail()


def t_of(m, tid):
    for t in realos.states(m):
        if t["id"] == tid:
            return t
    raise KeyError(tid)


def snapshot(m, cov):
    st = {t["id"]: t for t in realos.states(m)}
    ran = [tid for tid in ALL if cov.visited(st[tid]["entry"])]
    return {
        "steps": m.step_count, "words": cov.words(), "svc": len(m.flow.calls),
        "ran": ran, "rows": [(tid, cov.visited(st[tid]["entry"]), st[tid]["wait_kind"],
                              st[tid]["wait_pat"], st[tid]["pc"]) for tid in ALL],
        "pcs": sorted({c[1] for c in m.flow.calls}),
    }


def scenario(label, boot_all=False, sends=0, tail=0):
    m, cov = base(boot_all)
    spin(m, WARM)

    def mail():
        for i in range(0x20):
            m.bus.wr(MSG_BUF + i, 1, m.bus.rd(SAMPLE + i, 1))
        realos.kernel_call(m, -63, r4=MBX_ID, r5=MSG_BUF, max_steps=200000)
        m.sent = getattr(m, "sent", 0) + 1

    m.sent = 0
    if sends:
        for _ in range(sends):
            spin(m, 20000, mail)
            if t_of(m, 5)["wait_kind"] != "msg":
                spin(m, 20000)
    if tail:
        spin(m, tail)
    s = snapshot(m, cov)
    s["label"] = label
    s["sent"] = m.sent
    return s


def table(s):
    print("=" * 88)
    print("%s   步数 %d   执行地址 %d   服务 %d   投递 %d 条   跑过的任务 %d/12"
          % (s["label"], s["steps"], s["words"], s["svc"], s["sent"], len(s["ran"])))
    print("%-6s %-8s %-8s %-10s %s" % ("任务", "跑过", "在等", "要的位", "PC"))
    for tid, ran, kind, pat, pc in s["rows"]:
        print("%-6d %-8s %-8s 0x%-8X 0x%08X"
              % (tid, "是" if ran else "**没**", kind, pat, pc))


def main():
    out = []
    out.append(scenario("A 基线（不投递）", sends=0))
    table(out[-1])
    out.append(scenario("B 投 1 条", sends=1))
    table(out[-1])
    out.append(scenario("C 每 2 万步投 1 条 × 8", sends=8))
    table(out[-1])
    out.append(scenario("D C 之后再空转 20 万步", sends=8, tail=200000))
    table(out[-1])
    out.append(scenario("E 全开 dormant + 投 8 条", boot_all=True, sends=8))
    table(out[-1])

    print("=" * 88)
    print("总表")
    print("%-26s %-10s %-10s %-8s %-10s %s"
          % ("场景", "步数", "执行地址", "服务", "跑过任务", "任务 5"))
    for s in out:
        print("%-26s %-10d %-10d %-8d %-10s %s"
              % (s["label"], s["steps"], s["words"], s["svc"],
                 "%d/12" % len(s["ran"]), t_of_label(s)))

    print("\n新增的服务调用点（相对场景 A）")
    a = set(out[0]["pcs"])
    for s in out[1:]:
        new = [p for p in s["pcs"] if p not in a]
        print("  %-26s %d 处：%s"
              % (s["label"], len(new), " ".join("0x%08X" % p for p in new[:10])))

    print("\n覆盖率相对 A 的增量")
    for s in out[1:]:
        print("  %-26s Δ地址 %-+6d Δ服务 %-+5d"
              % (s["label"], s["words"] - out[0]["words"],
                 s["svc"] - out[0]["svc"]))


def t_of_label(s):
    for tid, ran, kind, pat, pc in s["rows"]:
        if tid == 5:
            return "%s @0x%08X" % (kind, pc)
    return "?"


if __name__ == "__main__":
    main()
