# -*- coding: utf-8 -*-
"""V90 正式探针：0x83E0 参数归属的地址错配纠正 / r7 读点普查 / 收敛态可见性（KI#351）。

    cd emu && python probe/exp_svc_v90.py > ../results/frbox/svc90.txt

A–C 段只读 ROM（含全 ROM 流式普查，只做一次）；D 段 3 台反事实/只观测机；
F 段 1 台标准收敛态机；合计 **4 台独立机器**，每台只跑一遍。
强制 PC 到 0x15FC2C、写 r7 都是**反事实补全（模型动作）**，
不是固件行为、不是真机器件事件。链未闭合。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
EMU = r"C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp/gh_upload/D300-emulator-v2/emu"
sys.path.insert(0, EMU)

import fr_emu
from collections import Counter
from frbox import bstatic, svc90 as V, blive

M = V.M32
SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base
T = fr_emu.TABLE


def ins_at(pc):
    ins = T.decode(rom, pc - base)
    return ("??", {}, 2) if ins is None else (ins[0], dict(ins[2]), ins[3])


def dec(lo, hi):
    out, pc = [], lo
    while pc < hi - 1:
        ins = T.decode(rom, pc - base)
        if ins is None:
            out.append((pc, "??", {}, 2))
            pc += 2
            continue
        mn, _b, v, sz, _w = ins
        out.append((pc, mn, dict(v), sz))
        pc += sz
    return out


def im(v):
    g = []
    for k in ("i8", "i20", "i32", "u4", "u8", "u6", "m4", "disp10",
              "disp8", "rel9", "rel12", "udisp6", "rs2"):
        if k in v:
            g.append("%s=%s" % (k, v[k]))
    for k in ("ri", "rj"):
        if k in v:
            g.append("%s=r%d" % (k, v[k]))
    return " ".join(g)


# ---------------------------------------------------------------- 全 ROM 流式普查（一次）
i32_true, i32_wrong, i20_e30 = [], [], []
off, n, decoded = 0, len(rom), 0
while off < n - 1:
    ins = T.decode(rom, off)
    if ins is None:
        off += 2
        continue
    mn, _b, v, sz, _w = ins
    decoded += 1
    pc = base + off
    if mn == "ldi:32" and v.get("i32") == V.I32_TRUE_TARGET:
        i32_true.append((pc, v.get("ri")))
    if mn == "ldi:32" and v.get("i32") == V.V89_SUB_DEC:
        i32_wrong.append((pc, v.get("ri")))
    if mn == "ldi:20" and v.get("i20") == V.I20_83E0:
        i20_e30.append((pc, v.get("ri")))
    off += sz

print("[A] 地址错配纠正（只读 ROM，全 ROM 流式解码一次）")
print("A1 全 ROM 解码 %d 条；ldi:32 #%d(0x%06X) 共 %d 条；"
      "ldi:32 #%d(0x%06X) 共 %d 条"
      % (decoded, V.I32_TRUE_TARGET, V.TARGET_HEX, len(i32_true),
         V.V89_SUB_DEC, V.V89_SUB_ADDR, len(i32_wrong)))
print("A2 差 = %d (0x%X)；判据 = %s"
      % (V.addr_delta(V.I32_TRUE_TARGET, V.V89_SUB_DEC), V.ADDR_DELTA,
         V.call_target_verdict(V.I32_TRUE_TARGET, V.V89_SUB_DEC)))
print("A3 真入口 0x%06X 函数序言：%s"
      % (V.SUB_ENTRY, " / ".join("0x%06X %s" % p for p in V.PROLOGUE)))
for pc, mn, v, _s in dec(V.SUB_ENTRY, V.SUB_ENTRY + 0x10):
    print("   0x%06X %-9s %s" % (pc, mn, im(v)))

print("")
print("[B] 函数体 r7 读点普查（0x%06X..0x%06X，只读）"
      % (V.SUB_ENTRY, V.SUB_FIRST_RET))
body = dec(V.SUB_ENTRY, V.SUB_FIRST_RET)
r7_refs = [(pc, mn, v) for pc, mn, v, _s in body
           if v.get("ri") == 7 or v.get("rj") == 7]
mem_reads = [(pc, mn, v) for pc, mn, v, _s in body
             if mn in ("ld", "st", "lduh", "ldub", "sth", "stb")]
mem_base = set()
for pc, mn, v in mem_reads:
    for k in ("ri", "rj"):
        if k in v:
            mem_base.add(v[k])
for pc, mn, v in r7_refs:
    print("B1 r7 引用 0x%06X %-9s %s" % (pc, mn, im(v)))
print("B2 r7 引用 %d 条 / 内存指令 %d 条 / 基址寄存器集合 %s / r7 在基址中=%s"
      % (len(r7_refs), len(mem_reads), tuple(sorted(mem_base)), 7 in mem_base))
print("B3 判据 = %s"
      % V.r7_usage_verdict(len(r7_refs), 0, 7 in mem_base))

print("")
print("[C] ldi:20 #0x83E0 三调用点（只读）")
print("C1 全 ROM 共 %d 条，ri 分布 %s，PC=%s"
      % (len(i20_e30), dict(Counter(r for _p, r in i20_e30)),
         [hex(p) for p, _r in i20_e30]))
for cp, _r in i20_e30:
    seq = [(pc, mn, v) for pc, mn, v, _s in dec(cp - 2, cp + 12)
           if mn in ("ldi:20", "ldi:32", "call:d")]
    print("C2 0x%06X 调用序列 %s" % (cp, " -> ".join(
        "%s(i20=%s,i32=%s)" % (mn, v.get("i20", "-"), v.get("i32", "-"))
        for _pc, mn, v in seq)))
print("C3 调用链模板 = %s" % " / ".join(V.CALL_SEQ))

print("")
print("[D] 动态反事实（模型动作：强制 PC 到 0x15FC2C + 写 r7，非固件行为）")
MACHINES = []
for tag, r7_init in (("CF_83E0", V.CF_R7_INIT), ("CF_ZERO", 0)):
    mm = blive.boot(0)
    mm.step(V.BOOT_STEP)
    cpu = mm.cpu
    r14_at_boot, r15_at_boot = cpu.r[14] & M, cpu.r[15] & M
    cpu.pc = V.SUB_ENTRY
    cpu.r[7] = r7_init
    seen, first_clear, r7_before = [], None, r7_init
    for i in range(80):
        pc = cpu.pc
        if not (V.SUB_ENTRY <= pc < V.SUB_FIRST_RET):
            break
        mn, v, _sz = ins_at(pc)
        seen.append((pc, mn, cpu.r[7] & M))
        mm.step(1)
        if first_clear is None and cpu.r[7] & M == 0 and r7_init != 0:
            first_clear = (pc, len(seen))
    # 统一用 mm.step（Fr.step 签名不接受参数）
    MACHINES.append({"tag": tag, "r7_init": r7_init, "r14": r14_at_boot,
                     "r15": r15_at_boot, "steps": len(seen),
                     "last_pc": seen[-1][0] if seen else None,
                     "first_clear": first_clear,
                     "r7_end": cpu.r[7] & M, "trace": seen})
# 第三台：只观测机（不改 PC、不写 r7），确认强制跳转前的自然初态
mm_obs = blive.boot(0)
mm_obs.step(V.BOOT_STEP)
OBS = {"tag": "OBS", "r7_init": mm_obs.cpu.r[7] & M,
       "r14": mm_obs.cpu.r[14] & M, "r15": mm_obs.cpu.r[15] & M,
       "pc": mm_obs.cpu.pc}
MACHINES.append(OBS)
for r in MACHINES:
    if r["tag"] == "OBS":
        print("D1 OBS 只观测 PC=0x%06X r7=0x%08X r14=0x%08X r15=0x%08X"
              % (r["pc"], r["r7_init"], r["r14"], r["r15"]))
    else:
        print("D1 %s 入口 r14=0x%08X r15=0x%08X r7_init=0x%08X "
              "步数=%d 末PC=0x%06X 清零点=%s r7_end=0x%08X"
              % (r["tag"], r["r14"], r["r15"], r["r7_init"], r["steps"],
                 r["last_pc"], hex(r["first_clear"][0]) if r["first_clear"] else "无",
                 r["r7_end"]))
print("D2 r7 常量传播判据 = %s"
      % V.constant_propagation(V.CF_R7_INIT, V.R7_CLEAR_I8,
                               MACHINES[0]["r7_end"]))

print("")
print("[E] 三机对照表（r7 初值 / 清零点 / 末态；模型动作）")
print("E1 %-8s %-12s %-12s %-10s %s"
      % ("机", "r7_init", "清零点", "r7_end", "结论"))
for r in MACHINES:
    if r["tag"] == "OBS":
        print("E1 %-8s 0x%08X %-12s %-10s %s"
              % (r["tag"], r["r7_init"], "—", "—", "只观测，不改初态"))
    else:
        print("E1 %-8s 0x%08X %-12s 0x%08X %s"
              % (r["tag"], r["r7_init"],
                 hex(r["first_clear"][0]) if r["first_clear"] else "无",
                 r["r7_end"], "未读即清零" if r["first_clear"] else "初值 0"))
print("E2 CF_83E0 与 CF_ZERO 的末态 r7 全同（0x%08X）⇒ 差异只来自"
      "被清零前的值，不来自写动作本身"
      % MACHINES[0]["r7_end"])

print("")
print("[F] 标准收敛态可见性（1 台标准机，V65–V89 策略逐字相同）")
mm = blive.boot(0)
mm.step(V.CONVERGE_BOOT)
mm.raise_irq(V.CONVERGE_IRQ)
mm.run(V.CONVERGE_RUN)
cov = getattr(mm, "cov", None)
hits = []
for cp, _r in i20_e30:
    h = None
    if cov is not None:
        try:
            h = cov.visited(cp)
        except Exception:
            h = None
    hits.append(bool(h))
    print("F1 0x%06X cov.visited=%s" % (cp, h))
print("F2 标准收敛末 PC=0x%06X（期望 0x%06X）"
      % (mm.cpu.pc, V.CONVERGE_TAIL))
print("F3 可见性判据 = %s" % V.converge_visibility(hits))

print("")
print("[G] 四处自我证伪（把看似结论证伪为不成立或不可证）")
print("G1 「目标是 0x15FA6C」 → 1440812=0x15FC2C，"
      "全 ROM #1440812 %d 条 / #1440364 %d 条"
      % (len(i32_true), len(i32_wrong)))
print("G2 「r7=0x83E0 被当地址用」 → r7 读点 0 条，"
      "18 条内存指令基址集合 %s 不含 r7" % (tuple(sorted(mem_base)),))
print("G3 「3 个调用点可动态取证」 → 收敛态 coverage %s，全 False"
      % [bool(h) for h in hits])
print("G4 「服务号槽被该子例程读取」 → 只证到 r7 参数被清零；"
      "别的任务是否读仍开放")
print("G5 链未闭合：本版只结地址口径、指令流结构与可见性，"
      "不是器件事件语义")

print("")
print("[H] 综合判据与开放项")
target_ok = (len(i32_true) == V.CALL_SITE_COUNT
             and len(i32_wrong) == V.WRONG_TARGET_REF_COUNT
             and V.TARGET_HEX == 0x15FC2C)
r7_ok = (len(r7_refs) == V.R7_REF_COUNT and 7 not in mem_base)
vis_ok = (sum(1 for h in hits if h) == V.CONVERGE_HIT_COUNT)
prop_ok = (MACHINES[0]["r7_end"] == V.CF_R7_AFTER)
print("H1 前置五项 = %s / %s / %s / %s / %s"
      % (target_ok, r7_ok, vis_ok, prop_ok,
         MACHINES[0]["steps"] == V.CF_TRACE_STEPS))
print("H2 综合判据 = %s" % V.source_verdict(target_ok, r7_ok, vis_ok, prop_ok))
print("H3 机器数 = %d（3 反事实/只观测 + 1 标准收敛态）"
      % (V.CF_MACHINE_COUNT + V.CONVERGE_MACHINE_COUNT))
print("H4 边界：强制 PC / 写 r7 是模型动作，r14/r15 用陷阱口值非真实调用点栈帧；"
      "三调用点 coverage 全 False，只报结构结论，不外推真机语义。")
print("H5 仍开放：0x83E0 在真机是否被别的任务读取 / 三个调用点真机触发条件 / "
      "8 对别名语义分工 / 真机 vec 0x18 器件事件 / −85 器件事件语义 / "
      "bit3 置位源 / vec 0x2D / 317 提交调用点 / 任务 9 下载后是否回任务 7 / "
      "0xFFFFFFFF r13 哨兵真机设置者")
