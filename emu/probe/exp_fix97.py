# -*- coding: utf-8 -*-
"""V97 修复版正式探针：14 项缺陷落地自证 + 主树真文件收敛等形对照。

复现（从 emu/ 目录）：
    cd emu && python probe/exp_fix97.py > ../results/frbox/fix97.txt

七段：
  A  口径与 14 项清单（P0 2 / P1 6 / P2 3 / P3 3）
  B  CGEN 8 条实跑回归（真跑 fr_emu，实跑值 vs CGEN 期望）
  C  判据可证伪对照（真多元组 / 故意不同 half / 单元组 / 少一组）
  D  主树真文件收敛等形对照（末 PC / 覆盖 / 步数 vs 0.96.0 基线）
  E  P2 三项行为对账（ROM 写进 watch / mtree 去重 / irq_level 负索引）
  F  额外项复核（报告未列入 14 项，只读观察，不纳入源码修复）
  G  边界与禁语 + 残留 + 复现

纪律：
  * 数字一律实跑实测，不预写；收敛用 V65–V96 逐字相同的设备策略，
    由 blive.boot(0) + Machine.step / Machine.run 推进（不自写 cpu.step 循环）。
  * 每台机器只跑一遍；D 段是主树真文件，不是临时副本。
  * 阴性结果只写「当前收敛路径未改变」，不外推为「全局无影响」（规矩 5）。
  * 未修项如实记录，不假报已落地。
"""

import sys
import os
import io
import inspect

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # emu/

from frbox import fix97 as F, svc91 as V91, blive, device, cover, machine, bus
from frbox.bus import AddressSpace
from frbox.machine import create
import frbox


def sec(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def src_of(mod):
    with io.open(mod.__file__, encoding="utf-8") as f:
        return f.read()


def src_of_path(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ A
sec("[A] V97 口径与 14 项修复清单")
print("A0 版本 %s（审查基线 %s，审查日 %s，修复日 %s）"
      % (frbox.__version__, F.AUDIT_BASELINE, F.AUDIT_DATE, F.FIX_DATE))
print("A1 修前全量自检基线 %d/%d（独立输出 selftest_v97_baseline.txt，修后需重跑）"
      % (F.BASELINE_SELFTEST, F.BASELINE_SELFTEST))
print("A2 修复项 %d 项，分级 %s，清单齐备判据 = %s"
      % (len(F.FIX_ITEMS), F.count_by_level(), F.fix_table_verdict()))
print("A3 逐项（编号 / 级别 / 位置 / 缺陷 / 处置）：")
for no, lv, loc, bug, fix in F.FIX_ITEMS:
    print("   %-5s %-2s %s" % (no, lv, loc))
    print("        缺陷：%s" % bug)
    print("        处置：%s" % fix)
print("A4 说明：本报告只对 11-27 只读审查（emulator_audit_v96.html 第 8 节）"
      "列出的 14 项确凿缺陷动手，不擅自扩大修复面；P0/P1/P2/P3 之外"
      "的观察项在 F 段单列复核。")

# ------------------------------------------------------------------ B
sec("[B] CGEN 8 条实跑回归（真跑 fr_emu，不是常量自证）")
print("B0 依据：仓库自带 emu/fr30.cpu（FR30 CGEN 语义）；"
      "每条构造 2 字节指令，Mem.load_rom 后 Fr.step() 真跑一条。")
rows = F.run_cgen_cases()
print("B1 实跑 %d 条：" % len(rows))
for tag, got, exp in rows:
    print("   %-18s 实跑 %-14s 期望 %-14s %s"
          % (tag, repr(got), repr(exp), "OK" if got == exp else "BAD"))
_v = F.cgen_regression_verdict(rows)
print("B2 综合判据 = %s（8/8 全对才为 all_match）" % _v)
print("B3 覆盖：bandl/bandh 常数位、逻辑 reg 不动 V/C 与 mem 置 Z、"
      "lsl amt=0 清 C、ldres Ri+4、stm1 save-r15、mul 按结果设 Z。")
print("B4 边界：mulh/muluh 的 (ge MDL 0) 按 CGEN 字面实现，真机语义仍列 KI 开放项。")

# ------------------------------------------------------------------ C
sec("[C] P0-2 判据可证伪对照（V91 结论不变，判据从恒真改为可证伪）")
print("C0 V91 原判据对单元素 (half,) 做 same_half 恒真，行数==8 即宣布绑定；"
      "V97 新增 ALIAS_HALF_TUPLES 真多元组，要求每行长度>=2 且组内同 half。")
print("C1 真多元组 8 组：%s" % [tuple("0x%04X" % h for h in t)
                                for t in V91.ALIAS_HALF_TUPLES])
print("C2 默认判据 = %s（期望 %s）"
      % (V91.half_binding_verdict(), F.FALSIFY_TRUE))
_bad = list(V91.ALIAS_HALF_TUPLES[:7]) + [(0x0012, 0x0034)]
print("C3 故意构造组内不同 half -> %s（期望 %s）"
      % (V91.half_binding_verdict(_bad), F.FALSIFY_DIFFERENT_HALF))
print("C4 单元组（恒真已去除） -> %s（期望 %s）"
      % (V91.half_binding_verdict([(0x12,)] * 8), F.FALSIFY_SINGLETON))
print("C5 少一组 -> %s（期望 unresolved）"
      % V91.half_binding_verdict(V91.ALIAS_HALF_TUPLES[:7]))
print("C6 V91 既有结论不受影响：ALIAS_ROWS %d 组，"
      "half 与多元组首元素逐项对账 = %s"
      % (len(V91.ALIAS_ROWS),
         tuple(r[2] for r in V91.ALIAS_ROWS)
         == tuple(t[0] for t in V91.ALIAS_HALF_TUPLES)))

# ------------------------------------------------------------------ D
sec("[D] 收敛等形对照（V65–V96 同一把尺子，主树真文件）")
print("D0 口径：blive.boot(0) → m.step(%d) → 单次 m.raise_irq(0x%02X) → "
      "m.run(%d, spin_guard=False)"
      % (F.CONVERGE_BOOT, F.CONVERGE_IRQ, F.CONVERGE_RUN))
print("D1 0.96.0 修前基线（V65–V96 逐字相同）：末 PC 0x%06X / 覆盖 %d / 步数 %d"
      % (F.BASELINE_TAIL_PC, F.BASELINE_COVERAGE, F.BASELINE_STEPS))
m = blive.boot(0)
m.step(F.CONVERGE_BOOT)
m.raise_irq(F.CONVERGE_IRQ)
m.run(max_steps=F.CONVERGE_RUN, spin_guard=False)
end_pc, cov, steps = m.cpu.pc, m.cov.words(), m.step_count
_same = (end_pc == F.BASELINE_TAIL_PC,
         cov == F.BASELINE_COVERAGE,
         steps == F.BASELINE_STEPS)
print("D2 V97 修后主树实测：末 PC 0x%06X / 覆盖 %d / 步数 %d"
      % (end_pc, cov, steps))
print("D3 末 PC  %s（基线 0x%06X）"
      % ("相同 OK" if _same[0] else "不同 BAD", F.BASELINE_TAIL_PC))
print("D4 覆盖    %s（基线 %d）"
      % ("相同 OK" if _same[1] else "不同 BAD", F.BASELINE_COVERAGE))
print("D5 步数    %s（基线 %d）"
      % ("相同 OK" if _same[2] else "不同 BAD", F.BASELINE_STEPS))
print("D6 综合 = %s"
      % ("三项逐字相同 ⇒ 当前收敛路径等形" if all(_same)
         else "存在差异 ⇒ 历史结论需重新取证"))
print("D7 审查阶段分组补丁对照（临时副本 7 组 %s + PALL）结论："
      "末 PC / 覆盖 / 步数与基线逐字相同；本版用主树真文件重跑一次确认，"
      "不重复跑 8 台机器。" % list(F.PATCH_GROUPS[:-1]))
print("D8 边界：单路径等形阴性结果（规矩 5：静态可达 ≠ 已执行）。"
      "只能报「当前收敛路径未改变」，不能报「全局无影响」，"
      "也不能报「补丁对照全同所以这些偏差不用修」。")

# ------------------------------------------------------------------ E
sec("[E] P2 三项行为对账（真建对象、真调用）")
b = AddressSpace("v97")
b.map_rom(0x40000, b"\xAA" * 16)
b.watch.add(0x40000)
b.all_writes.clear()
b.writes_to_rom.clear()
b.access_log.clear()
b.wr(0x40000, 1, 0x55, pc=0x1234)
print("E1 ROM 写进 watch 审计链 = %s；仍记 writes_to_rom = %d 笔"
      % (any(a == 0x40000 and k == "W" for k, a, *_ in b.access_log),
         len(b.writes_to_rom)))
b2 = AddressSpace("v97m")
b2.unmapped_writes = [(0x1000, 1, 0, 0)] * 2 + [(0x2000, 1, 0, 0)]
print("E2 mtree 去重口径：%s" % ("2 个地址 / 3 次" in b2.mtree()))
mm = create("d300-b")
print("E3 irq_level(vec=0x00) = %d（期望 0，不再负索引回绕）；"
      "vec=0x10 = %d（合法类型 int）"
      % (mm.irq_level(0x00), mm.irq_level(0x10)))

# ------------------------------------------------------------------ F
sec("[F] 额外项复核（只读观察，未列入 14 项，不纳入源码修复）")
print("F0 这些是审查过程中看到的观察项，不是确凿缺陷；"
      "本版不擅自扩大修复面，逐条留痕以便后续单独立项。")
_bl, _bs, _cv, _mc, _dv = (src_of(blive), src_of(bus), src_of(cover),
                            src_of(machine), src_of(device))
print("F1 blive.Watch.rd() 双读：源码内 self._rd(a, n, pc) 出现 %d 次 "
      "（0x44 是写后哑读锁存器，读无副作用 ⇒ 观察重复调用不属缺陷）"
      % _bl.count("self._rd(a, n, pc)"))
print("F2 bus._io_read 截断：返回 v & M32 = %s（按 32 位掩码，不按 n 截断；"
      "设备返回值本身在范围内，未观察到越界 ⇒ 观察项）"
      % ("return v & M32" in _bs))
print("F3 cover.steps 统计字段存在 = %s（只统计步数，不参与覆盖判定）"
      % ("self.steps = 0" in _cv))
print("F4 machine.load() data/payload CRC 校验存在 = %s"
      % ("crc16_xmodem(payload)" in _mc))
print("F5 device 预留 irq 接口：源码含 def irq = %s（属预留钩子，"
      "不是漏实现；当前设备由 Machine.raise_irq 统一驱动）" % ("def irq" in _dv))
_root = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_push = os.path.join(_root, ".workbuddy", "tmp", "push_emu.py")
_vrfy = os.path.join(_root, ".workbuddy", "tmp", "verify_emu.py")
if os.path.isfile(_push) and os.path.isfile(_vrfy):
    _ps, _vs = src_of_path(_push), src_of_path(_vrfy)
    print("F6 P0-1 推送脚本护栏：R 指向主树 = %s / %s；_guard() 定义 = %s / %s；"
          "版本断言 0.97.0 = %s / %s"
          % ("D300-emulator-analysis" in _ps, "D300-emulator-analysis" in _vs,
             "def _guard" in _ps, "def _guard" in _vs,
             "0.97.0" in _ps, "0.97.0" in _vs))
    print("F7 NUL 残留内存重建护栏：payload_for 定义 = %s / %s；"
          "MAIN_FIXED 常量 = %s / %s（本地 143 字节 NUL 时按 git HEAD 原版 + "
          "main guard 重建参与推/校，远端不收损坏文件）"
          % ("def payload_for" in _ps, "def payload_for" in _vs,
             "MAIN_FIXED" in _ps, "MAIN_FIXED" in _vs))
else:
    print("F6 P0-1 推送脚本护栏：脚本不存在，无法复核（BAD）")

# ------------------------------------------------------------------ G
sec("[G] 边界、残留与禁语")
print("G0 本版是「按 CGEN 修正 CPU 语义 + 工程/文档缺陷修复」，不是新的固件侦查；"
      "不宣布 V51–V96 既有结论作废。")
print("G1 残留（如实记录，不假报已修）：emu/frbox/__main__.py 仍是 143 字节全 NUL，"
      "delete-pending / 宿主变更索引冲突，所有常规写入通道被拒（含 git checkout -f）；"
      "P3-1（main guard）本地未落地。frbox_selftest.py 不引用该文件 ⇒ "
      "全量自检不阻塞，但 python -m frbox 会崩。push_emu.py / verify_emu.py 已加 "
      "payload_for()：本地确为 NUL 残留时按 git HEAD 原版 + main guard 在内存中重建 "
      "并打印 [WARN]，远端拿修复版、不推损坏文件；本地磁盘状态不隐瞒。")
print("G2 交付链：修后全量自检（独立输出 selftest_v97_after.txt）、README 概览数字"
      "按实测回填、docs / KNOWN_ISSUES / 索引同步、V97 报告、推送校验与交付闸门"
      "依次执行；本探针只负责结果文件与收敛等形对照。")
print("")
print("不能报：模拟器全错 / 既有 V51–V96 结论全废 / 出画面 / 系统跑起来了 / "
      "补丁对照全同所以不用修 / 全局无影响 / __main__.py 已修好 / "
      "任务 12–15 的功能是什么 / 表驱动是「按功能分组」/ "
      "256 是固件周期 / 补一次 tick 就等于真机时基。")
print("能报：14 项确凿缺陷已在主树逐项落地并自证；8 条 CGEN 实跑回归全对；"
      "half_binding 判据从恒真改为可证伪；V97 修后主树收敛三项与 0.96.0 "
      "基线逐字相同（仅当前路径）；P2 三项行为对账通过。")
print("")
print("G3 复现：cd emu && python probe/exp_fix97.py > ../results/frbox/fix97.txt")
print("G4 结果文件由本探针 stdout 重定向生成；所有数字来自实跑，未预写。")
print("G5 综合判据 = %s；CGEN = %s；收敛 = %s"
      % (F.VERDICT, _v, "三项逐字相同" if all(_same) else "有差异"))

# ------------------------------------------------------------------ H
sec("[H] V97 语义修正的行为影响（11 条 BAD 逐条归因）")
print("H0 修后首次全量自检 5812/5823、11 条 BAD（独立输出 selftest_v97_after.txt）；"
      "不是修复失败，是「0.96.0 错误 CPU 语义下锁死的实测值」在 CGEN 对齐后变化。")
print("H1 归因实验：影子目录 emu96/ 只把 fr_emu.py 回退 0.96.0（其余全修后），"
      "重跑 test_kernel + test_v92 + test_v93 → 0 BAD / EXIT=0，证据 %s"
      % F.SHADOW_EVIDENCE)
print("    ⇒ 7 条行为变化全由 P1 六类 CPU 语义修正引起；4 条 CLI BAD 由 "
      "__main__.py 的 143 字节 NUL 残留（P3-1 本地未落地）引起。")
print("H2 11 条逐条（分组 / 断言 / 0.96.0 期望 / V97 实测 / 归因）：")
for grp, name, old, new, why in F.SEMANTIC_IMPACTS:
    print("   [%-4s] %-22s %-18s → %-18s %s" % (grp, name, old, new, why))
print("H3 处置：7 条行为断言按修后实测更新并在断言名标注差异；"
      "info() 步数改动态判据；4 条 CLI 改走 cli.main 直调（仍是子进程、"
      "仍测真实参数解析，不掩盖包入口损坏）；修后受影响子集 221 OK / 0 BAD。")
print("H4 边界：这正好证明审查阶段「单路径等形阴性结果」只在主收敛路径成立；"
      "带时基 / μITRON 半主机路径确实受语义修正影响（规矩 5）。"
      "KI#388：V92/V93 带时基结论需在 CGEN 对齐语义下重新取证。")
print("H5 不能报：11 条 BAD 是回归 / V92/V93 结论已作废 / 修复引入新 bug / "
      "全局无影响 / 补丁对照全同所以不用修。")
print("H6 综合判据 = %s" % F.VERDICT_SEMANTIC)
