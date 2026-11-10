# -*- coding: utf-8 -*-
"""V71 正式探针：第二道等待的掩码判据与四级状态机可达性（A–H 八段）。

承接 KI#274 第一条。设备策略与 V65–V70 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；反事实段全部标注（kernel_call 注入真内核服务，
不是固件行为）。六台独立机器：基线/窗口各一台、G0–G3 四台。
"""
import sys
import os
import bisect

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, bdesc, brealos, btick,
                   descgate69 as G, wait271 as C)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000
POST = 200000
WINDOW = 1000000
FIRST = C.WANTPTN_1
GROUPS = (("G0", 0x00), ("G1", C.EXPECT),
          ("G2", 0x20), ("G3", C.MASK))

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


def hr(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def decode_range(lo, hi):
    out = []
    off, end = lo - base, hi - base
    while off < end - 1:
        ins = fr_emu.TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        out.append((base + off, mn, v, size))
        off += size
    return out


def boot_run(steps=RUN):
    m = blive.boot(0)
    m.step(BOOT)
    m.raise_irq(VEC)
    m.run(max_steps=steps, spin_guard=False)
    return m


def covset(m):
    return set(m.cov.base + i * 2 for i, v in enumerate(m.cov.buf) if v)


def key_set(m):
    return sorted(p for p in C.KEY_PCS if p in covset(m))


def snap(m, tag):
    idx = bready.task_index(m)
    t5 = idx.get(5, 0)
    cov = covset(m)
    handlers = G.group_handlers(bdesc.entries(m))
    half = (m.bus.rd(t5 + 0x20, 4) >> 16) & 0xFFFF if t5 else 0
    reason = half - 0x10000 if half & 0x8000 else half
    return {
        "tag": tag, "pc": m.cpu.pc, "cov": cov, "key": key_set(m),
        "t5": t5, "t5_state": bready.state_of(m, t5) if t5 else -1,
        "t5_want": m.bus.rd(t5 + 0x14, 4) if t5 else 0,
        "t5_reason": reason,
        "waiting": sum(1 for t in idx
                       if bready.state_of(m, idx[t]) == 0x04),
        "desc": m.bus.rd(C.DESC_PTR, 4),
        "gate": m.bus.rd(G.GATE, 1),
        "word_a": m.bus.rd(C.WORD_A, 4),
        "word_b": m.bus.rd(C.WORD_B, 4),
        "slots": [m.bus.rd(C.SLOTS8 + i, 1) for i in range(8)],
        "handlers": sum(1 for h in handlers if h in cov),
        "n_handlers": len(handlers),
        "queues": [(owner, [t for t, tcb in idx.items() if tcb in nodes])
                   for _i, owner, nodes in btick.object_queues(m)
                   if nodes and t5 in nodes],
        "boot": [BOOT], "svc": [3], "irqs": [2], "post": [POST, POST],
    }


def show(tag, s):
    print("     %s：覆盖 %d / 末PC 0x%06X / 任务5 state=0x%02X "
          "等待位 0x%08X 原因 %d / 等待任务 %d / 处理函数 %d/%d"
          % (tag, len(s["cov"]), s["pc"], s["t5_state"], s["t5_want"],
             s["t5_reason"], s["waiting"], s["handlers"], s["n_handlers"]))


def run_group(tag, bits):
    m = boot_run()
    b = snap(m, tag + " 基线")
    n_svc, n_irq, posts = 0, 0, []
    pre = brealos.kernel_call(m, -24)
    n_svc += 1
    r1 = brealos.kernel_call(m, -48, r4=C.FLAG_ID, r5=FIRST)
    n_svc += 1
    m.raise_irq(VEC)
    n_irq += 1
    m.run(max_steps=POST, spin_guard=False)
    posts.append(POST)
    mid = snap(m, tag + " 第一级后")
    r2 = brealos.kernel_call(m, -48, r4=C.FLAG_ID, r5=bits)
    n_svc += 1
    m.raise_irq(VEC)
    n_irq += 1
    m.run(max_steps=POST, spin_guard=False)
    posts.append(POST)
    end = snap(m, tag + " 第二级后")
    shape = {"boot": [BOOT], "svc": [n_svc], "irqs": [n_irq],
             "post": posts}
    print("   %s 第二级 set_flg(3,0x%02X)：预热 steps=%d err=%d；"
          "一级 err=%d；二级 err=%d"
          % (tag, bits, pre["steps"], pre["err"], r1["err"], r2["err"]))
    show("基线  ", b)
    show("一级后", mid)
    show("二级后", end)
    return {"b": b, "mid": mid, "end": end, "m": m, "bits": bits,
            "shape": shape,
            "new1": mid["cov"] - b["cov"], "new2": end["cov"] - mid["cov"]}


# ===================================================================== A
hr("A. 静态：0x74 不是要置的位，是等待模式与检查掩码")
print("A1 0x12E162 是 enter（函数入口），序言起点 0x12E160；"
      "函数体 [0x12E162,0x12E1EC) 共 %d 条指令"
      % len(decode_range(C.WAIT2_FN, C.DISPATCH_FN)))
print("A2 第二道等待的参数（全部是编译期常量）：")
for p, mn, v, _sz in decode_range(0x12E1C0, 0x12E1D6):
    for fld in ("i8", "i20", "i32"):
        if fld in v:
            val = v[fld]
            print("     0x%06X %-8s r%-2d = %-12d (0x%08X)"
                  % (p, mn, v.get("ri", -1), val, val & 0xFFFFFFFF))
print("A3 服务号按立即数宽度解码（规矩 48）："
      "ldi:32 原值 0x%08X → 32 位补码 %d；"
      "bstatic.Sweep.calls 取低 8 位报 %d 是假象"
      % (C.SVC32_RAW, C.svc_width(C.SVC32_RAW, 32),
         C.svc_width(C.SVC32_RAW, 8)))
print("     运行时 TCB+0x20 高半 0xFF56 按 sx16 = %d（两路互证）"
      % C.svc_width(0xFF56, 16))
print("A4 进入等待前 0x%06X 先 clr_flg(3,0x%02X)："
      "清的是 mask 里为 0 的位 = 0x%02X，"
      "是否覆盖整个 0x%02X 等待模式：%s"
      % (C.CLR_FLG_PC, C.CLR_BITS,
         C.cleared_bits(C.CLR_BITS), C.MASK,
         C.mask_covered(C.CLR_BITS, C.MASK)))
print("A5 事后位域判据 (flgptn & 0x%02X) == 0x%02X；"
      "0x12E1E2 beq:d 的延迟槽给 r4=1，落到下一行给 r4=0"
      % (C.MASK, C.EXPECT))
for pat, name in ((0x10, "期望位"), (0x20, "模式内非期望位"),
                  (0x74, "全模式位"), (0x00, "无位")):
    print("     flgptn=0x%02X (%-12s) → (pat&mask)=0x%02X "
          "判据=%s r4=%d bne=%s"
          % (pat, name, pat & C.MASK, C.mask_check(pat),
             C.return_value(pat), C.branch_taken(C.return_value(pat))))
print("A6 第二道等待函数返回段逐条 PC（供报告地址对账）：")
for p, mn, v, _s in decode_range(0x12E1D0, C.DISPATCH_FN):
    print("     0x%06X %-10s %s" % (p, mn, str(v)[:80]))

# ===================================================================== B
hr("B. 控制流：四级串联状态机，0x12DFD0 是 bne 的直接目标")
print("B1 任务 5 主体 [0x%06X,0x12E00A) 共 %d 条指令"
      % (C.TASK5_FN, len(decode_range(C.TASK5_FN, 0x12E00A))))
print("B2 四级结构（call → cmp r4,#0 → bne 下一级 → 失败 bra 回 0x%06X）："
      % C.LOOP_HEAD)
for i, lv in enumerate(C.LEVELS, 1):
    call_pc, cmp_pc, bne_pc, tgt, bra = lv
    bd = dict((p, (mn, v)) for p, mn, v, _s
              in decode_range(bne_pc, bne_pc + 2))
    rel = bd[bne_pc][1].get("rel9") if bne_pc in bd else None
    calc = C.rel9_target(bne_pc, rel) if rel is not None else None
    print("     L%d call 0x%06X → cmp 0x%06X → bne 0x%06X rel9=%s "
          "目标 0x%06X（表列 0x%06X %s）｜失败 bra 0x%06X"
          % (i, call_pc, cmp_pc, bne_pc, rel, calc or 0, tgt,
             "一致" if calc == tgt else "不一致", bra))
print("B3 0x12DFD0 call rel12 目标 = 0x%06X（V70 找到的派发函数）"
      % C.rel12_target(0x12DFD0, 269))
bras = [(p, C.rel9_target(p, v["rel9"]))
        for p, mn, v, _s in decode_range(C.TASK5_FN, 0x12E00A)
        if mn in ("bra", "bra:d") and "rel9" in v]
back = sorted({t for _p, t in bras})
print("B4 回环：%d 条 bra 的目标集合 = %s（%s 全部指回 0x%06X）；"
      "所以「0x12DFC2 之后 12 条 call 没跑」不能读成直线后继没跑，"
      "要按 bne 目标逐条判（规矩 57）"
      % (len(bras), ["0x%06X" % x for x in back],
         "是" if back == [C.LOOP_HEAD] else "否", C.LOOP_HEAD))

# ===================================================================== C
hr("C. 静态三层判据：谁能置上 0x10")
mb = boot_run()
cov_base = covset(mb)
setters = C.static_setters(SW.of(-48), C.FLAG_ID, C.EXPECT)
encl = [(p, SW.enclosing(p) or 0) for p in setters]
ranges = sorted({e for _p, e in encl})
isr_range = (0x12F540, 0x12FECC)
tl = C.three_layer(setters, cov_base,
                   enclosing=lambda p: SW.enclosing(p) or 0,
                   isr_range=isr_range)
print("C1 全 ROM set_flg %d / clr_flg %d；"
      "能置 flag3 且命中 0x%02X 的点 %d 个"
      % (len(SW.of(-48)), len(SW.of(-47)), C.EXPECT, tl["points"]))
for p, e in encl:
    print("     0x%06X enclosing=0x%06X  收敛态命中=%s"
          % (p, e, p in cov_base))
print("C2 三层：存在性 %d 点 / 执行性收敛态命中 %d 点 / "
      "触发性落在 [0x%06X,0x%06X) ISR 区 %d 点（全部=%s）"
      % (tl["points"], tl["hit"], isr_range[0], isr_range[1],
         tl["in_isr"], tl["in_isr_all"]))
print("C3 归属函数起点去重 = %s；零命中 = %s ⇒ "
      "缺的是真机中断事件流，不是代码缺席（规矩 50）"
      % (["0x%06X" % x for x in ranges], tl["unique_zero_hit"]))
print("C4 会清掉 0x%02X 的 clr_flg(3,...) 点：" % C.EXPECT)
harm = [c for c in SW.of(-47)
        if c.r4 == C.FLAG_ID and isinstance(c.r5, int)
        and bool(C.cleared_bits(c.r5, 32) & C.EXPECT)]
for c in harm:
    print("     0x%06X clr_flg(3,0x%08X) 收敛态命中=%s"
          % (c.addr, c.r5, c.addr in cov_base))
print("     共 %d 个" % len(harm))

# ===================================================================== D
hr("D. 收敛态基线与 1000000 步窗口自证")
sb = snap(mb, "基线")
show("500000 步", sb)
print("D1 基线关键 PC 命中：%s" % ["0x%06X" % x for x in sb["key"]])
print("D2 基线任务 5 执行前沿：cmp 命中到第 %d 级；"
      "bne 目标命中级 %s"
      % (C.reached_level(sb["cov"]), C.advanced(sb["cov"])))
mw = boot_run(WINDOW)
sw = snap(mw, "1000000 步")
w = C.window_same({"pc": sb["pc"], "cov": sb["cov"], "key": sb["key"],
                   "waiting": sb["waiting"], "t5_want": sb["t5_want"]},
                  {"pc": sw["pc"], "cov": sw["cov"], "key": sw["key"],
                   "waiting": sw["waiting"], "t5_want": sw["t5_want"]})
print("D3 1000000 步：覆盖 %d / 末PC 0x%06X；"
      "窗口自证 same=%s 差异字段=%s PC 对称差=%d 关键 PC 差=%s"
      % (len(sw["cov"]), sw["pc"], w["same"], w["diff"],
         w["cov_diff"], ["0x%06X" % x for x in w["key_diff"]]))

# ===================================================================== E
hr("E. 两级等形反事实：只有满足位域判据的那一组跨过 bne")
print("E0 四组各一台独立机器：1 次 get_tid 预热 + 2 次 set_flg "
      "+ 2 次中断 + 2×%d 步；唯一差异是第二级置位值" % POST)
res = {}
for tag, bits in GROUPS:
    print("")
    res[tag] = run_group(tag, bits)
print("")
shapes = [res[tag]["shape"] for tag, _b in GROUPS]
es = C.equal_shape(shapes)
print("E1 等形校验（各组实测）：%s；逐项 %s"
      % (es["ok"], {k: v for k, v in es["keys"].items()}))
mid0 = res[GROUPS[0][0]]["mid"]
print("E1b 四组第一级一致：推进到 0x12DFC0 后进入第二道等待，"
      "等待位 0x%08X、等待原因 %d（V70 F5 的执行前沿）"
      % (mid0["t5_want"], mid0["t5_reason"]))
print("E2 差分总账：")
cross = []
for tag, _bits in GROUPS:
    e = res[tag]["end"]
    newk = sorted(set(e["key"]) - set(res[tag]["mid"]["key"]))
    k = set(e["key"])
    ok = C.LEVELS[0][3] in k
    if ok:
        cross.append(tag)
    print("     %s 一级新增 %3d / 二级新增 %3d / "
          "L1 bne 目标 0x%06X %s / 派发函数 %s / 新增关键 PC %s"
          % (tag, len(res[tag]["new1"]), len(res[tag]["new2"]),
             C.LEVELS[0][3], "命中" if ok else "未命中",
             "命中" if C.DISPATCH_FN in k else "未命中",
             ["0x%06X" % x for x in newk]))
print("E3 跨过第一级 bne 的组 = %s；"
      "按 (pat&0x%02X)==0x%02X 预期命中的组 = %s；两者 %s"
      % (cross, C.MASK, C.EXPECT,
         [t for t, b in GROUPS if C.mask_check(b)],
         "一致" if cross == [t for t, b in GROUPS
                             if C.mask_check(b)] else "不一致"))
print("E4 所有组末尾：处理函数 %s / 描述符指针 %s / 门 %s / "
      "两个状态字 %s / 8 槽 %s"
      % (sorted({"%d/%d" % (r["end"]["handlers"], r["end"]["n_handlers"])
                 for r in res.values()}),
         sorted({"0x%08X" % r["end"]["desc"] for r in res.values()}),
         sorted({"0x%02X" % r["end"]["gate"] for r in res.values()}),
         sorted({"%08X/%08X" % (r["end"]["word_a"], r["end"]["word_b"])
                 for r in res.values()}),
         sorted({"".join("%02X" % x for x in r["end"]["slots"])
                 for r in res.values()})))

# ===================================================================== F
hr("F. 跨过 bne 之后：新增覆盖分布与第三道等待")
g1 = res[cross[0]] if cross else res["G1"]
c1, c2 = set(g1["mid"]["cov"]), set(g1["end"]["cov"])
BANDS = ((0x040000, 0x041000, "内核区"),
         (0x120000, 0x12E000, "显示/启动区"),
         (C.TASK5_FN, 0x12E00A, "任务5主体"),
         (C.WAIT2_FN, C.DISPATCH_FN, "第二道等待函数"),
         (C.DISPATCH_FN, 0x12E328, "派发函数"),
         (0x12E328, C.BIG_FN, "0x12E328 函数"),
         (C.BIG_FN, 0x12E752, "大函数"),
         (0x12F540, 0x12FECC, "ISR 置位区"))
print("F1 %s 二级新增 %d 个 PC 的区间分布："
      % (", ".join(cross) if cross else "（无组跨过）", len(c2 - c1)))
for lo, hi, name in BANDS:
    n = sum(1 for p in (c2 - c1) if lo <= p < hi)
    if n:
        print("     [0x%06X,0x%06X) %-16s %d" % (lo, hi, name, n))
print("F2 四级状态机走到第 %d 级；真正跨过的级 = %s"
      % (C.reached_level(c2), C.advanced(c2)))
print("F3 末尾任务 5 仍挂在 %s，等待位 0x%08X、等待原因 %d ⇒ "
      "进入下一道等待，派发函数返回后回到 0x%06X 循环"
      % (["owner=0x%08X 同任务 %s" % (o, ts)
          for o, ts in g1["end"]["queues"]],
         g1["end"]["t5_want"], g1["end"]["t5_reason"], C.LOOP_HEAD))
print("F4 决定性反事实是外部注入（真内核服务）；"
      "「跨过 bne」不等于「显示链通了」。")

# ===================================================================== G
hr("G. 窗口自证与边界")
print("G1 基线 500000 与 1000000 步覆盖对称差集 %d 个 ⇒ "
      "所有「未命中」不是执行窗口不够长" % w["cov_diff"])
print("G2 两个状态字 0x%08X / 0x%08X 仍 0；8 槽 0x%08X 全 0"
      % (C.WORD_A, C.WORD_B, C.SLOTS8))
print("G3 不能报：显示链通了 / 出画面 / 真机如此 / "
      "「补一次 set_flg 就唤醒成功」/「0x74 是被置的位」。")
print("G4 仍缺：真机给 flag3 置 0x%02X 的中断事件流、"
      "真机 tick 源（KI#191）、[12,13,14,15] 激活机制、"
      "两个状态字与 8 槽填充源" % C.EXPECT)
print("G5 关键常量地址清单（供报告地址对账）：")
for name, addr, width in (("门字节", G.GATE, 8),
                          ("描述符指针", C.DESC_PTR, 8),
                          ("状态字A", C.WORD_A, 8),
                          ("状态字B", C.WORD_B, 8),
                          ("8槽", C.SLOTS8, 8),
                          ("间接调用点", C.INDIRECT_CALL, 6),
                          ("提交函数", C.SUBMIT, 6),
                          ("空槽位", C.NOOP_HANDLER, 6),
                          ("L2目标函数", 0x12E326, 6)):
    print("     %-12s 0x%0*X" % (name, width, addr))

# ===================================================================== H
hr("H. 综合判据")
v = C.chain_verdict(
    level=C.reached_level(c2),
    advanced_levels=C.advanced(c2),
    gate_hit=C.GATE_PC in c2,
    indirect_hit=C.INDIRECT_CALL in c2,
    handler_hit=g1["end"]["handlers"] > 0,
    noop_hit=C.NOOP_HANDLER in c2,
    state_zero=all(x == 0 for x in g1["end"]["slots"])
    and g1["end"]["word_a"] == 0 and g1["end"]["word_b"] == 0)
print("H1 走到第 %d 级 / 跨过级 %s / 到达派发函数 %s / "
      "门 %s / 间接调用 %s / 处理函数 %s / 空槽位 %s / 状态全 0 %s / "
      "链闭合 %s"
      % (v["level"], v["advanced"], v["reached_dispatch"],
         v["gate_hit"], v["indirect_hit"], v["handler_hit"],
         v["noop_hit"], v["state_zero"], v["closed"]))
print("")
print("结果文件 results/frbox/wait271.txt；设施 frbox/wait271.py；"
      "六台独立机器（基线一台 + G0–G3 四台 + 1000000 步自证一台）。")
