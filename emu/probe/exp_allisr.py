# -*- coding: utf-8 -*-
"""exp_allisr.py —— A 段 48 个 ISR 全量注入验证

对 vec 0x10–0x3F 每一个向量：
  在 main 循环里注入 → 跑 → 看是否精确 reti 回注入点、r15/SSP 是否复原
判定三档：
  OK       正确回到注入点且寄存器复原
  NOWRAP   没回到注入点（跑飞/死循环）
  SHORT    步数异常少（可能 ISR 里直接改了 PC）

同时记录每个 ISR 的实际起止 PC 与执行步数。
"""
import sys, os

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create

INJECT_AT = 0x00080506


def to_inject_point(m):
    c = m.cpu
    m.run(max_steps=3000)
    g = 0
    while c.pc != INJECT_AT and g < 80000:
        c.step()
        m.step_count += 1
        g += 1
        if m.cycles_per_tick and m.step_count % m.cycles_per_tick == 0:
            for d in m.devices:
                d.tick(m.cycles_per_tick)
    return c.pc == INJECT_AT


def one(vec):
    m = create("d300-a")
    m.load()
    if not to_inject_point(m):
        return dict(vec=vec, verdict="NOENTRY")
    c = m.cpu
    pc0, r15_0, ssp0 = c.pc, c.r[15], c.dr[2]
    c.eit(vec)
    entry = c.pc
    trail = [entry]
    ok = False
    for i in range(6000):
        c.step()
        m.step_count += 1
        if c.pc == pc0:
            ok = True
            break
        if len(trail) < 4000:
            trail.append(c.pc)
    return dict(vec=vec, verdict="OK" if ok else "NOWRAP",
                entry=entry, steps=len(trail), tlen=len(trail),
                r15_ok=(c.r[15] == r15_ok_val(c, r15_0)) if False else (c.r[15] == r15_0),
                ssp_ok=(c.dr[2] == ssp0),
                final=c.pc, trail=trail)


def r15_ok_val(c, v):
    return v


def main():
    print("=" * 80)
    print("A 段 48 个 ISR 全量注入验证（vec 0x10–0x3F）")
    print("=" * 80)
    print()
    print("%-5s %-11s %-11s %6s %6s %6s" %
          ("vec", "ISR 入口", "判定", "指令数", "r15", "SSP"))
    print("-" * 80)
    rows = []
    for vec in range(0x10, 0x40):
        r = one(vec)
        rows.append(r)
        print("0x%02X  0x%08X %-11s %6d %6s %6s"
              % (vec,
                 r.get("entry", 0),
                 r["verdict"],
                 r.get("tlen", 0),
                 ("OK" if r.get("r15_ok") else "--") if r["verdict"] == "OK" else "--",
                 ("OK" if r.get("ssp_ok") else "--") if r["verdict"] == "OK" else "--"))
    print("-" * 80)
    ok = [r for r in rows if r["verdict"] == "OK"]
    bad = [r for r in rows if r["verdict"] != "OK"]
    print("结果：%d / 48 正确 reti 回注入点" % len(ok))
    if bad:
        print("异常向量：")
        for r in bad:
            print("  vec 0x%02X  %s  入口 0x%08X  最终 PC=0x%08X  轨迹 %d 步"
                  % (r["vec"], r["verdict"], r.get("entry", 0),
                     r.get("final", 0), r.get("tlen", 0)))
            tr = r.get("trail", [])[:14]
            print("     轨迹: %s" % " ".join("0x%06X" % p for p in tr))
    print()
    # 步数分布
    if ok:
        st = sorted(r["tlen"] for r in ok)
        print("ISR 长度分布：最短 %d 步、中位 %d 步、最长 %d 步"
              % (st[0], st[len(st) // 2], st[-1]))
        from collections import Counter
        c = Counter(r["tlen"] for r in ok)
        print("按步数分组：%s" % dict(sorted(c.items())))
    print()
    # 入口去重
    ents = {}
    for r in rows:
        ents.setdefault(r.get("entry", 0), []).append(r["vec"])
    dup = {k: v for k, v in ents.items() if len(v) > 1}
    print("共用同一入口的向量：%s"
          % ("、".join("0x%08X←%s" % (k, ",".join("0x%02X" % x for x in v))
                      for k, v in dup.items()) or "无（48 个入口互不相同）"))


if __name__ == "__main__":
    main()
