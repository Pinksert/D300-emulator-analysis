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
from . import __version__


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
    p.add_argument("--events", nargs="?", type=int, const=4096, default=None,
                   metavar="PERIOD",
                   help="挂「完成位+中断」事件源（默认周期 4096 周期；0 = 按启动位触发）")
    p.add_argument("--bits", default=None, metavar="MASK[:VECS[:PERIOD]]",
                   help="V14：挂「事件位图 0x41A56 + 中断」源，例如 0x0110:0x13,0x19:2048"
                        "（只给 MASK 时向量默认 0x13,0x19、周期 2048）")
    p.add_argument("--msg", action="store_true",
                   help="V17：挂信箱源（借固件内核投递消息）。配合 --realos 用，"
                        "之后 monitor 里 msg send 投一条")
    p.add_argument("--msg-every", dest="msg_every", type=int, default=None,
                   metavar="N",
                   help="V17：每 N 步自动投一条消息（隐含 --msg），最大步数按 --max-steps 分批跑")
    p.add_argument("--cb", action="store_true",
                   help="V18：挂上两个回调槽（只观测，不主动 tick）")
    p.add_argument("--cb-tick", dest="cb_tick", type=int, default=None,
                   metavar="N",
                   help="V18：每 N 步替主循环扫一圈回调槽（隐含 --cb）。"
                        "主循环 0x80588 自己从不执行，这是外部注入的 tick")
    p.add_argument("--irq2b", dest="irq2b", type=int, default=None, metavar="N",
                   help="V19：接上 INT 0x2B（0x80588 的位分派器）并请求 N 次。"
                        "配合 --cb 看两个回调槽如何自己往前走")
    p.add_argument("--answer", dest="answer", action="store_true",
                   help="V20：在 0x9C97A 上硬写 0x174 的完成位（V19 的做法，留作对照）。"
                        "默认不写 —— 由 XferHandshake 设备模型自己置完成位")
    p.add_argument("--no-answer", dest="no_answer", action="store_true",
                   help=argparse.SUPPRESS)   # V19 旧开关，V20 起默认就是这样
    p.add_argument("--flow", action="store_true",
                   help="挂服务调用流水记录器（配合 --realos，事后用 monitor 的 flow 看）")
    p.add_argument("--cov", action="store_true",
                   help="记录覆盖率（monitor 的 cov 看；找「一次都没跑过的代码段」用）")
    p.add_argument("--boot", metavar="TASKS", default=None,
                   help="把任务表「复位就激活」位置 1（2,3,6 / dormant / all）——"
                        " 让 REALOS 初始化自己创建这些任务（V16）")
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
    p.add_argument("--version", action="version", version=f"frbox {__version__}")
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
    if args.boot:
        from . import taskboot
        spec = args.boot.strip()
        if spec in ("dormant", "all"):
            done = taskboot.activate(m, spec)
        else:
            done = taskboot.activate(m, [int(x, 0) for x in spec.replace(",", " ").split()])
        print("任务表「复位就激活」已开：%s（这是改配置做的实验，真实固件这些位是 0）"
              % (", ".join("任务%d" % t for t in done) or "（无变化）"))
    if args.cov:
        m.enable_cov()
    if args.events is not None:
        m.enable_events(period=args.events)
    if args.bits:
        spec = args.bits.split(":")
        mask = int(spec[0], 0)
        vecs = ([int(v, 0) for v in spec[1].split(",")]
                if len(spec) > 1 and spec[1] else [0x13, 0x19])
        period = int(spec[2], 0) if len(spec) > 2 and spec[2] else 2048
        dev = m.enable_bits(mask=mask, vecs=vecs, period=period)
        print(f"事件位图源已挂：0x41A56 |= 0x{mask:04X} → "
              f"{','.join('0x%02X' % v for v in vecs)}，周期 {period}")
    if args.msg or args.msg_every:
        mb = m.enable_msgs()
        print(f"信箱源已挂：mbx={mb.mbx} 控制块 0x466E4 发送缓冲 0x{mb.buf:05X}"
              f"（样本来自固件自己的 0x{mb.sample:05X}）"
              + (f"，每 {args.msg_every} 步投一条" if args.msg_every else ""))
    if args.cb or args.cb_tick or args.irq2b:
        cb = m.enable_callbacks()
        print(f"回调槽已挂：{cb.summary()}"
              + (f"，每 {args.cb_tick} 步扫一圈" if args.cb_tick else ""))
    if args.irq2b:
        h = m.enable_isr2b()
        print(f"INT 0x2B 已挂：entry=0x{h.state()['entry']:05X} "
              f"ICR=0x{h.state()['icr']:02X} 待办标志 {h.pending() or '（空）'}"
              + ("，硬写完成位（对照）" if args.answer else "，完成位由设备模型给"))
    if args.flow:
        m.attach_flow()
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

    if args.irq2b:
        h = m.isr2b

        def _ev(r):
            print(f"  第{r['n']:>4} 次中断：{r['steps']:>6} 步  覆盖+{r['delta']:<5}"
                  f"服务+{r['svc']}  {r['cb']}"
                  + ("  （卡在轮询，没回到 reti）" if r["cut"] else ""))

        h.burst(args.irq2b, answer=args.answer, on_event=_ev)
        n = m.step_count
    elif args.msg_every or args.cb_tick:
        n = 0
        every = args.msg_every or args.cb_tick or 20000
        batch = max(1000, min(every, 20000))
        while n < args.max_steps:
            n += m.run(min(batch, args.max_steps - n), spin_guard=False)
            m.bus.wr(0x90, 1, m.bus.rd(0x90, 1) | 0x02)
            for v in (0x13, 0x19):
                m.raise_irq(v)
            if m.cpu.halted or m.fault:
                break
            if args.cb_tick and m.cb is not None:
                m.cb.tick(1)
            if args.msg_every and m.msg is not None and \
                    m.msg.task5_state()["kind"] == "msg":
                m.msg.send()
    else:
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
