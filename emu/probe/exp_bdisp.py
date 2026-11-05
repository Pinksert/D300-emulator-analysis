# -*- coding: utf-8 -*-
"""V26 探针：能不能出画面？—— 显示链路审计 + 结构重建图。

结论先说：**出不了真实画面**。这一版把「为什么」量出来，并把固件
「屏幕上应该有什么」按结构重建出来（`results/frbox/ui-rebuild.png`）。

三条证据：
  1. 固件里没有帧缓冲 —— SDRAM 里的写入没有一块按 640×480 组织；
  2. 固件里没有字库 —— 界面文字全在 ROM 文本池（0x3A7FC0 起），
     变成点阵是 EXPEED 的事；
  3. 「绘制」= 改状态 + 发标志：描述符表 0x1BC020（70 条）里的绘制函数
     没有一条写像素，0x12E752 甚至是裸 `ret`；真正的像素走
     B↔EXPEED 位串行（0x70000002 / 0x7000000A），整场运行只被碰 2 次。

A 描述符表 / B 绘制函数解剖 / C 像素通道审计 / D 文案池 / E 重建图 / F 结论
"""
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import brealos, bgate, bdisp


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------- A 描述符
head("A 显示描述符表：固件自己维护的「屏幕状态」")

m = create("d300-b")
m.load()
s = bdisp.summary(m)
print("表 @0x%08X，%d 条 × 16 B（文本缓冲 / 长度 / 样式 / 绘制函数）"
      % (bdisp.DESC_TABLE, s["count"]))
print("  有文本的条目 %d；样式 0(普通)/1(高亮)/2(空行) = %d / %d / %d"
      % (s["nonempty"], s["type0"], s["type1"], s["type2"]))
print("  文本总字节 %d；绘制函数去重 %d 个，其中空实现（裸 ret）%d 个"
      % (s["text_bytes"], s["unique_fn"], s["noop_fn"]))
print("\n前 12 条：")
print("   #   文本缓冲    长度  样式  绘制函数")
for r in bdisp.descriptors(m)[:12]:
    print("  %2d  %08X  %4d  %d     %08X%s"
          % (r["idx"], r["data"], r["len"], r["type"], r["fn"],
             "  ← 空实现" if bdisp.fn_is_noop(m, r["fn"]) else ""))

# ---------------------------------------------------------------- B 绘制
head("B 绘制函数实际做了什么：改状态、发标志，不写像素")

for addr, note in ((0x12E752, "空实现（几乎所有条目都用它）"),
                   (0x12E7D6, "读一个字节 → 写 / 清标志 → set_flg(3,0x400)"),
                   (0x12E806, "改 0x9F58D4BE 的状态字节")):
    print("\n0x%08X —— %s" % (addr, note))
    for ln in m.disasm_at(addr, 10).splitlines():
        print("   " + ln)
print("\n这些函数里没有任何一条指令写像素缓冲：它们改的是 0x9F58D4xx/D8xx")
print("这类「界面状态字节」，然后用 set_flg 通知显示任务。")

# ---------------------------------------------------------------- C 像素通道
head("C 像素通道审计：EXPEED 总线碰了几次、SDRAM 有没有帧缓冲")

# 钩子要装在 boot 之前：整场运行（boot + 装配 + 开门 + 注入）一起统计
mm = create("d300-b")
_low = 0x9F580000
sid = [False] * 0x20000
bus_w, bus_r = Counter(), Counter()
_ow, _or = mm.bus.wr, mm.bus.rd


def _hook_w(a, sz, v, pc=None):
    if 0x70000000 <= a < 0x70100000:
        bus_w[a] += 1
    if _low <= a < _low + 0x20000:
        sid[(a - _low) >> 1] = True
    return _ow(a, sz, v, pc)


def _hook_r(a, sz, pc=None):
    if 0x70000000 <= a < 0x70100000:
        bus_r[a] += 1
    return _or(a, sz, pc)


mm.bus.wr, mm.bus.rd = _hook_w, _hook_r
mm.load()
mm.reset()
mm.enable_cov()
mm.attach_flow()
n = 0
while not mm.cpu.halted and n < 191520:
    mm.step(1)
    n += 1
brealos.sta_tsk(mm, 2)
mm.raise_irq(0x3F)
mm.run(max_steps=200000, spin_guard=False)
mm.run(max_steps=800000, spin_guard=False)
bgate.open_gate(mm)
mm.run(max_steps=1000000, spin_guard=False)


for i in range(4):
    mm.raise_irq(0x3F)
    mm.run(max_steps=50000, spin_guard=False)
mm.bus.wr, mm.bus.rd = _ow, _or
print("EXPEED 窗口 0x7000xxxx：写 %s" % {hex(k): v for k, v in bus_w.most_common(6)})
print("                      读 %s" % {hex(k): v for k, v in bus_r.most_common(6)})
print("（B↔EXPEED 是位串行：CS=0x70000002 bit2、CLK/DATA=0x7000000A bit1/bit2）")

# SDRAM 写入跨度：找最长连续写段（整场统计，见上面的钩子）
best = cur = 0
for f in sid:
    cur = cur + 1 if f else 0
    best = max(best, cur)
print("SDRAM 0x9F580000 + 128KB 内：写了 %d 个半字地址，最长连续段 %d 个半字（%d 字节）"
      % (sum(sid), best, best * 2))
print("640×480 单色就要 38,400 字节连续；实测最长连续段远小于它 → 没有帧缓冲。")

# ---------------------------------------------------------------- D 文案池
head("D 界面文案池：文字都在 ROM 里（0x3A7FC0 起）")

for menu in ("PLAYBACK", "SHOOTING", "SETUP"):
    items = bdisp.menu_items(m, menu, 6)
    print("  %-9s MENU：%s" % (menu, items))
print()
print("分段方式：大写字母前若跟着小写/右括号/数字就切一刀 —— 固件把菜单项首尾相接")
print("存放（无分隔符），这个启发式对前三段的前几项都对得上。切错时表现为一个")
print("词条里出现两个大写开头，肉眼可辨（例如 SHOOTING 第 1 条会把两行并在一起）。")

# ---------------------------------------------------------------- E 重建图
head("E 重建图：把「屏幕上应该有什么」画出来（像素是我们补的）")

out = os.path.join(os.path.dirname(_EMU), "results", "frbox", "ui-rebuild.png")
print("输出：", out)
bdisp.render(out, m, facts={"expeed_w": bus_w.get(0x7000000A, 0),
                                      "expeed_r": bus_r.get(0x7000000A, 0),
                                      "sdram_run": best * 2})
print("图里左侧是模拟屏（标题 + 前 5 个菜单项 + 高亮 + 滚动条），")
print("右侧是三条证据与结论。文案来自固件，像素/字体/配色是重建侧补的。")

# ---------------------------------------------------------------- F 结论
head("F 能报 / 不能报")
print("能报：")
print("  * 描述符表 70 条：65 条有文本、样式 0/1/2 = 41/24/5、绘制函数 44 个")
print("  * 绘制函数不写像素：只改界面状态字节 + set_flg；0x12E752 是裸 ret")
print("  * 像素通道是 B↔EXPEED 位串行，整场运行只写了 2 次")
print("  * SDRAM 里没有 640×480 级别的连续帧缓冲")
print("  * 界面文案（菜单项、帮助）确实在固件 ROM 里，可以按结构重建")
print("不能报：")
print("  * 真实像素画面（要 EXPEED 模型 + 驱动显示的外设事件）")
print("  * 屏幕上每个位置显示哪条文案（那是运行期 UI 状态，事件没来就填不上）")
print("  * 字体/字号（固件没有字库，字形在显示控制器里）")
print()
print("⚠️ 口径：重建图的像素不是固件渲染的；它只证明「固件认为屏幕上有这些条目」，")
print("   不证明「屏幕上长这样」。")
