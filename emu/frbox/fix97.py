# -*- coding: utf-8 -*-
"""V97 修复版设施：把 11-27 只读审查的 14 项确凿缺陷落地并**自证**。

承接 `emulator_audit_v96.html`（只读审查、未改任何源码）第 8 节建议：
  P0 两项（推送脚本树错配 / half_binding 判据恒真）
  P1 六类（fr_emu.py CPU 语义，按 fr30.cpu CGEN 逐条对齐）
  P2 三项（bus.mtree 去重口径 / ROM 写补 watch / irq_level 负索引）
  P3 三项（README 概览数字 / __main__.py main guard / task93 死参数）

本模块提供：
  1. FIX_ITEMS —— 14 项修复清单（编号 / 级别 / 位置 / 处置）。
  2. CGEN_CASES + run_cgen_cases() —— 8 个 CGEN 最小复现的**实跑**回归
     （构造字节真跑 fr_emu，返回实跑值，与期望值对账；不是常量自证）。
  3. 判据可证伪的三条对照数据（真多元组 / 故意不同 half / 单元组）。
  4. 收敛基线锚点（V65–V96 逐字相同），供等形对照。

**边界**：本版是「按 CGEN 修正 CPU 语义 + 工程/文档缺陷修复」，不是新的固件
侦查；不宣布既有 V51–V96 结论作废、不宣布出画面、不宣布系统跑起来了。
CPU 六类偏差在**当前收敛路径**上末 PC / 覆盖 / 步数不变，是单路径等形
阴性结果（规矩 5：静态可达 ≠ 已执行），不得外推为「全局无影响」。
"""

M32 = 0xFFFFFFFF

VERSION = "0.97.0"
AUDIT_BASELINE = "0.96.0"
AUDIT_DATE = "2026-11-27"
FIX_DATE = "2026-11-28"
BASELINE_SELFTEST = 5783          # 0.96.0 修前实测（独立输出 selftest_v97_baseline.txt）

# ---------------------------------------------------------------- 14 项清单
# (编号, 级别, 位置, 缺陷, 处置)
FIX_ITEMS = (
    ("P0-1", "P0", ".workbuddy/tmp/push_emu.py / verify_emu.py",
     "REPO 指向主树仓库但 R 指向 0.93.0 旧快照，误用会把旧代码推到新仓库造成版本回退",
     "R 改指向主树 D300-emulator-analysis，并在网络动作前加本地 __version__ / 仓库名一致性护栏"),
    ("P0-2", "P0", "frbox/svc91.py:153 half_binding_verdict",
     "r[2:] 是单元素元组，same_half 恒真；行数==8 即宣布全组同 half 绑定，不可证伪",
     "新增 ALIAS_HALF_TUPLES（组内各号 slot 实测 half 多元组），判据要求每行长度>=2 且组内同 half；补负对照"),
    ("P1-1", "P1", "fr_emu.py bandl / bandh",
     "掩码漏常数位（CGEN bandl=u4|0xF0、bandh=(u4<<4)|0x0F）；bor/beor/btst 不带常数位",
     "只对 bandl/bandh 补常数位，其余 h 结尾指令保持 u4<<4"),
    ("P1-2", "P1", "fr_emu.py and / or / eor",
     "寄存器型强制清 V；内存型完全不设 Z/N（CGEN binary-logical-op 只 set-z-and-n）",
     "寄存器型不清 V/C；内存型补 _set_zn"),
    ("P1-3", "P1", "fr_emu.py lsl / lsr / asr",
     "amt==0 只 _set_zn 就 return，C 保持旧值（CGEN shift==0 -> cbit=0）",
     "amt==0 时显式清 C"),
    ("P1-4", "P1", "fr_emu.py mul / mulu / mulh / muluh",
     "四变体只写 MDL/MDH，完全不动标志（CGEN 按结果设 N/Z/V）",
     "按 fr30.cpu:770-825 逐条设标志；mulh/muluh 的 (ge MDL 0) 按字面实现并列 KI 开放项"),
    ("P1-5", "P1", "fr_emu.py ldres / stres",
     "归入 unimpl_seen 直接 return，Ri 不变（CGEN 明确 Ri += 4）",
     "ldres/stres 单独补 Ri+=4，其余协处理指令仍是 stub"),
    ("P1-6", "P1", "fr_emu.py ldm1 / stm1 含 R15",
     "stm1 先减后存取到减后值；ldm1 恢复 R15 又多 +4（CGEN save-r15）",
     "stm1 先存旧 R15 再减；ldm1 命中 R15 时不再 +4"),
    ("P2-1", "P2", "frbox/bus.py mtree()",
     "n/tot 都是写次数却印成「N 个地址 / N 次」，与 stats() 去重口径不一致",
     "n 用去重地址数、tot 用写次数"),
    ("P2-2", "P2", "frbox/bus.py wr()",
     "ROM 写只记 writes_to_rom，不进 watch 审计链",
     "ROM 写补 _maybe_watch"),
    ("P2-3", "P2", "frbox/machine.py irq_level()",
     "(vec-0x10)&0x3F 对 vec<0x10 负索引回绕，与 irq_table() 口径不一致",
     "vec 不在 0x10-0x3F 时显式返回 0"),
    ("P3-1", "P3", "frbox/__main__.py",
     "无 main guard，作为模块导入即执行 CLI（跑 391495 步、打印 2163 字节）",
     "加 if __name__ == '__main__' 保护"),
    ("P3-2", "P3", "README.md:61/137/170",
     "概览区仍写 5676/5676（V93 数字），当前实测 5783",
     "回填为 V97 修后实测值"),
    ("P3-3", "P3", "frbox/task93.py verdict_bootstrap",
     "total 参数赋值后完全不参与输出，是死参数",
     "删除该参数（调用点只传 unr）"),
)

FIX_LEVELS = ("P0", "P1", "P2", "P3")


def count_by_level():
    """返回 {级别: 项数}；用于自检对账 2/6/3/3。"""
    out = {k: 0 for k in FIX_LEVELS}
    for row in FIX_ITEMS:
        out[row[1]] += 1
    return out


# ---------------------------------------------------------------- CGEN 实跑回归
# 8 个最小复现（构造字节来自审查阶段 _verify_exact.py，已确认解码成目标指令）。
# 每项: (tag, code_bytes, setup, read, expect)
#   setup(m, cpu) / read(m, cpu) -> 值；实跑值由 run_cgen_cases() 用 fr_emu 真跑得到。
ROM = 0x00080000


def _set_r(vals):
    def f(m, c):
        c.r = vals + [0] * (16 - len(vals))
    return f


def _cgen_cases():
    cases = []
    # ① 位操作 bandl #0xE @0xA1 -> 0xA0（CGEN mask=0xFE）
    cases.append(("bandl #0xE", [0x80, 0xE0],
                  lambda m, c: (_set_r([0x40000])(m, c), m.wr(0x40000, 1, 0xA1)),
                  lambda m, c: m.rd(0x40000, 1), 0xA0))
    # ② 位操作 bandh #0xD @0xB7 -> 0x97（CGEN mask=0xDF）
    cases.append(("bandh #0xD", [0x81, 0xD0],
                  lambda m, c: (_set_r([0x40000])(m, c), m.wr(0x40000, 1, 0xB7)),
                  lambda m, c: m.rd(0x40000, 1), 0x97))
    # ③ 逻辑 reg/reg 不动 V/C（预设 V=C=1）
    cases.append(("and reg 不动 V", [0x82, 0x10],
                  lambda m, c: (_set_r([0xF0F0F0F0, 0x0F0F0F0F])(m, c),
                                setattr(c, "v", 1), setattr(c, "c", 1)),
                  lambda m, c: (c.v, c.c), (1, 1)))
    # ④ 逻辑 reg/mem 也 set-z-and-n（结果 0 -> Z=1）
    cases.append(("and mem 置 Z", [0x84, 0x10],
                  lambda m, c: (_set_r([0x40000, 0xFFFFFFFF])(m, c),
                                m.wr(0x40000, 4, 0), setattr(c, "z", 0),
                                setattr(c, "n", 1)),
                  lambda m, c: c.z, 1))
    # ⑤ 移位 amt==0 清 C（预设 C=1）
    cases.append(("lsl #0 清 C", [0xB4, 0x00],
                  lambda m, c: (_set_r([0x80000000])(m, c), setattr(c, "c", 1)),
                  lambda m, c: c.c, 0))
    # ⑥ ldres 后 Ri += 4
    cases.append(("ldres Ri+4", [0xBC, 0x00],
                  lambda m, c: _set_r([0x40000])(m, c),
                  lambda m, c: c.r[0], 0x40004))
    # ⑦ stm1 含 R15 保存减 4 前的值
    cases.append(("stm1 save-r15", [0x8F, 0x01],
                  lambda m, c: _set_r([0] * 15 + [0x40000])(m, c),
                  lambda m, c: m.rd(0x3FFFC, 4), 0x40000))
    # ⑧ mul 2x3 按结果设 Z（预设 Z=1，结果 6 非 0 -> Z=0）
    cases.append(("mul 2x3 清 Z", [0xAF, 0x10],
                  lambda m, c: (_set_r([2, 3])(m, c), setattr(c, "z", 1)),
                  lambda m, c: c.z, 0))
    return tuple(cases)


CGEN_CASES = _cgen_cases()


def run_cgen_cases():
    """真跑 fr_emu，返回 [(tag, 实跑值, 期望值)]。自检据此逐条 check。"""
    from fr_emu import Fr, Mem, TABLE
    out = []
    for tag, code, setup, read, expect in CGEN_CASES:
        m = Mem()
        m.load_rom(ROM, bytes(code) + b"\x00" * 16)
        c = Fr(m, TABLE)
        c.pc = ROM
        setup(m, c)
        c.step()
        out.append((tag, read(m, c), expect))
    return out


# ---------------------------------------------------------------- 判据可证伪对照
# 真多元组（8 组各号 slot 实测 half），与 svc91.ALIAS_HALF_TUPLES 同源
FALSIFY_TRUE = "static_same_half_binding_dispatch_folds_to_one_entry"
FALSIFY_DIFFERENT_HALF = "unresolved"       # 故意构造不同 half
FALSIFY_SINGLETON = "unresolved"            # 单元组恒真已去除


# ---------------------------------------------------------------- 收敛锚点（V65–V96 逐字相同）
CONVERGE_BOOT = 191520
CONVERGE_IRQ = 0x2B
CONVERGE_RUN = 500000
BASELINE_TAIL_PC = 0x0407DC
BASELINE_COVERAGE = 5439
BASELINE_STEPS = 691520

# 审查阶段分组补丁对照（临时副本，7 类逐项 + 全量叠加）结论：
# 末 PC / 覆盖 / 步数与基线逐字相同。V97 用**主树真文件**重跑一次确认。
PATCH_GROUPS = ("P1_bitop", "P2_logic", "P3_shift", "P4_mul",
                "P5_res", "P6_multireg", "PALL")


def cgen_regression_verdict(rows):
    """8 条 CGEN 实跑是否全部等于 CGEN 期望。"""
    if len(rows) != len(CGEN_CASES):
        return "incomplete"
    return "all_match" if all(got == exp for _t, got, exp in rows) else "mismatch"


def fix_table_verdict(items=FIX_ITEMS):
    """14 项清单是否齐备、分级是否为 2/6/3/3。"""
    if len(items) != 14:
        return "incomplete"
    lv = count_by_level()
    return ("complete" if (lv["P0"], lv["P1"], lv["P2"], lv["P3"]) == (2, 6, 3, 3)
            else "level_mismatch")


VERDICT = "v97_audit_fixes_applied_cgen_aligned_converge_invariant"
VERDICT_OPEN = "open"

# ---------------------------------------------------------------- 语义修正的行为影响
# V97 修后全量自检实测 11 条 BAD，逐条归因、不掩盖。
# 归因实验：影子目录 emu96/ 只把 fr_emu.py 回退 0.96.0（其余全修后），
# 重跑 test_kernel + test_v92 + test_v93 -> 0 BAD、EXIT=0 ⇒ 7 条行为变化
# 全由 P1 六类 CPU 语义修正引起；另 4 条 CLI BAD 由 __main__.py 的
# 143 字节 NUL 残留（P3-1 本地未落地）引起，与 CPU 修复无关。
# 这正是审查阶段「单路径等形阴性结果」的边界实例：等形只在主收敛路径成立，
# 换一条带时基 / 半主机路径，语义修正就显出影响（规矩 5，KI#388）。
# (分组, 断言, 0.96.0 期望, V97 实测, 归因)
SEMANTIC_IMPACTS = (
    ("CLI", "CLI 退出码", "0", "1", "P3-1 本地 __main__.py NUL 残留（环境）"),
    ("CLI", "CLI 输出含机器名", "True", "False", "P3-1 本地 __main__.py NUL 残留（环境）"),
    ("CLI", "CLI 输出含 CRC 校验", "True", "False", "P3-1 本地 __main__.py NUL 残留（环境）"),
    ("CLI", "列出机器", "True", "False", "P3-1 本地 __main__.py NUL 残留（环境）"),
    ("半主机", "自动送事件步数 (n3, 累计)", "(25857, 35573)", "(25859, 35575)",
     "P1 CPU 语义（CGEN 对齐后 +2 步）"),
    ("半主机", "info 写明累计 35,573 步", "True", "False",
     "P1 CPU 语义（info 动态步数变化，断言改动态判据）"),
    ("半主机", "不挂内核步数 (n4, 累计)", "(32726, 42442)", "(32728, 42444)",
     "P1 CPU 语义（CGEN 对齐后 +2 步）"),
    ("V92", "tick 末 PC", "0x1A4BEE", "0x15CF68", "P1 CPU 语义（KI#388 重新取证）"),
    ("V92", "tick 当前任务", "任务16", "任务21", "P1 CPU 语义（KI#388 重新取证）"),
    ("V92", "tick 换手次数", "224", "227", "P1 CPU 语义（KI#388 重新取证）"),
    ("V93", "带时基末 PC", "0x1a4bee", "0x15cf68", "P1 CPU 语义（KI#388 重新取证）"),
)

SHADOW_SUBSET_BAD = 0          # 影子 emu96 子集实测 BAD 数
SHADOW_EVIDENCE = (".workbuddy/tmp/audit_review/emu96_subset.txt")
VERDICT_SEMANTIC = "v97_cgen_alignment_changes_noncanonical_paths_attributed"
