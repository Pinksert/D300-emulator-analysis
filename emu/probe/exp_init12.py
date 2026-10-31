# -*- coding: utf-8 -*-
"""exp_init12.py —— 追踪 A 段启动里 0xFE434–0xFE44A 那 12 条初始化 call

目标：
1. 每条 call 的真实目标地址、返回地址、执行了多少条指令、走了多少个不同 PC
2. 调用内部有没有执行 `int #0x40`（μITRON 服务入口）
3. 每条调用内部写了哪些内存区间（尤其是任务表 / 栈 / 邮箱）
4. 哪几条是「空壳」（只 ret），哪几条有实质内容
"""
import sys, io, os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create

CALL_PCS = [0x000FE434 + 2 * i for i in range(12)]
CALL_SET = set(CALL_PCS)


def run(m, max_steps=200000):
    c = m.cpu
    for i in range(max_steps):
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    return m.step_count


def main():
    m = create("d300-a")
    m.load()
    c = m.cpu

    # 先跑到 0xFE426（OS 启动），沿途记下是否已经被别的调用带过
    m.run(max_steps=80000)
    print("到达 PC=0x%08X  步数=%d" % (c.pc, m.step_count))

    # 若已越过 0xFE434（12 条 call 已执行完），需要回退：直接用新机器精确停在 0xFE434
    m = create("d300-a")
    m.load()
    c = m.cpu

    # 跑到第一次进入 0xFE434
    hist = []
    for i in range(120000):
        if c.pc == CALL_PCS[0]:
            break
        hist.append(c.pc)
        c.step()
        m.step_count += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    else:
        print("没走到 0xFE434，停在 0x%08X" % c.pc)
        return

    print("首次到达 0xFE434，已执行 %d 步，前 12 个 PC: %s"
          % (m.step_count, " ".join("0x%06X" % x for x in hist[-12:])))
    print()

    # 逐条 call：记录 from→to、返回点、内部 PC 集合、内部是否 int
    results = []
    for k in range(12):
        from_pc = c.pc
        assert from_pc == CALL_PCS[k], (hex(from_pc), hex(CALL_PCS[k]))
        pcs = set()
        wr_before = len(m.bus.all_writes)
        int_before = len(c.irq_log)
        ret_pc = None
        n = 0
        # 执行一步后进入被调函数
        c.step()
        m.step_count += 1
        target = c.pc
        # 记录 RP 上的返回地址
        rp = c.dr[0] if hasattr(c, "dr") else None
        # 继续跑到回到 0xFE436 + 2k（返回点）
        expect_ret = CALL_PCS[k] + 2
        guard = 0
        while c.pc != expect_ret and guard < 40000:
            pcs.add(c.pc)
            c.step()
            m.step_count += 1
            n += 1
            guard += 1
            if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
                for d in m.devices:
                    d.tick(m.cycles_per_tick)
        ret_pc = c.pc
        wr = m.bus.all_writes[wr_before:]
        addrs = sorted({a for a, _, _, _ in wr})
        results.append(dict(
            idx=k, from_pc=from_pc, target=target, ret=ret_pc,
            insns=n, pcs=len(pcs), ints=len(c.irq_log) - int_before,
            writes=len(wr), addrs=addrs,
        ))

    print("== 12 条初始化 call 逐条实测 ==")
    print("%-3s %-11s %-11s %6s %5s %5s %6s  %s"
          % ("#", "call", "target", "insns", "PCs", "int", "writes", "写入地址范围"))
    for r in results:
        a = r["addrs"]
        rng = ""
        if a:
            rng = "0x%06X–0x%06X (%d 处)" % (a[0], a[-1], len(a)) if len(a) > 1 \
                else "0x%06X" % a[0]
            if len(a) > 6:
                rng = "0x%06X…0x%06X (%d 处)" % (a[0], a[-1], len(a))
        print("%-3d 0x%08X 0x%08X %6d %5d %5d %6d  %s"
              % (r["idx"], r["from_pc"], r["target"], r["insns"],
                 r["pcs"], r["ints"], r["writes"], rng or "（无写）"))

    tot_int = sum(r["ints"] for r in results)
    print()
    print("12 条合计：指令 %d 条，int 触发 %d 次"
          % (sum(r["insns"] for r in results), tot_int))

    # 打印每条目标处的头几条指令，人工判断性质
    print()
    for r in results:
        print("--- #%d  target 0x%08X ---" % (r["idx"], r["target"]))
        print(m.disasm_at(r["target"], 8))
        print()


if __name__ == "__main__":
    main()
