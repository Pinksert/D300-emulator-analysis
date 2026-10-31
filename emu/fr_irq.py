# -*- coding: utf-8 -*-
"""中断向量表测绘 + ISR 试跑。

FR 的向量槽地址 = TBR + (255 - 向量号) * 4，倒序排列。D300 实测：
  A 段 TBR = 0x000FFC00（512KB 段末尾）
  B 段 TBR = 0x000EFC00
未使用的中断统一指向一个「兜底」地址，所以「非兜底」的就等于「这台机器真接了这条中断」。

用法：
  python fr_irq.py vec             两段的向量表分布
  python fr_irq.py run  1B         跑 B 段 IRQ 0x1B 的 ISR
  python fr_irq.py run  1B --io 60=FF
"""
import sys

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")



sys.stdout.reconfigure(encoding="utf-8")

from fr_emu import Fr, Mem, TABLE, M32, Unimpl
from fr_itron import ItronStub

FW = r"C:\Users\admin\WorkBuddy\D300固件\D300Update"
SEG = {
    "A": dict(file="A3000111.bin", base=0x00080000, tbr=0x000FFC00,
              fallback=0x000FE250, ssp=0x0007F000),
    "B": dict(file="B3000111.bin", base=0x00040000, tbr=0x000EFC00,
              fallback=None, ssp=0x0003F000),
}

# FRS 里这套名字来自 FR80 手册的中断源编号，仅作标注；未确认的留空
KNOWN = {
    "B": {0x1B: "Serial IF0", 0x20: "EXT9"},
}


def load(seg):
    s = SEG[seg]
    return open(f"{FW}\\{s['file']}", "rb").read(), s


def vectors(seg):
    """返回 {向量号: 处理程序地址}，只保留落在段内的合法项"""
    d, s = load(seg)
    out = {}
    for vec in range(256):
        slot = s["tbr"] + (255 - vec) * 4
        off = slot - s["base"]
        if 0 <= off <= len(d) - 4:
            out[vec] = int.from_bytes(d[off:off + 4], "big")
    return out, d, s


def cmd_vec():
    for seg in ("A", "B"):
        v, d, s = vectors(seg)
        from collections import Counter
        c = Counter(v.values())
        fb = s["fallback"] or c.most_common(1)[0][0]
        live = {k: x for k, x in v.items() if x != fb}
        print(f"===== {seg} 段  TBR=0x{s['tbr']:08X} =====")
        print(f"  向量槽 0x{s['tbr'] + 0:08X} .. 0x{s['tbr'] + 255 * 4:08X}"
              f"（{len(v)} 条）")
        print(f"  兜底地址 0x{fb:08X}：{c[fb]} 条占位")
        print(f"  真正接线的中断 {len(live)} 条，去重处理程序 {len(set(live.values()))} 个")
        for vec in sorted(live):
            nm = KNOWN.get(seg, {}).get(vec, "")
            print(f"    IRQ 0x{vec:02X}  槽 0x{s['tbr'] + (255 - vec) * 4:08X}"
                  f"  →  0x{live[vec]:08X}   {nm}")
        print()


def build_isr(seg):
    d, s = load(seg)
    m = Mem()
    m.load_rom(s["base"], d)
    cpu = Fr(m, TABLE)
    cpu.dr[0] = s["tbr"]
    cpu.dr[2] = s["ssp"]
    cpu.dr[3] = s["ssp"] - 0x1000
    cpu.i = 1
    cpu.ilm = 0
    cpu.r[15] = s["ssp"]
    cpu.pc = s["base"] + (s["tbr"] - s["base"]) - 0x4000   # 段内一个安全落点
    return cpu, m, s


def cmd_run(argv):
    seg = "B"
    if argv and argv[0] in ("A", "B"):
        seg = argv.pop(0)
    vec = int(argv.pop(0), 16)
    io = {}
    while argv:
        a = argv.pop(0)
        if a == "--io":
            k, val = argv.pop(0).split("=")
            io[int(k, 16)] = int(val, 16)
    cpu, m, s = build_isr(seg)
    m.io_value.update(io)
    svc = ItronStub(seg=seg)
    cpu.svc_hook = svc
    cpu.request_irq(vec, level=8)
    start = cpu.pc
    peak = 0
    n = 0
    for n in range(50000):
        if cpu.halted:
            break
        cpu.step()
        peak = max(peak, s["ssp"] - cpu._get_sp(0))
        if cpu.pc == start:
            break
    print(f"IRQ 0x{vec:02X}（{seg} 段）注入外设 {[hex(k)+'='+hex(v) for k, v in io.items()] or '无'}")
    print(f"  步数 {n + 1}   结束 PC 0x{cpu.pc:08X}   "
          f"{'已 reti 返回' if cpu.pc == start else '未返回'}")
    if cpu.halted:
        print(f"  停机：{cpu.halt_reason}")
    print(f"  峰值栈用量 {peak} 字节")
    if cpu.irq_log:
        v_, lv, pc_, tgt = cpu.irq_log[0]
        print(f"  EIT：从 0x{pc_:08X} 进 → 向量 0x{v_:02X} → 0x{tgt:08X}")
    if m.io_reads:
        print("  外设读：" + "  ".join(f"0x{a:03X}×{c}" for a, c in sorted(m.io_reads.items())))
    if m.io_writes:
        print("  外设写：" + "  ".join(f"0x{a:03X}←0x{v:02X}" for a, _, v, _ in m.io_writes))
    for pc_, num, name, tgt, impl, args in svc.log:
        print(f"  μITRON @0x{pc_:08X}  {name} (svc {num}, 内核入口 0x{tgt:08X}, "
              f"{'已实现' if impl else '桩'})  参数 " +
              " ".join(f"{k}=0x{v:08X}" for k, v in args.items()))


def cmd_sweep(seg="B", max_steps=20000):
    v, d, s = vectors(seg)
    from collections import Counter
    c = Counter(v.values())
    fb = s["fallback"] or c.most_common(1)[0][0]
    live = {k: x for k, x in v.items() if x != fb}
    print(f"{seg} 段：{len(live)} 条接线中断，逐条单独跑（RAM 全 0、外设读 0）")
    ok = 0
    rows = []
    for vec in sorted(live):
        cpu, m, s2 = build_isr(seg)
        svc = ItronStub(seg=seg)
        cpu.svc_hook = svc
        cpu.request_irq(vec, level=8)
        start = cpu.pc
        peak = 0
        n = 0
        reason = ""
        for n in range(max_steps):
            if cpu.halted:
                reason = cpu.halt_reason
                break
            try:
                cpu.step()
            except Unimpl as e:
                reason = f"未实现 {e.args[0]}"
                break
            peak = max(peak, s2["ssp"] - cpu._get_sp(0))
            if cpu.pc == start:
                break
        ret = cpu.pc == start
        ok += ret
        rows.append((vec, live[vec], n + 1, ret, peak, reason,
                     ",".join(svc.count) if svc.count else ""))
    for vec, tgt, n, ret, peak, reason, svcs in rows:
        flag = "返回" if ret else "未返回"
        extra = f"  {reason}" if reason else ""
        extra += f"  服务[{svcs}]" if svcs else ""
        print(f"  IRQ 0x{vec:02X} → 0x{tgt:08X}  {n:6d} 步  {flag}  栈{peak:4d}{extra}")
    print(f"\n合计：{ok}/{len(rows)} 条能独立跑到 reti")
    return ok, len(rows)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "vec":
        cmd_vec()
    elif a[0] == "run":
        cmd_run(a[1:])
    elif a[0] == "sweep":
        cmd_sweep(a[1] if len(a) > 1 else "B")
    else:
        print(__doc__)
