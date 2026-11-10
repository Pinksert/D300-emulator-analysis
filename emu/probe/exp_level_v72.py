# -*- coding: utf-8 -*-
"""V72 正式探针：五级链、0x12E1EC 两条数据早退路径与描述符指针填充（A–H 八段）。

承接 KI#278 第一条。设备策略与 V65–V71 收敛组逐字相同：
blive.boot(0) → step(191520) → 单次 raise_irq(0x2B) → run(500000)。
静态段只读 ROM；反事实段全部标注（写 SDRAM 字节 + kernel_call 注入真内核
服务，都不是固件行为）。七台独立机器：基线/窗口各一台、G0–G3 四台、
G4 不等形边界一台。
"""
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.getcwd())

import fr_emu
from frbox import (bstatic, blive, bready, bdesc, brealos, btick,
                   descgate69 as G, wait271 as W, level72 as C)

BOOT = blive.BOOT_STEPS
VEC = 0x2B
RUN = 500000
POST = 200000
WINDOW = 1000000

GROUPS = (("G0", 0x00, 0x00), ("G1", 0x00, 0x01),
          ("G2", 0x00, 0x46), ("G3", 0x46, 0x01))
EDGE = ("G4", 0x00, 0x01)

SW, m0 = bstatic.sweep_of_type("d300-b")
rom, base = m0.bus.rom, m0.bus.rom_base


def hr(title):
    print("")
    print("=" * 78)
    print(title)
    print("=" * 78)


def dec(lo, hi):
    out = []
    off, end = lo - base, hi - base
    while off < end - 1:
        ins = fr_emu.TABLE.decode(rom, off)
        if ins is None:
            off += 2
            continue
        mn, _bs, v, size, _w = ins
        out.append((base + off, mn, dict(v), size))
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


def nine(m):
    return [m.bus.rd(C.CUR_BASE + i, 1) for i in range(9)]


def snap(m, tag, cov=None):
    cov = cov if cov is not None else covset(m)
    idx = bready.task_index(m)
    t5 = idx.get(5, 0)
    half = (m.bus.rd(t5 + 0x20, 4) >> 16) & 0xFFFF if t5 else 0
    handlers = G.group_handlers(bdesc.entries(m))
    return {
        "tag": tag, "cov": cov, "pc": m.cpu.pc,
        "key": [p for p in C.KEY_PCS if p in cov],
        "nine": nine(m),
        "idx_byte": m.bus.rd(C.IDX_BYTE, 1),
        "valid_byte": m.bus.rd(C.VALID_BYTE, 1),
        "clear_byte": m.bus.rd(C.CLEAR_BYTE, 1),
        "desc": m.bus.rd(C.DESC_PTR, 4),
        "gate": m.bus.rd(G.GATE, 1),
        "word_a": m.bus.rd(C.WORD_A, 4), "word_b": m.bus.rd(C.WORD_B, 4),
        "slots": [m.bus.rd(C.SLOTS8 + i, 1) for i in range(8)],
        "t5_state": bready.state_of(m, t5) if t5 else -1,
        "t5_want": m.bus.rd(t5 + 0x14, 4) if t5 else 0,
        "t5_reason": half - 0x10000 if half & 0x8000 else half,
        "handlers": sum(1 for h in handlers if h in cov),
        "n_handlers": len(handlers),
        "queues": [(owner, [t for t, tcb in idx.items() if tcb in nodes])
                   for _i, owner, nodes in btick.object_queues(m)
                   if nodes and t5 in nodes],
    }


def show(tag, s):
    print("     %s：覆盖 %d / 末PC 0x%06X / 九字节 %s / "
          "DA83=%02X DA84=%02X D858=%08X"
          % (tag, len(s["cov"]), s["pc"],
             " ".join("%02X" % x for x in s["nine"]),
             s["idx_byte"], s["valid_byte"], s["desc"]))
    print("          任务5 state=0x%02X 等待位 0x%08X 原因 %d / "
          "处理函数 %d/%d / 门 %02X / 状态字 %08X/%08X"
          % (s["t5_state"], s["t5_want"], s["t5_reason"],
             s["handlers"], s["n_handlers"], s["gate"],
             s["word_a"], s["word_b"]))


def run_group(tag, v83, v84, second=True):
    m = boot_run()
    c0 = covset(m)
    b = snap(m, tag + " 基线", c0)
    pre = brealos.kernel_call(m, -24)
    r1 = brealos.kernel_call(m, -48, r4=C.FLAG_ID, r5=W.WANTPTN_1)
    n_svc, n_irq, posts = 2, 1, []
    m.raise_irq(VEC)
    m.run(max_steps=POST, spin_guard=False)
    posts.append(POST)
    c1 = covset(m)
    mid = snap(m, tag + " 一级后", c1)
    # 唯一差异：两个 SDRAM 数据字节（反事实写入，不是固件行为）
    m.bus.wr(C.IDX_BYTE, 1, v83)
    m.bus.wr(C.VALID_BYTE, 1, v84)
    if second:
        r2 = brealos.kernel_call(m, -48, r4=C.FLAG_ID, r5=C.EXPECT)
        n_svc += 1
        n_irq += 1
        m.raise_irq(VEC)
        m.run(max_steps=POST, spin_guard=False)
    else:
        r2 = {"err": None}
        m.run(max_steps=POST, spin_guard=False)
    posts.append(POST)
    c2 = covset(m)
    end = snap(m, tag + " 二级后", c2)
    ph = C.path_hits(c2)
    return {
        "tag": tag, "v83": v83, "v84": v84, "second": second,
        "b": b, "mid": mid, "end": end,
        "pre": pre["steps"], "err": (r1["err"], r2["err"]),
        "new1": len(c1 - c0), "new2": len(c2 - c1),
        "path": ph["verdict"], "hits": ph,
        "L2": W.LEVELS[1][3] in c2,
        "dispatch_hit": C.DISPATCH_FN in c2,
        "shape": {"boot": [BOOT], "svc": [n_svc], "irqs": [n_irq],
                  "post": posts},
    }


# ===================================================================== A
hr("A. 静态：任务 5 是四级串联 + 第五个 call")
body = dec(C.TASK5_FN, 0x12E00A)
print("A1 任务 5 主体 [0x%06X,0x12E00A) 共 %d 条指令；"
      "循环头 0x%06X 调第一道等待 0x%06X"
      % (C.TASK5_FN, len(body), C.LOOP_HEAD, C.WAIT1_FN))
bmap = {p: (mn, v) for p, mn, v, _s in body}
print("A2 四级 call 目标（rel12 实测）与失败分支：")
for i, lv in enumerate(C.LEVELS, 1):
    call_pc, cmp_pc, bne_pc, tgt, bra = lv
    mn, v = bmap[call_pc]
    calc = C.rel12_target(call_pc, v["rel12"])
    print("     L%d 0x%06X %-8s rel12=%-4d -> 0x%06X（表列 0x%06X %s）"
          " ｜ cmp 0x%06X ｜ bne 0x%06X -> 0x%06X ｜ 失败 bra 0x%06X"
          % (i, call_pc, mn, v["rel12"], calc, C.LEVEL_TARGETS[i - 1],
             "一致" if calc == C.LEVEL_TARGETS[i - 1] else "不一致",
             cmp_pc, bne_pc, tgt, bra))
mn, v = bmap[C.FIFTH_CALL]
print("A3 L4 通过后 0x%06X call rel12=%d -> 0x%06X（第五个 call，"
      "V69 的门 0x%06X 与间接调用 0x%06X 都在这个函数里）"
      % (C.FIFTH_CALL, v["rel12"], C.rel12_target(C.FIFTH_CALL, v["rel12"]),
         C.GATE_PC, C.INDIRECT_CALL))
print("A4 四级失败路径 call:d 目标集合 = %s（全部是 0x%06X 序言）"
      % (sorted({"0x%06X" % C.rel12_target(p, bmap[p][1]["rel12"])
                 for p in C.FAILURE_CALLS}),
         C.BIG_PROLOGUE))
print("A4b 四级失败路径 call:d 的 PC 清单（供报告地址对账）：%s"
      % ["0x%06X" % p for p in C.FAILURE_CALLS])
bras = {C.rel9_target(p, v["rel9"]) for p, mn, v, _s in body
        if mn in ("bra", "bra:d") and "rel9" in v}
print("A5 回环：%d 条 bra 目标集合 = %s；五级链目标 = %s"
      % (len([1 for p, mn, v, _s in body
              if mn in ("bra", "bra:d") and "rel9" in v]),
         ["0x%06X" % x for x in sorted(bras)],
         ["0x%06X" % x for x in C.chain_targets()]))

# ===================================================================== B
hr("B. 0x12E1EC 三条返回路径：两个数据字节决定 r4")
d1 = dec(C.DISPATCH_FN, C.WAIT3_FN)
print("B1 函数体 [0x%06X,0x%06X) 共 %d 条；ret 在 0x%06X"
      % (C.DISPATCH_FN, C.WAIT3_FN, len(d1), C.DISPATCH_RET))
print("B2 早退 A（索引越界）特征 PC：%s"
      % ["0x%06X" % x for x in C.EARLY_A])
print("     0x12E212 ldub @DA83,r0 → cmp r0,#0 / beq 0x12E232；"
      "非零时 cmp r1=70,r0 / blt 0x12E22E，否则 0x12E226 起早退")
print("B3 早退 B（无待显示项）特征 PC：%s"
      % ["0x%06X" % x for x in C.EARLY_B])
print("     0x12E232 ldub @DA84,r0 → cmp r0,#0 / bne 0x12E240；"
      "为 0 时 0x12E238 起早退")
print("B4 两条早退都 stb @%s=0，并在 bra:d 延迟槽 0x12E22C / "
      "0x12E23E 给 r4=0；正常路径 0x12E320 ldi:8 #1,r4 给 r4=1"
      % "0x9F58DA82")
print("B5 静态判据 early_path(DA83, DA84) 四组预测：")
for tag, a, b in GROUPS:
    p = C.early_path(a, b)
    print("     %s DA83=0x%02X DA84=0x%02X -> %-8s r4=%d L2 bne=%s"
          % (tag, a, b, p, C.return4(p), C.level_advanced(C.return4(p))))
print("B6 正常路径写描述符指针：0x%06X st @r7,r0；"
      "算式 0x%06X + idx*0x%X（描述符表 %d 条）"
      % (0x12E274, C.DESC_TABLE, C.DESC_STRIDE, C.DESC_COUNT))
print("     0x12E2EC 起还写 DA7C / DA7D / DA7F 三个游标字节与 "
      "D854 / D850 / D87C 三个指针")
print("B7 开头判据逐条 PC（供报告地址对账）：")
for q, mn, v, _s in dec(0x12E20C, 0x12E244):
    print("     0x%06X %-10s %s" % (q, mn, str(v)[:66]))
print("B8 正常路径的两条补充 PC：描述符表基址 0x12E26A / "
      "共同返回 0x12E322；逐条：")
for q, mn, v, _s in dec(0x12E26A, 0x12E26C):
    print("     0x%06X %-10s %s" % (q, mn, str(v)[:66]))
for q, mn, v, _s in dec(0x12E322, 0x12E326):
    print("     0x%06X %-10s %s" % (q, mn, str(v)[:66]))

# ===================================================================== C
hr("C. 口径证伪：enclosing 函数边界与服务号宽度")
rows = [(p, mn) for p, mn, _v, _s in dec(C.BIG_FN, C.NOOP_HANDLER)]
bounds = C.function_bounds(rows)
rets = [p for p, mn in rows if mn in ("ret", "ret:d")]
print("C1 [0x%06X,0x%06X) 有 %d 个 ret：%s"
      % (C.BIG_FN, C.NOOP_HANDLER, len(rets),
         ["0x%06X" % x for x in rets]))
print("C2 真实函数起点（按 stm1/enter + 间隔 >0x40 拆）：%s"
      % ["0x%06X" % s for s, _e in bounds])
emap = {p: SW.enclosing(p) or 0 for p, _mn in rows}
mis = C.enclosing_mismatch(bounds, emap)
print("C3 enclosing() 把 %d 个真实起点归并到别处：" % len(mis))
for s, e, wrong in mis:
    print("     0x%06X (ret 0x%06X) -> enclosing=0x%06X"
          % (s, e, wrong or 0))
print("C4 ⇒ 规矩 59：V70「0x12E490 是 262 条大函数、0x12E69E/0x12E6BA "
      "在它内部」应更正为「0x12E674 是独立函数，r10 是它自己的局部标志」")
print("C5 规矩 48：区内 6 个 Sweep.calls 报 svc=%d 的点全是 "
      "ldi:32 #0x%08X（%d 位补码 %d）取低 8 位的假象"
      % (C.SVC8_FALSE, C.SVC32_RAW, 32, C.svc_width(C.SVC32_RAW, 32)))
for pc, want in sorted(C.WAIT_PCS.items()):
    print("     0x%06X wai_flg(3,0x%02X)  低8位假象=%s"
          % (pc, want, C.is_low8_artifact(C.SVC32_RAW)))
print("C6 L3 目标 0x%06X 是第三道等待：0x12E3F6 wai_flg(3,0x74) + "
      "0x12E3FE/0x12E402 的 &0x74 == 0x10 判据（与 V71 第二道等待同构）"
      % C.WAIT3_FN)
print("C7 L3 返回段逐条 PC（供报告地址对账）：")
for q, mn, v, _s in dec(0x12E3F0, 0x12E410):
    print("     0x%06X %-10s %s" % (q, mn, str(v)[:66]))

# ===================================================================== D
hr("D. 收敛态基线与 1000000 步窗口自证")
mb = boot_run()
cb = covset(mb)
sb = snap(mb, "500000 步", cb)
show("基线", sb)
print("D1 九字节 DA7C..DA84 全 0 = %s；DA83=%02X DA84=%02X ⇒ "
      "early_path = %s（V69「游标九字节全 0」落到这里）"
      % (all(x == 0 for x in sb["nine"]), sb["idx_byte"],
         sb["valid_byte"], C.early_path(sb["idx_byte"], sb["valid_byte"])))
print("D2 基线 0x%06X 命中 %s；描述符指针 D858=%08X"
      % (C.DISPATCH_FN, C.DISPATCH_FN in cb, sb["desc"]))
print("D2b 基线任务 5 等待队列归属（V71 的 0x680008D8 出处）：%s"
      % ["owner=0x%08X 同任务 %s" % (o, ts)
         for o, ts in sb["queues"]])
mw = boot_run(WINDOW)
cw = covset(mw)
sw = snap(mw, "1000000 步", cw)
w = C.window_same({"pc": sb["pc"], "cov": cb, "key": sb["key"],
                   "t5_want": sb["t5_want"], "desc": sb["desc"]},
                  {"pc": sw["pc"], "cov": cw, "key": sw["key"],
                   "t5_want": sw["t5_want"], "desc": sw["desc"]})
print("D3 1000000 步：覆盖 %d / 末PC 0x%06X；窗口自证 same=%s "
      "差异字段=%s PC 对称差=%d 关键 PC 差=%s"
      % (len(cw), sw["pc"], w["same"], w["diff"], w["cov_diff"],
         ["0x%06X" % x for x in w["key_diff"]]))

# ===================================================================== E
hr("E. 四组等形反事实：只有 DA84 非 0 且 DA83 不越界才跨过 L2")
print("E0 四组各一台独立机器：1 次预热 + 2 次 set_flg + 2 次中断 + "
      "2×%d 步；唯一差异是一级后写入 DA83 / DA84 两个字节" % POST)
res = {}
for tag, v83, v84 in GROUPS:
    print("")
    res[tag] = run_group(tag, v83, v84)
    r = res[tag]
    print("   %s DA83=0x%02X DA84=0x%02X：预热 steps=%d err=%s；"
          "一级新增 %d / 二级新增 %d"
          % (tag, v83, v84, r["pre"], r["err"], r["new1"], r["new2"]))
    show("基线  ", r["b"])
    show("一级后", r["mid"])
    show("二级后", r["end"])
    print("           实测路径 %s（早退A %d/%d 早退B %d/%d 正常 %d/%d）"
          " / L2 bne 目标 0x%06X %s"
          % (r["path"], r["hits"]["early_A"], r["hits"]["max"][0],
             r["hits"]["early_B"], r["hits"]["max"][1],
             r["hits"]["normal"], r["hits"]["max"][2],
             W.LEVELS[1][3], "命中" if r["L2"] else "未命中"))
shapes = [res[t]["shape"] for t, _a, _b in GROUPS]
es = C.equal_shape(shapes)
print("")
print("E1 等形校验（各组实测）：%s；逐项 %s"
      % (es["ok"], {k: v for k, v in es["keys"].items()}))
print("E2 路径 / L2 总账：")
for tag, a, b in GROUPS:
    r = res[tag]
    print("     %s 静态预测 %-8s 实测 %-8s %s ｜ L2 %s ｜ D858 %08X"
          % (tag, C.early_path(a, b), r["path"],
             "一致" if C.early_path(a, b) == r["path"] else "不一致",
             "跨过" if r["L2"] else "未跨", r["end"]["desc"]))
print("E3 跨过 L2 的组 = %s；静态预测可跨的组 = %s；两者 %s"
      % ([t for t, a, b in GROUPS if res[t]["L2"]],
         [t for t, a, b in GROUPS if C.level_advanced(
             C.return4(C.early_path(a, b)))],
         "一致" if [t for t, a, b in GROUPS if res[t]["L2"]] ==
         [t for t, a, b in GROUPS if C.level_advanced(
             C.return4(C.early_path(a, b)))] else "不一致"))
print("E4 所有组末尾：处理函数 %s / 门 %s / 两个状态字 %s / 8 槽 %s"
      % (sorted({"%d/%d" % (r["end"]["handlers"], r["end"]["n_handlers"])
                 for r in res.values()}),
         sorted({"%02X" % r["end"]["gate"] for r in res.values()}),
         sorted({"%08X/%08X" % (r["end"]["word_a"], r["end"]["word_b"])
                 for r in res.values()}),
         sorted({"".join("%02X" % x for x in r["end"]["slots"])
                 for r in res.values()})))

# ===================================================================== F
hr("F. 跨过 L2 之后：描述符指针、九字节与第三道等待")
g1 = res["G1"]
print("F1 地址算式核对（两点独立）：")
for idx, got in ((0x01, res["G1"]["end"]["desc"]),
                 (0x46, res["G2"]["end"]["desc"])):
    calc = C.desc_ptr(idx)
    print("     DA84=0x%02X -> 0x%08X（实测 0x%08X）%s"
          % (idx, calc, got, "一致" if calc == got else "不一致"))
print("F2 G0 早退 B / G1 正常：D858 %08X -> %08X；"
      "九字节 %s -> %s"
      % (res["G0"]["end"]["desc"], g1["end"]["desc"],
         " ".join("%02X" % x for x in res["G0"]["end"]["nine"]),
         " ".join("%02X" % x for x in g1["end"]["nine"])))
print("F3 失败路径：G0 末尾任务5 等待位 0x%08X 原因 %d，"
      "命中 0x%06X（大函数里的 wai_flg(3,0x10)）=%s ⇒ "
      "这就是 V71 F3「等待位 0x10」的出处"
      % (res["G0"]["end"]["t5_want"], res["G0"]["end"]["t5_reason"],
         0x12E56A, 0x12E56A in res["G0"]["end"]["cov"]))
print("F4 正常路径：G1 末尾任务5 等待位 0x%08X 原因 %d ⇒ "
      "L3 的 0x12E3F6 wai_flg(3,0x74) 重新挂起；"
      "一次 set_flg(3,0x10) 只够过一级"
      % (g1["end"]["t5_want"], g1["end"]["t5_reason"]))
c0, c2 = res["G0"]["end"]["cov"], g1["end"]["cov"]
print("F5 G1 相对 G0 新增 %d / 消失 %d 个 PC；新增区间分布："
      % (len(c2 - c0), len(c0 - c2)))
for lo, hi, name in ((0x040000, 0x042000, "内核区"),
                     (C.TASK5_FN, 0x12E00A, "任务5主体"),
                     (C.DISPATCH_FN, C.WAIT3_FN, "0x12E1EC 派发"),
                     (C.WAIT3_FN, C.BIG_FN, "0x12E326 区"),
                     (C.BIG_FN, C.NOOP_HANDLER, "0x12E490 区")):
    n = sum(1 for p in (c2 - c0) if lo <= p < hi)
    if n:
        print("     [0x%06X,0x%06X) %-16s %d" % (lo, hi, name, n))
print("F6 五级链关键 PC（G1 末尾覆盖）：")
for name, p in (("L2 派发函数", C.DISPATCH_FN),
                ("L2 bne 目标", W.LEVELS[1][3]),
                ("L3 等待函数", C.WAIT3_FN),
                ("L4 bne 目标", W.LEVELS[3][3]),
                ("第五个 call 目标", C.FIFTH_FN),
                ("前置门", C.GATE_PC), ("间接调用点", C.INDIRECT_CALL),
                ("提交函数", C.SUBMIT), ("空槽位", C.NOOP_HANDLER)):
    print("     0x%06X %-16s %s"
          % (p, name, "命中" if p in c2 else "未命中"))

# ===================================================================== G
hr("G. 边界、不等形对照与关键常量地址")
r4 = run_group(*EDGE, second=False)
print("G1 不等形边界 G4（只写 DA83/DA84、不给二级 set_flg）："
      "二级新增 %d / 0x%06X 命中 %s / L2 %s ⇒ 写数据门 ≠ 给调度事件（规矩 53）"
      % (r4["new2"], C.DISPATCH_FN, C.DISPATCH_FN in r4["end"]["cov"],
         "跨过" if r4["L2"] else "未跨"))
print("G2 窗口自证：500000 与 1000000 步覆盖对称差 %d 个 ⇒ "
      "所有「未命中」不是执行窗口不够长" % w["cov_diff"])
print("G3 全部反事实（写 SDRAM 字节 + kernel_call 注入真内核服务）"
      "都是外部补全，不是固件行为；不能报显示链通了 / 出画面 / 真机如此 / "
      "「补一次 set_flg 就唤醒成功」")
print("G4 仍缺：真机给 DA84 填待显示项的中断事件流、真机给 flag3 置 0x10 "
      "的硬件事件（V71 定位到 0x12F540 ISR）、L3 所需 0x74 的置位源、"
      "真机 tick 源（KI#191）、[12,13,14,15] 激活机制、"
      "两个状态字与 8 槽填充源")
print("G5 关键常量地址清单（供报告地址对账）：")
for name, addr, width in (
        ("游标九字节", C.CUR_BASE, 8), ("游标 DA7D", 0x9F58DA7D, 8),
        ("游标 DA7F", 0x9F58DA7F, 8),
        ("索引 DA83", C.IDX_BYTE, 8), ("有效 DA84", C.VALID_BYTE, 8),
        ("早退清位", C.CLEAR_BYTE, 8),
        ("描述符指针", C.DESC_PTR, 8), ("描述符表", C.DESC_TABLE, 8),
        ("指针 D850", 0x9F58D850, 8), ("指针 D854", 0x9F58D854, 8),
        ("指针 D87C", 0x9F58D87C, 8),
        ("门字节", G.GATE, 8), ("状态字A", C.WORD_A, 8),
        ("状态字B", C.WORD_B, 8), ("8槽", C.SLOTS8, 8),
        ("L1等待", C.WAIT2_FN, 6), ("L2派发", C.DISPATCH_FN, 6),
        ("L3等待", C.WAIT3_FN, 6), ("L4", C.L4_FN, 6),
        ("第五call", C.FIFTH_FN, 6), ("大函数序言", C.BIG_PROLOGUE, 6),
        ("前置门", C.GATE_PC, 6), ("间接调用", C.INDIRECT_CALL, 6),
        ("提交函数", C.SUBMIT, 6), ("空槽位", C.NOOP_HANDLER, 6),
        ("失败等待", 0x12E56A, 6), ("L3等待点", 0x12E3F6, 6)):
    print("     %-12s 0x%0*X" % (name, width, addr))

# ===================================================================== H
hr("H. 综合判据")
v = C.chain_verdict(
    path=g1["path"], l2_hit=g1["L2"],
    desc_ptr_value=g1["end"]["desc"],
    gate_hit=C.GATE_PC in c2, indirect_hit=C.INDIRECT_CALL in c2,
    handler_hit=g1["end"]["handlers"] > 0,
    state_zero=all(x == 0 for x in g1["end"]["slots"])
    and g1["end"]["word_a"] == 0 and g1["end"]["word_b"] == 0)
print("H1 G1 路径 %s / 跨过 L2 %s / 描述符指针已填 %s (0x%08X) / "
      "门 %s / 间接调用 %s / 处理函数 %s / 状态全 0 %s / 链闭合 %s"
      % (v["path"], v["l2_crossed"], v["desc_ptr_filled"], v["desc_ptr"],
         v["gate_hit"], v["indirect_hit"], v["handler_hit"],
         v["state_zero"], v["closed"]))
print("H2 KI#278 第一条已结：0x12E1EC 返回 0 是 DA84==0 的数据早退，"
      "不是空实现；0x12DFE0 -> 0x12E326 是第三道等待，"
      "与 0x12E162 同构的 (flgptn & 0x74)==0x10 判据")
print("")
print("结果文件 results/frbox/level72.txt；设施 frbox/level72.py；"
      "七台独立机器（基线/1000000 步窗口各一台 + G0–G3 四台 + G4 边界一台）。")
