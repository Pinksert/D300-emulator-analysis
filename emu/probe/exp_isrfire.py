# -*- coding: utf-8 -*-
"""exp_isrfire.py —— A 段中断闭环实验：48 个向量逐一注入

此前（V6）只验证了「注入向量 → ISR → reti 回原位」的骨架，没验证 ISR 里
`call @r0` 是否真跳进应用区回调。本轮把闭环补上：

  中断请求 → EIT 压栈 → ISR stub（0x80A54 一带）→ 取回调槽 0x3F0xx
          → call @r0 进应用区真函数 → 逐层 ret → reti → 回轮询点 0x0DCD2C

做法：
  1. boot_task 拉起任务 1，跑到第一处硬件等待 0x0DCD2C，再跑 519 步让应用区
     把回调槽注册完（1,981 步时槽还是空的，2,500 步时 5 个槽有值）
  2. savevm 存一份基点快照
  3. 48 个向量逐个 loadvm + 注入，记录步数 / 不同 PC / 是否进回调 / 是否回原位

判据：
  闭环   = 回到注入点且 r15 复原
  进回调 = PC 序列里出现过该槽里存的值（说明 call @r0 真的跳进去了）
  跑飞   = 撞 0x81338（bra -1 停机）或超步数
"""
import sys
import os
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from frbox.machine import create
from frbox import snapshot

TASK_ENTRY = 0x0008133A
TASK_RP = 0x00081338      # 停机占位（任务 ret 的落地处）
TASK_SP = 0x000462EC      # 任务表记录里的真值
BOOT_STOP = 0x00080506          # 复位链里 BSS 清零之后、main 继续之前
POLL_PC = 0x000DCD2C            # 任务 1 的第一个硬件等待（等 0x0AF 位7）
SETTLE = 2500                   # 从任务入口再跑 2500 步，让回调槽注册完
MAX_STEPS = 20000


def bring_up():
    m = create("d300-a")
    m.load()
    m.boot_task(TASK_ENTRY, sp=TASK_SP, arg=0, stop_pc=BOOT_STOP, rp=TASK_RP)
    m.run(max_steps=SETTLE, spin_guard=False)
    return m


def main():
    m = bring_up()
    c = m.cpu
    print("== 基点 ==")
    print(f"  任务入口 0x{TASK_ENTRY:06X}  栈 0x{TASK_SP:06X}")
    print(f"  当前 PC 0x{c.pc:06X}  步数 {m.step_count}  "
          f"I={c.i}  ILM=0x{c.ilm:02X}  SP=0x{c.r[15]:06X}")
    print()

    table = {r["vec"]: r for r in m.irq_table()}
    live = [(v, r) for v, r in table.items() if any(r["callbacks"])]
    print(f"== 基点处已注册回调的向量：{len(live)} 条 ==")
    for v, r in live:
        pairs = "  ".join(f"0x{s:05X}→0x{cb:06X}"
                          for s, cb in zip(r["slots"], r["callbacks"]) if cb)
        print(f"  vec 0x{v:02X}  L{r['level']:02X}  ISR 0x{r['isr']:06X}  {pairs}")
    print()

    snap = os.path.join(tempfile.gettempdir(), "frbox_isrfire.snap")
    snapshot.save(m, snap)
    print(f"== 基点快照 {os.path.getsize(snap)} 字节：{snap} ==")
    print()

    home = c.pc
    rows = []
    for vec in range(0x10, 0x40):
        snapshot.load(m, snap)
        base_sp = m.cpu.r[15]
        r = table[vec]
        cbs = set(cb for cb in r["callbacks"] if cb)
        got = m.irq_return(vec, max_steps=MAX_STEPS, home=home)
        pcs = set(got["pcs"])
        hit_cb = sorted(cbs & pcs)
        entered_app = any(0x00080000 <= p < 0x000E0000 for p in pcs)
        entered_kern = any(0x000E0000 <= p < 0x00110000 for p in pcs)
        rows.append({
            "vec": vec, "level": got["level"], "isr": r["isr"],
            "kind": r["kind"], "steps": got["steps"], "npcs": len(pcs),
            "back": got["back"] is not None,
            "cb": hit_cb, "app": entered_app, "kern": entered_kern,
            "halt": got["halted"],
            "sp": m.cpu.r[15] == base_sp,
        })

    print("== 48 个向量逐一注入 ==")
    print("  vec  L    ISR      类型  步数  不同PC 回原位 进回调 进应用区")
    for x in rows:
        cb = ",".join(f"0x{a:06X}" for a in x["cb"]) or "-"
        print(f"  0x{x['vec']:02X} {x['level']:02X}  0x{x['isr']:06X}  "
              f"{x['kind']:<5} {x['steps']:>5} {x['npcs']:>6} "
              f"{'是' if x['back'] else '否':<3}  {cb:<9} "
              f"{'是' if x['app'] else '否'}")
    print()

    stub = [x for x in rows if x["kind"] == "stub"]
    unused = [x for x in rows if x["kind"] == "unused"]
    special = [x for x in rows if x["kind"] == "special"]
    closed = [x for x in rows if x["back"] and x["sp"]]
    withcb = [x for x in rows if x["cb"]]
    default_only = [x for x in stub if not x["cb"] and x["back"]]
    bad = [x for x in rows if not x["back"] or not x["sp"]]

    print("== 汇总 ==")
    print(f"  向量总数            {len(rows)}")
    print(f"  真 stub             {len(stub)}")
    print(f"  未用（兜底桩）      {len(unused)}")
    print(f"  特殊（vec 0x3F）    {len(special)}")
    print(f"  闭环且 SP 复原      {len(closed)}")
    print(f"  → 真回调被调用      {len(withcb)}  "
          f"{[hex(x['vec']) for x in withcb]}")
    print(f"  → 走默认空 ret      {len(default_only)}")
    print(f"  未闭环 / SP 异常    {len(bad)}  {[hex(x['vec']) for x in bad]}")
    print()

    print("== 闭环样板：vec 0x13 的 20 步轨迹 ==")
    snapshot.load(m, snap)
    home = m.cpu.pc
    m.raise_irq(0x13)
    for i in range(20):
        pc = m.cpu.pc
        asm = m.disasm_at(pc, 1).split("  ", 1)[-1]
        print(f"  {i:>2}  0x{pc:06X}  {asm}")
        m.step(1)
    print()

    # 例外：vec 0x3C 不是单槽模板，它先看直接页 0xAD 的状态位再决定走哪一支
    print("== 例外：vec 0x3C 是「状态位驱动的双分支」==")
    print("  0x0811E6  btsth #4, @0xAD   ; 测 0xAD 位6")
    print("  0x0811F8  ld   @0x3F0D0     ; 分支 A 的槽（实测为空，有空槽保护）")
    print("  0x08120A  btsth #8, @0xAD   ; 测 0xAD 位7")
    print("  0x08121C  ld   @0x3F0D4     ; 分支 B 的槽 = 0x0E2D1C")
    print()
    print("  0xAD 取值   步数  回原位  走 A 支(0x3F0D0)  走 B 支(0xE2D1C)")
    for bits in (0x00, 0x40, 0x50, 0x80, 0xA0, 0xF0):
        snapshot.load(m, snap)
        m.dp.shadow[0xAD] = bits
        got = m.irq_return(0x3C, max_steps=MAX_STEPS, home=home)
        pcs = set(got["pcs"])
        print(f"  0x{bits:02X}       {got['steps']:>4}  "
              f"{'是' if got['back'] is not None else '否':<4}   "
              f"{'是' if 0x000811F8 in pcs else '否':<14}  "
              f"{'是' if 0x000E2D1C in pcs else '否'}")
    print()
    print("  只有 0xAD 位7+位5 同时置位（0xA0）才走 B 支去调 0x3F0D4 的回调；")
    print("  位6+位4（0x50）走 A 支，而 A 支的槽 0x3F0D0 是空的 —— ISR 里自带空槽保护")
    print("  （ld → cmp #0 → beq 跳过 call），所以不会跳到地址 0。")
    print()


if __name__ == "__main__":
    main()
