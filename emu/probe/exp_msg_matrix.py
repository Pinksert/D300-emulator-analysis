# -*- coding: utf-8 -*-
"""exp_msg_matrix.py —— 消息内容矩阵：任务 5 是命令分派器吗？（V17 第 4 步）

已知（exp_mbx_probe.py）：

  * 用 `realos.kernel_call(m, -63, r4=1, r5=msg)` 能让**固件自己的内核**投递成功（err=0）；
  * 任务 5 从 rcv_msg 醒来，PC 从 0x0A6326 走到 0x0A674A —— 说明它真的在处理消息体；
  * 它处理完又回到 rcv_msg 等第二条，也就是一个**常驻消息循环**。

于是有一个可证伪的假设：**消息体第一个字是命令码，值不同走不同分支**。
若成立，遍历命令码就能让任务 5 把处理函数跑遍 —— 这是 V15「事件越多≠覆盖越广」
之后第二个该被量化的东西：**内容比数量重要**。

本脚本用同一套基线（场景 A 步数口径），逐个命令码投递，记：
    执行地址数 / 服务调用数 / 任务 5 的落脚 PC / 是否回到 rcv_msg
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
MSG = 0x0003F900          # 消息体暂存区（回调槽末尾的空地）
MSG_WORDS = 8


def base():
    m = create("d300-a")
    m.load()
    m.reset()
    cov = m.enable_cov()
    m.boot_realos(1)
    m.enable_events(period=4096)
    m.attach_flow()
    return m, cov


def spin(m, steps, hit=True):
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
        if hit:
            for v in (0x13, 0x19):
                m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break


def write_msg(m, words):
    for i, w in enumerate(words):
        m.bus.wr(MSG + i * 2, 2, w & 0xFFFF)
    return MSG


def t5(m):
    # ⚠️ states() 按任务表顺序返回，空闲任务 0 排在最后 —— 不能用下标 [5]
    for t in realos.states(m):
        if t["id"] == 5:
            return t
    raise KeyError(5)


def run(words, label, rounds=6, per=20000):
    m, cov = base()
    spin(m, WARM)
    base_addr = cov.words()
    base_svc = len(m.flow.calls)
    base_pcs = {c[1] for c in m.flow.calls}
    base_rcv = sum(1 for c in m.flow.calls if c[2] == -61)
    sent = 0
    errs = []
    trail = []
    for r in range(rounds):
        st = t5(m)
        if st["wait_kind"] != "msg":
            # 不在等消息就别硬塞
            trail.append(("%d:跳过(%s)" % (r, st["wait_kind"]), st["pc"]))
            spin(m, per)
            continue
        write_msg(m, words)
        res = realos.kernel_call(m, -63, r4=MBX_ID, r5=MSG, max_steps=200000)
        sent += 1 if res.get("err") == 0 else 0
        errs.append(res.get("err"))
        spin(m, per)
        s2 = t5(m)
        trail.append(("%d:%s" % (r, s2["wait_kind"]), s2["pc"]))
    return {
        "label": label, "words": cov.words(), "delta_w": cov.words() - base_addr,
        "svc": len(m.flow.calls), "delta_s": len(m.flow.calls) - base_svc,
        "sent": sent, "errs": errs, "trail": trail,
        "rcv": sum(1 for c in m.flow.calls if c[2] == -61) - base_rcv,
        "newpcs": sorted({c[1] for c in m.flow.calls} - base_pcs), "pc": t5(m)["pc"],
        "kind": t5(m)["wait_kind"], "steps": m.step_count,
    }


def main():
    cases = [("不投递（基线）", None)]
    # 单字命令码扫描
    for c in (0x0000, 0x0001, 0x0002, 0x0008, 0x0010, 0x0020, 0x0040,
              0x0080, 0x0100, 0x0800, 0x8000, 0xFFFF):
        cases.append(("cmd=0x%04X" % c, [c] + [0] * (MSG_WORDS - 1)))
    # 固定首字、变第二字
    for c in (0x0001, 0x0004, 0x0010):
        cases.append(("w0=0x0001 w1=0x%04X" % c, [0x0001, c] + [0] * (MSG_WORDS - 2)))

    print("=" * 92)
    print("任务 5 消息内容矩阵（每例：20 万步预热 + 6 轮「投递→再跑 2 万步」）")
    print("=" * 92)
    print("%-24s %-6s %-7s %-7s %-8s %-10s %s"
          % ("消息内容", "投出", "执行地址", "Δ地址", "服务", "任务5状态", "落脚 PC"))
    rows = []
    for label, words in cases:
        if words is None:
            # ⚠️ 基线必须跑**同样多**的步数（WARM + 6×20000），否则「多跑出来的覆盖」
            # 会被误记成「投递带来的覆盖」。exp_msg_chain 的 A 场景是这个口径。
            m, cov = base()
            spin(m, WARM)
            spin(m, 6 * 20000)
            r = {"label": label, "words": cov.words(), "delta_w": 0,
                 "svc": len(m.flow.calls), "delta_s": 0, "sent": 0,
                 "kind": t5(m)["wait_kind"], "pc": t5(m)["pc"],
                 "steps": m.step_count, "trail": []}
        else:
            r = run(words, label)
        rows.append(r)
        print("%-24s %-6d %-7d %-+7d %-8d %-10s 0x%08X"
              % (r["label"], r["sent"], r["words"], r["delta_w"],
                 r["svc"], r["kind"], r["pc"]))

    print("\n" + "=" * 92)
    print("Δ 排名（相对基线）")
    best = sorted([r for r in rows if r["delta_w"] != 0],
                  key=lambda r: -r["delta_w"])[:6]
    for r in best:
        print("  %-24s Δ地址 %-+6d Δ服务 %-+5d 轨迹 %s"
              % (r["label"], r["delta_w"], r["delta_s"],
                 " ".join("%s@0x%05X" % (k.split(":")[1], v)
                          for k, v in r["trail"][:4])))

    print("\n" + "=" * 92)
    print("各例任务 5 的落脚点（判定「命令码是否真的分流」）")
    pcs = {}
    for r in rows[1:]:
        pcs.setdefault(r["pc"], []).append(r["label"])
    for pc in sorted(pcs):
        print("  0x%08X  ← %d 例：%s" % (pc, len(pcs[pc]), "、".join(pcs[pc][:6])))


if __name__ == "__main__":
    main()
