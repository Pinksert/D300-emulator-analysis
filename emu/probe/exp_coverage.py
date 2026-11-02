# -*- coding: utf-8 -*-
"""exp_coverage.py —— 覆盖率：跑过的代码 / 从没跑过的代码段（V15）

四个场景各跑 30 万步，看事件源一步步把覆盖率抬高多少，以及「还没踩到的地方」长什么样：

    A 只有 REALOS（不挂任何事件源）
    B + dev_evt（周期 4096，只打 vec 0x23）
    C + 手动打 vec 0x13 / 0x19（V14 说这是拉起任务 8 的前提）
    D + 位图源 0x0110

最后把 D 的未访问区间按长度排序，给最长那几段的反汇编和它们读写的外设地址 ——
那些就是「要再往前推，必须补的硬件」。
"""
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

import fr_emu                              # noqa: E402
from frbox.machine import create           # noqa: E402
from frbox.flow import Flow                # noqa: E402
from frbox import realos                   # noqa: E402

T = fr_emu.TABLE
STEPS = 300000
BATCH = 5000
DP_LIMIT = 0x1000                          # 低于此地址按「外设」看


def scenario(label, events=False, raise_vecs=(), bits=0, steps=STEPS):
    m = create("d300-a")
    m.load()
    cov = m.enable_cov(label)
    m.boot_realos(1)
    if events:
        m.enable_events(period=4096)
    if bits:
        m.enable_bits(mask=bits, vecs=(0x13, 0x19), period=2048)
    fl = Flow(m)
    for _ in range(steps // BATCH):
        m.run(max_steps=BATCH, spin_guard=False)
        for v in raise_vecs:
            m.raise_irq(v)
        if m.cpu.halted or m.fault:
            break
    cov.steps = m.step_count
    st = realos.states(m)
    live = [t["id"] for t in st if t["pc"]]
    return {
        "label": label, "m": m, "cov": cov, "flow": fl,
        "steps": m.step_count, "svc": len(fl.calls),
        "live": live,
        "waits": [(t["id"], t["wait_kind"], t["wait_flag"], t["wait_pat"])
                  for t in st if t["wait_kind"] not in ("ready", "dormant")],
    }


def collect_ptrs(rom, base):
    """全段扫一遍，把每条 `ldi:20 #imm` 的立即数记成「ROM 里有人拿它当指针」。

    精度有限（指令边界靠 2 字节对齐扫，会有假阳性），但用来回答
    「这段没人跑过的代码，是不是某个任务/回调的入口」够用了。
    """
    refs = {}
    for off in range(0, len(rom) - 4, 2):
        ins = T.decode(rom, off)
        if not ins:
            continue
        if ins[0] == "ldi:20" and "i20" in ins[2]:
            refs.setdefault(ins[2]["i20"], []).append(base + off)
    return refs


def dis(rom, base, start, n=8):
    out, pc = [], start
    for _ in range(n):
        ins = T.decode(rom, pc - base)
        if ins is None:
            out.append("0x%06X  <解码失败>" % pc)
            break
        mn, _, vals, size, w = ins
        ops = " ".join("%s=%s" % (k, ("0x%X" % v) if isinstance(v, int) and v > 9 else v)
                       for k, v in vals.items() if k not in ("op1", "op2", "op3", "op4", "op5"))
        out.append("0x%06X  %-10s %s" % (pc, mn, ops))
        pc += max(2, size)
    return out


def io_footprint(rom, base, start, n=80):
    """线性扫 n 条，收集它碰的直接页/外设地址（靠 `ldi #imm` 与直接页写法粗略估）。"""
    regs, io = {}, set()
    pc = start
    for _ in range(n):
        ins = T.decode(rom, pc - base)
        if ins is None:
            break
        mn, _, vals, size, w = ins
        if "i20" in vals and "ri" in vals:
            regs[vals["ri"]] = vals["i20"]
        if "ri" in vals and vals.get("ri") in regs:
            a = regs[vals["ri"]]
            if a < DP_LIMIT:
                io.add(a)
        pc += max(2, size)
        if mn in ("ret", "ret:d", "reti"):
            break
    return sorted(io)


def main():
    runs = [
        scenario("A 只有 REALOS"),
        scenario("B + events(0x23)", events=True),
        scenario("C + vec13/19", events=True, raise_vecs=(0x13, 0x19)),
        scenario("D + bits 0x0110", events=True, raise_vecs=(0x13, 0x19), bits=0x0110),
    ]

    print("=" * 92)
    print("覆盖率：%d 步 × 4 个场景" % STEPS)
    print("=" * 92)
    print("%-18s %-9s %-8s %-9s %-9s %s"
          % ("场景", "服务调用", "有现场任务", "执行地址", "占 ROM", "未跑区间≥64B"))
    for r in runs:
        s = r["cov"].summary()
        print("%-18s %-9d %-10s %-9d %-8.1f%% %d"
              % (r["label"], r["svc"], ",".join(str(x) for x in sorted(r["live"])) or "-",
                 s["已执行指令地址"], s["占比"], s["未跑区间(≥64B)"]))

    base_run = runs[0]["cov"]
    for r in runs[1:]:
        d = r["cov"].diff(base_run, min_len=24)
        print("\n%s 比 A 多踩到的区间：%d 段，共 %d 字节"
              % (r["label"], len(d), sum(b - a for a, b in d)))
        for a, b in sorted(d, key=lambda t: t[1] - t[0], reverse=True)[:6]:
            print("   0x%06X–0x%06X  %5d B" % (a, b, b - a))

    last = runs[-1]
    m, cov = last["m"], last["cov"]
    rom, base = m.bus.rom, m.bus.rom_base
    holes = cov.holes(min_len=64)[:10]
    print("\n" + "=" * 92)
    print("D 场景下从没执行过的区间（按长度，前 %d 段）" % len(holes))
    print("=" * 92)
    ptrs = collect_ptrs(rom, base)
    tasks = {t["entry"]: t["id"] for t in realos.states(m)}
    for a, b in holes:
        print("\n0x%06X–0x%06X  %d B" % (a, b, b - a))
        for line in dis(rom, base, a, 5):
            print("   " + line)
        io = io_footprint(rom, base, a)
        print("   首 80 条里碰到的直接页/外设：%s"
              % (" ".join("0x%02X" % x for x in io) if io else "无"))
        inside = sorted(p for p in ptrs if a <= p < b)
        if inside:
            head = inside[:6]
            print("   段内有 %d 个地址被 `ldi:20` 引用过：%s"
                  % (len(inside), " ".join(
                      "0x%06X%s" % (p, ("(任务%d入口)" % tasks[p]) if p in tasks else "")
                      for p in head)))

    print("\n仍在等的任务：" + ("；".join(
        "任务%d %s%s" % (t[0], t[1], ("/f%d=0x%X" % (t[2], t[3])) if t[2] else "")
        for t in last["waits"]) or "无"))

    print("\n" + "=" * 92)
    print("13 条任务记录：入口跑过没有 / 谁把它的入口当地址装进寄存器")
    print("=" * 92)
    print("%-6s %-10s %-8s %s" % ("任务", "入口", "跑过", "ROM 里 `ldi:20 #入口` 的站点（★=该站点自己跑过）"))
    for t in realos.states(m):
        e = t["entry"]
        sites = ptrs.get(e, [])
        mark = lambda p: ("★0x%06X" % p) if cov.visited(p) else ("0x%06X" % p)
        tail = " ".join(mark(p) for p in sites[:4]) or "无"
        print("%-6s 0x%06X  %-8s %s"
              % (t["id"], e, "是" if cov.visited(e) else "**没**", tail))


if __name__ == "__main__":
    main()
