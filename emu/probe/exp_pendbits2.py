# -*- coding: utf-8 -*-
"""V23 探针（A 段）：位分派器 61 位全部「干净机单点」定性。

V22 发现位与位之间有状态耦合：顺序扫描（一台机器逐位点亮）与干净机
单点（每位置一台新机器）的数字对不上。V22 只对 2 个位做了干净机单点
（0x3F402&0x40 → +369、0x3F404&0x80 → +299），其余 59 个位的单点
数字是空白 —— 本探针把这块补齐。

口径：与 exp_pendbits 完全相同的 base()（boot REALOS + 回调槽 +
INT 0x2B + 12×5000 预热，基线 1,443）。每个位：
  新机器 → 只点亮这一位 → 一次 INT 0x2B → 2000 步 → 记
  覆盖增量 / call 点执行 / 目标函数执行 / 服务调用 / 结束 PC。

分类（干净机判定）：
  A 类：目标函数执行（处理函数真的跑了）
  B 类：call 点执行但目标没跑（只走了分派器骨架 / 需要前置状态）
  C 类：两者都没执行（位亮了但分派器没走这一支 —— 需要别的前置）

对比 V22 顺序扫描：顺序扫描里「无效果」的位里有多少其实单点有效果
（= 顺序扫描时被前面位留下的状态压住的）。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
sys.path.insert(0, _EMU)
sys.stdout.reconfigure(encoding="utf-8")

from frbox.isr2b import table as dispatch_table
from exp_pendbits import base


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def main():
    head("A 61 位干净机单点：一位一台新机器")
    # dispatch_table 需要 machine：先起一台拿表
    m0, _ = base()
    t = dispatch_table(m0)
    print("位分派表 %d 条（基线口径 1,443）。逐位干净机单点：" % len(t))
    print()
    rows = []
    for byte, mask, btst, call, tgt in t:
        m, h = base()
        m.bus.wr(byte, 1, mask, m.cpu.pc)
        b = m.cov.words()
        s0 = len(m.flow.calls)
        h.raise_once()
        m.run(max_steps=2000, spin_guard=False)
        dc = m.cov.words() - b
        ds = len(m.flow.calls) - s0
        ran_call = m.cov.visited(call)
        ran_tgt = m.cov.visited(tgt)
        pc = m.cpu.pc
        rows.append((byte, mask, tgt, dc, ds, ran_call, ran_tgt, pc))
        print("  [0x%08X]&0x%02X → 0x%05X：+%-4d 调用+%-2d call点%s 目标%s 结束 0x%05X"
              % (byte, mask, tgt, dc, ds,
                 "√" if ran_call else "×", "√" if ran_tgt else "×", pc))

    head("B 分类统计")
    a = [r for r in rows if r[6]]
    b = [r for r in rows if r[5] and not r[6]]
    c = [r for r in rows if not r[5]]
    print("A 类（目标函数执行）：%d 位" % len(a))
    print("B 类（只走骨架，目标未执行）：%d 位" % len(b))
    print("C 类（call 点都没执行）：%d 位" % len(c))
    print()
    print("A 类按覆盖增量排序（干净机的确定性数字）：")
    for r in sorted(a, key=lambda r: -r[3]):
        print("  [0x%08X]&0x%02X → 0x%05X：+%-4d 调用+%d"
              % (r[0], r[1], r[2], r[3], r[4]))
    print()
    print("B/C 类清单（需要前置状态 / 顺序扫描压不住的单点无效位）：")
    for r in sorted(b + c, key=lambda r: -r[3]):
        cls = "B" if r[5] else "C"
        print("  %s [0x%08X]&0x%02X → 0x%05X：+%-4d"
              % (cls, r[0], r[1], r[2], r[3]))

    head("C 口径提醒")
    print("· 本表是「干净机单点」口径 —— 与 exp_pendbits B 段的顺序扫描表")
    print("  不可混比（V22 已证位间有状态耦合）。")
    print("· 2000 步的覆盖增量是该位「无前置状态」下的下限：处理函数")
    print("  走到需要更多事件的地方会停住。")


if __name__ == "__main__":
    main()
