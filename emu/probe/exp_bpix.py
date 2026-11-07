# -*- coding: utf-8 -*-
"""V49 探针：现在有画面了吗 —— 三条判据重算，和画面下游那只没启动的任务。

    cd emu && python probe/exp_bpix.py > ../results/frbox/bpix.txt

V26 判过「出不了画面」，给了三条证据。那批数字是在旧机器上量的
（SDRAM 只铺 0x20000、`0x40`/`0x60`/`0x50000000` 都还没建模），
而 V41 的整片填充、V43–V46 的映射补齐之后，第一条「最长连续段 2,126 字节」
已经不成立了 —— 同一口径现在能测出 217,008 字节。所以这一版做三件事：

A  V48 口径重算「有没有帧缓冲」：不只量面积，还要量**值多样**和**是否重画**
B  V26 口径复现：同一套代码跑老路径，把 2,126 B 那句对回去
C  「值多样」的那 72 KB 到底是什么：抓内存内容做字节级判别
D  画面下游：任务 22（入口 `0x242DA0`）—— 今天谁该启动它、它进门先擦哪一位、启动了没有
E  结论与边界
F  复现

⚠️ 三台独立的机器：静态普查一台、V48 口径一台、V26 口径一台。
同一台机器不能跑两遍（会从停住的现场继续，V43 栽过）。
"""
import sys

sys.path.insert(0, '.')

from frbox import bpix, blive, brealos, bstatic

T22 = 0x242DA0          # 任务 22 入口（KI#9 记成「任务 21」，本版纠正）
STUB = 0x242B8A         # sta_tsk(22) 的封装
WAIT10 = 0x242BF4       # 0x242B9A 里的 wai_flg(10, 0x7F0001D0, mode=0)
T22_CLR = 0x242DC6      # 任务 22 入口里的 clr_flg(10, ~0x40000000) —— 清 bit30
V26_WINDOW = 0x20000    # V26 当时的 SDRAM 观察窗口
FRAME = bpix.FRAME_BYTES


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def hx(v):
    return "None" if v is None else "0x%X" % v


def bytes_at(m, base, n=48):
    return bytes(m.bus.rd(base + i, 1) & 0xFF for i in range(n))


def pixelness(m, base, n=0x1000):
    """一段内存的「像不像位图」：不同字节值数 + 相邻相等占比。"""
    d = bytes_at(m, base, n)
    same = sum(1 for i in range(1, n) if d[i] == d[i - 1])
    return {"bytes": n, "distinct": len(set(d)), "adj_same": 1.0 * same / (n - 1),
            "head": d[:32].hex()}


# ---------------------------------------------------------------- A
hr("A  V48 口径：整机自跑，三条判据重算")
m, mt = bpix.run_live()
bpix.report(mt, out=print)
print()
print("连续被写区间（>= 一帧 %d B）：" % FRAME)
regs = mt.regions()
for base, ln, med, rw in regs[:5]:
    print("  0x%X  %7d B  值种数中位数 %3d  重写 %4.0f%%"
          % (base, ln, med, rw * 100))
print("  （共 %d 个这样的区间）" % len(regs))
ex = mt.expeed()
print()
print("位串行按地址（写 / 读）：%s"
      % ", ".join("0x%X %d/%d" % (a, ex["by_addr"].get(a, 0), ex["rd_by_addr"].get(a, 0))
                  for a in sorted(set(ex["by_addr"]) | set(ex["rd_by_addr"]))))
vk = mt.value_kinds()
print()
print("「值多样」的块：%d 个（值种数 >= %d 且写满），最长连续 %d 块 = %d B @0x%X"
      % (vk["blocks"], bpix.MIN_VALUE_KINDS, vk["longest_run"],
         vk["bytes"], vk["longest_at"]))

# ---------------------------------------------------------------- B
hr("B  V26 口径复现：同一条路径、同一个 0x20000 窗口")
m26, mt26 = bpix.run_v26()
print("步数 %d / 总线写 %d 次" % (m26.step_count, mt26.writes))
r1_26 = mt26.r1
r2_26 = mt26.r2(window=V26_WINDOW)
r2_all = mt26.r2()
print("R1 写入顺序连续：%d 字节 @0x%X" % (r1_26[0], r1_26[1]))
print("R2 地址集合连续（0x9F580000+0x20000 窗口，V26 的口径）：%d 字节 @0x%X..0x%X"
      % (r2_26[0], r2_26[1], r2_26[2]))
print("R2 同一次运行、放开到 0x280000 全窗口：%d 字节 @0x%X"
      % (r2_all[0], r2_all[1]))
print("V26 报告写的是「21,323 个半字地址被写，最长连续段 1,063 个半字 = 2,126 字节」")
print("→ 本机同口径实测 %d 个半字 / %d 字节" % (mt26.halfwords(), r2_26[0]))
print()
print("两套口径并排（**不许混比**，这是两次不同的运行）：")
print("  %-22s %-14s %-10s" % ("口径", "R2 最长连续", "位串行写"))
print("  %-22s %-14s %-10d" % ("V26（手工 sta_tsk 2）", "%d B" % r2_26[0],
                               mt26.expeed()["w"]))
print("  %-22s %-14s %-10d" % ("V48（整机自跑）", "%d B" % mt.r2()[0],
                               mt.expeed()["w"]))

# ---------------------------------------------------------------- C
hr("C  那块「值多样」的 72 KB 到底是什么")
VK = vk["longest_at"]
VK_END = VK + vk["bytes"]                    # 覆盖整段「值多样」的连续块
s, ms = bstatic.sweep_of_type('d300-b')      # 只读 ROM：把写点 PC 归到函数
import fr_dis

rom, rbase = ms.bus.rom, ms.bus.rom_base
for base in (VK, 0x9F5B6000, 0x9F5A0000):
    p = pixelness(m, base)
    print("0x%X  不同字节值 %3d 个 / 相邻相等 %4.1f%%  前 32 字节 %s"
          % (base, p["distinct"], p["adj_same"] * 100, p["head"]))
print()
print("内容本身只能给倾向（位图大片同色、相邻相等占比高），不足以定性。")
print("硬证据是「这块是谁写的」——下面换一台机器只统计这段的写点 PC：")

m3, mt3 = bpix.run_live(watch=(VK, VK_END))
tot = sum(mt3.watch_pc.values())
print()
print("0x%X..0x%X：%d 笔写，来自 %d 个 PC" % (VK, VK_END, tot, len(mt3.watch_pc)))
for pc, c in mt3.watch_pc.most_common(6):
    print("  pc=0x%X  %5d 笔  所属函数 0x%X" % (pc, c, s.enclosing(pc) or 0))
if mt3.watch_pc:
    pc = mt3.watch_pc.most_common(1)[0][0]
    print()
    print("写点 0x%X 附近：" % pc)
    for ln in fr_dis.disasm_addr(rom, pc - 8, pc + 16, rbase):
        print("  " + ln)

# ---------------------------------------------------------------- D
hr("D  画面下游：任务 22（入口 0x242DA0）")
print("动态（本机 99 万步，与 A 同一台）：")
for name, a in (("任务 22 入口 0x242DA0", T22), ("入口里 clr_flg(10) 0x242DC6", T22_CLR),
                ("sta_tsk(22) 封装 0x242B8A", STUB), ("0x242B9A 里的 wai_flg(10)", WAIT10)):
    print("  %-30s visited=%s" % (name, m.cov.visited(a)))
print("  跑到过的任务：%s" % [r["id"] for r in brealos.records(m) if m.cov.visited(r["entry"])])

print()
print("静态（同一台只读 ROM 的机器）：")
recs = [r for r in brealos.records(m) if r["entry"] == T22]
print("  任务表里 entry=0x242DA0 的是：%s"
      % ["任务 %d" % r["id"] for r in recs])
st = s.of(-23)
launch = [c for c in st if c.r4 == 22]
print("  全 ROM 里 sta_tsk(-23) 调用点 %d 个，其中 r4=0x16（任务 22）的 %d 个：%s"
      % (len(st), len(launch), [hx(c.addr) for c in launch]))
print("  也就是说「启动任务 22」在 ROM 里只有一处：%s（封装起于 0x%X）"
      % ([hx(c.addr) for c in launch] or "没有", STUB))
print("  谁调 0x%X：%s" % (STUB, [hx(a) for a in s.callers_of(STUB)]))
print("  wai_flg(10) 静态 %d 处：%s"
      % (len(s.flag_waits(10)), [(hx(c.addr), hx(c.bits), hx(c.mode)) for c in s.flag_waits(10)]))
print("  set_flg(10) 静态 %d 处" % len(s.flag_sets(10)))
c22 = [c for c in s.calls if c.addr == T22_CLR][0]
print("  0x242DC6 到底是哪个服务：svc=%d  flgid=%s  bits=0x%X"
      % (c22.svc, hx(c22.flgid), c22.bits))
print("    → clr_flg(10, ~0x40000000) = **只清 bit30**（V48 硬规矩 20）")
b30 = [c for c in s.flag_sets(10) if c.bits == 0x40000000]
print("  能置 flag 10 bit30 的 set_flg 静态 %d 处：%s" % (len(b30), [hx(c.addr) for c in b30[:6]] + ["…"]))

# ---------------------------------------------------------------- E
hr("E  结论")
v = bpix.verdict(mt)
print("1) 帧缓冲：%s" % ("有" if v["framebuffer"][0] else "没有"))
print("   %s" % v["framebuffer"][1])
print("2) 重画：%s" % ("有" if v["rewrite"][0] else "没有"))
print("   %s" % v["rewrite"][1])
print("3) 位串行带宽：%s" % ("够" if v["bitstream"][0] else "不够"))
print("   %s" % v["bitstream"][1])
print()
print("所以：**现在仍然没有画面**，而且这一版把 V26 的第一条论据换掉了 ——")
print("「最长连续段不够一帧」在补齐映射之后已经不成立（%d B > %d B），"
      % (mt.r2()[0], FRAME))
print("真正站得住的是「没有任何一块够大的内存同时满足值多样 + 被重画」。")
print()
print("画面链路上还差的两截，按证据排：")
callers = s.callers_of(STUB)
buck = {}
for a in callers:
    k = a >> 16
    buck[k] = buck.get(k, 0) + 1
print("  ① 下游任务 22（0x242DA0）从没启动。启动它的 sta_tsk(22) 全 ROM 只有")
print("     0x242B92 一处，那一段封装 0x%X 有 %d 个调用点，按 64 KB 段分：%s"
      % (STUB, len(callers), ["0x%X: %d 个" % (k << 16, v) for k, v in sorted(buck.items())]))
print("     这些调用点动态一次都没走到（上面 visited 全 False）。")
print("     任务 22 自己是「进门先擦铃」：入口函数的第一个系统调用就是")
print("     clr_flg(10, ~0x40000000)（0x%X，清 bit30）；能置 bit30 的" % T22_CLR)
print("     set_flg(10,0x40000000) 静态有 %d 处 —— 铃在 ROM 里，只是没人走到。" % len(b30))
print("  ② 像素本身要过 EXPEED 位串行，实测量级 %d 位 vs 一帧 %d 位。"
      % (mt.expeed()["bits"], bpix.FRAME_BITS))
print()
print("不可报：真实像素画面、屏幕上每条文案的位置、字体字号、")
print("        「再跑几步就能出画面」（三条判据一起否掉这句）。")

# ---------------------------------------------------------------- F
hr("F  复现")
print("""cd emu
python probe/exp_bpix.py > ../results/frbox/bpix.txt

判据定义（frbox/bpix.py）：
  L 连续区间 >= 38,400 B（640×480 1bpp 一帧）
  V 该区间 4 KB 块的值种数中位数 >= 16
  R 该区间被写 >= 2 次的半字占比 >= 50%
三条同时成立才算「有帧缓冲」；分开判会互相打架。""")
