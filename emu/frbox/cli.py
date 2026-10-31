# -*- coding: utf-8 -*-
"""QEMU 式启动参数。

    python -m frbox                       # 默认 d300-b，跑到 -max-steps
    python -m frbox -M d300-a -S          # 起 A 段并停在提示符
    python -m frbox -M d300-b -d int,dev  # 打开中断/设备日志
    python -m frbox -M help               # 列出机器

对照关系：
    QEMU                        frbox
    qemu-system-x86_64 -M pc    python -m frbox -M d300-b
    -kernel vmlinuz             -kernel B3000111.bin
    -S（启动即暂停）              -S
    -d in_asm,int -D log.txt    -d in_asm,int -D log.txt
    -monitor stdio              -monitor stdio
    -serial stdio               -serial stdio（默认，串口字节直接打到终端）
"""
import argparse
import sys

from .machine import MACHINES, create, machine_names
from .monitor import Monitor
from .trace import Trace


def build_parser():
    p = argparse.ArgumentParser(
        prog="frbox", add_help=True,
        description="D300 固件全系统模拟器（FR80 CPU + 总线 + 外设 + monitor）")
    p.add_argument("-M", "--machine", default="d300-b",
                   help="机器型号：%s，或 help" % " / ".join(machine_names()))
    p.add_argument("-kernel", dest="kernel", default=None,
                   help="固件镜像路径（默认取 D300Update 里的同版本镜像）")
    p.add_argument("-S", "--start-paused", action="store_true",
                   help="启动后停在 monitor，不自动运行")
    p.add_argument("-d", "--debug", default="",
                   help="日志类别：in_asm,exec,int,dev,unimp,svc,guest,all（逗号分隔）")
    p.add_argument("-D", "--debug-file", default=None, help="日志写入文件")
    p.add_argument("-monitor", dest="monitor", choices=["stdio", "none"], default=None,
                   help="monitor 后端（默认：无 -c 时 stdio）")
    p.add_argument("-serial", dest="serial", choices=["stdio", "none"], default="stdio",
                   help="串口输出方向")
    p.add_argument("-c", "--command", dest="cmds", default=None,
                   help="执行 monitor 命令后退出，多条用 ; 分隔")
    p.add_argument("-max-steps", dest="max_steps", type=int, default=1000000,
                   help="最多执行多少条指令（默认 1000000）")
    p.add_argument("--itron", action="store_true",
                   help="挂上表驱动 μITRON 层（INT #0x40 立即返回）")
    p.add_argument("--itron-block", dest="itron_block", action="store_true",
                   help="挂上会阻塞的 μITRON 半主机：wai_flg 等不到就真的停住")
    p.add_argument("--autoevent", action="store_true",
                   help="与 --itron-block 配合：等不到事件就自动补上并记录（列出握手序列）")
    p.add_argument("--task-from-table", dest="task_from_table", action="store_true",
                   help="--task 只给入口，栈顶从 REALOS 任务表里取")
    p.add_argument("--rp", type=lambda x: int(x, 0), default=None, metavar="ADDR",
                   help="任务初始 RP（任务的 ret 落地处）；推荐固件自带的 0x81338")
    p.add_argument("--realos", type=lambda x: int(x, 0), default=None,
                   metavar="TASKID",
                   help="V10：跑固件自带的 REALOS 并把首次派发改给这个任务号（如 --realos 1）。"
                        "不需要任何 μITRON 半主机；与 --itron*/--task 互斥")
    p.add_argument("--poke", action="append", default=[], metavar="ADDR=VAL[:SIZE]",
                   help="复位后往某地址写值，模拟「外设就绪」，可重复；"
                        "例如 --poke 0x482=0x08")
    p.add_argument("--latch", action="append", default=[], metavar="ADDR=VAL[:N]",
                   help="该地址第 N 次读之后返回 VAL（模拟对方置位握手）；"
                        "例如 --latch 0x482=0x88:2")
    p.add_argument("--clk-lock", dest="clk_lock", type=int, default=None,
                   metavar="N",
                   help="时钟寄存器 0x482 位7 就绪需要几个 tick（默认 8，约 2048 步）；"
                        "0 表示写入启动位后立刻就绪")
    p.add_argument("--ppc", dest="ppc", type=int, default=None,
                   help="每 N 条打一次进度（等价 -d exec 的粗粒度版）")
    p.add_argument("--task", dest="task", default=None,
                   metavar="ENTRY[:SP[:ARG[:STOPPC]]]",
                   help="手工启动任务，跳过 REALOS 调度器。A 段任务 1 入口 = 0x8133A；"
                        "栈顶写 0x462EC（任务表记录里的值，不是表头的 0x4616C）。"
                        "推荐 --task 0x8133A:0x462EC --rp 0x81338，"
                        "或省掉栈顶改用 --task-from-table")
    p.add_argument("--autospin", action="store_true",
                   help="自动破解「等硬件就绪位」的忙等：运行期读轮询指令的寄存器，"
                        "把该位置 1 后继续（等价于外设已就绪）")
    p.add_argument("--autospin-threshold", dest="autospin_threshold", type=int,
                   default=3000, metavar="N",
                   help="同一 0x100 窗口内连续 N 步未离开就算自旋（默认 3000）")
    p.add_argument("--version", action="version", version="frbox 0.10.0")
    return p


def _dash_arg_fix(argv):
    """允许 -kernel/-max-steps/-monitor/-serial 这些「单横线长选项」被正常解析"""
    return argv


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-M", "--machine") and len(argv) > 1 and argv[1] == "help":
        print("可用机器：")
        for n in machine_names():
            print(f"  {n:<8} {MACHINES[n].desc}")
        return 0
    p = build_parser()
    args = p.parse_args(_dash_arg_fix(argv))

    if args.machine.lower() in ("help", "list"):
        for n in machine_names():
            print(f"  {n:<8} {MACHINES[n].desc}")
        return 0

    logf = open(args.debug_file, "w", encoding="utf-8") if args.debug_file else sys.stdout
    trace = Trace()
    trace.out = logf
    if args.debug:
        for c in args.debug.replace(" ", "").split(","):
            if c:
                try:
                    trace.enable(c)
                except ValueError as e:
                    print(e, file=sys.stderr)
                    return 2

    m = create(args.machine, trace=trace, serial_console=(args.serial == "stdio"),
               clk_lock_ticks=args.clk_lock, autospin=args.autospin,
               autospin_threshold=args.autospin_threshold)
    try:
        m.load(args.kernel)
    except FileNotFoundError as e:
        print(f"警告：{e}；只起内存骨架，不加载固件", file=sys.stderr)
    except Exception as e:                                   # noqa: BLE001
        print(f"固件加载失败：{e}", file=sys.stderr)
        return 2
    if args.realos is not None:
        info = m.boot_realos(args.realos, max_steps=args.max_steps)
        rec = info["record"]
        print(f"原生 REALOS 已就位：{info['steps']} 步到派发点；"
              f"派发任务 {rec['id']}（TCB=0x{rec['tcb']:07X}，"
              f"入口 0x{rec['entry']:07X}，栈顶 0x{rec['sp']:07X}）")
    if args.itron:
        m.attach_itron()
    if args.itron_block or args.autoevent:
        m.attach_kernel(auto_event=args.autoevent)
    if args.task:
        try:
            parts = [p for p in args.task.split(":")]
            entry = int(parts[0], 0)
            sp = int(parts[1], 0) if len(parts) > 1 and parts[1] else None
            arg = int(parts[2], 0) if len(parts) > 2 and parts[2] else 0
            stop_pc = int(parts[3], 0) if len(parts) > 3 and parts[3] else None
        except ValueError:
            print(f"--task 格式错误：{args.task}"
                  f"（应为 0x8133A:0x462EC 或 0x8133A:0x462EC:0:0x80506）",
                  file=sys.stderr)
            return 2
        e, s, a = m.boot_task(entry, sp, arg, stop_pc=stop_pc,
                              max_steps=args.max_steps, rp=args.rp,
                              from_table=args.task_from_table)
        print(f"已启动任务：入口 0x{e:06X}  SP=0x{s:08X}  r4=0x{a:X}"
              f"{'  （先跑到 0x%06X）' % stop_pc if stop_pc else ''}")
    for spec in args.poke:
        try:
            left, val = spec.split("=", 1)
            size = 1
            if ":" in val:
                val, sz = val.split(":", 1)
                size = int(sz, 0)
            m.poke(int(left, 0), size, int(val, 0))
        except ValueError:
            print(f"--poke 格式错误：{spec}（应为 0x482=0x08[:1]）", file=sys.stderr)
            return 2
    for spec in args.latch:
        try:
            left, val = spec.split("=", 1)
            n = 1
            if ":" in val:
                val, nn = val.split(":", 1)
                n = int(nn, 0)
            m.bus.io_after[int(left, 0)] = (n, int(val, 0))
        except ValueError:
            print(f"--latch 格式错误：{spec}（应为 0x482=0x88:2）", file=sys.stderr)
            return 2

    if not args.cmds:
        print(m.info())

    if args.cmds:
        mon = Monitor(m, out=print)
        for line in args.cmds.split(";"):
            if line.strip() and not mon.exec_line(line):
                break
        return 0

    want_monitor = args.monitor == "stdio" or (args.monitor is None and args.start_paused)
    if want_monitor:
        if args.start_paused:
            Monitor(m, out=print).run(sys.stdin)
        else:
            Monitor(m, out=print).run(sys.stdin)
        return 0

    n = m.run(args.max_steps)
    print(f"\n执行 {n:,} 步后停：PC=0x{m.cpu.pc:08X}"
          + (f"（{m.cpu.halt_reason}）" if m.cpu.halted else "（到达步数上限）"))
    print(m.bus.mtree())
    for d in m.devices:
        print("  " + d.info())
    if trace.summary():
        print(trace.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
