# -*- coding: utf-8 -*-
"""HMP 式命令行（QEMU monitor 的简化版）。

命令刻意对齐 QEMU 的肌肉记忆：
    info registers / info mtree / info devices / info irq
    x/16x 0x40000      x/8i 0x000FC000
    step [N]           cont [MAX]         fast [N]（不吃自旋保护）
    bp 0x40000         bps / bpc ADDR
    reset              load [PATH]        irq raise 0x1B    irq rx 41 42
    savevm PATH        loadvm PATH         info snap
    boottask ENTRY[:SP[:ARG[:STOPPC]]] [rp=ADDR] [table]
                                            手工启动任务（跳过 REALOS 调度器）
    autospin [on|off|reset|status]          自旋消除器（等硬件就绪位）
    svc [N]            服务调用直方图（μITRON 半主机）
    flag [list|set ID=BITS|clr ID]          事件标志（注入外部事件）
    resume             从被阻塞的 int 指令继续
    tasks               REALOS 任务状态（入口 / 当前 PC / 在等哪个 flag 的哪些位）
    flow [N]            最近 N 条服务调用流水（被动记录，不改执行）
    events [PERIOD]     挂/看「完成位 + 中断」事件源（默认周期 4096）
    bits [MASK[:VECS[:PERIOD]]]  挂/看「事件位图 0x41A56 + 中断」源（V14）
    msg [send [W0..W7]|sample|queue]  信箱：借固件内核投一条消息（V17）
    cb [tick [N]|chain [N]]          回调槽：替主循环扫一圈（V18）
    irq2b [N|answer N]  接上 INT 0x2B（0x80588）：请求 N 次，派发器自己往前走（V19/V20）
    cov [MINLEN]        覆盖率：还差哪些从没执行过的代码段（V15）
    readyq            就绪位图 + 13 个优先级槽的队列（V15）
    boot [TASKS|dormant] / boot off TASKS   改任务表「复位就激活」位（V16）
    unlock TASK [ROUNDS]                    一站一站把任务往下推（V16）
    wake FLAG BITS      用固件自己的 sys_set_flg 置位（注入一个外部事件）
    kcall SVC [r4..r7]  直接调一次固件系统调用（服务号用负数，如 -48 = set_flg）
    realos [TASKID]     固件自带 REALOS：13 条记录 / 让内核原生调度某任务
    dev                dump                quit
"""
import re

from . import __version__
from .machine import MACHINES


class Monitor:
    def __init__(self, machine, out=print):
        self.m = machine
        self.out = out
        self.bps = []

    # ------------------------------------------------------------------ 入口
    def run(self, stdin):
        self.out(f"frbox {__version__}  ——  机器 {self.m.name}（{self.m.desc}）")
        self.out("输入 help 看命令；quit 退出。")
        while True:
            try:
                line = stdin.readline()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if not self.exec_line(line):
                break
        return 0

    def exec_line(self, line):
        """执行一行；返回 False 表示要退出"""
        for part in [p.strip() for p in line.split(";") if p.strip()]:
            if not self.execute(part):
                return False
        return True

    def execute(self, line):
        if line.startswith("x/") or line.startswith("x "):
            cmd, arg = "x", line[1:].strip()       # QEMU 写法 x/16x ADDR
        else:
            cmd, _, arg = line.partition(" ")
            arg = arg.strip()
        cmd = cmd.lower()
        if cmd == "info" and arg.strip().lower() == "snap":
            cmd, arg = "info_snap", ""
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            self.out(f"未知命令 {cmd}，输入 help")
            return True
        return fn(arg)

    # ------------------------------------------------------------------ 基本
    def cmd_help(self, arg=""):
        self.out(__doc__.split("\n\n", 1)[1].strip())
        return True

    def cmd_quit(self, arg=""):
        return False

    cmd_q = cmd_quit
    cmd_exit = cmd_quit

    def cmd_info(self, arg=""):
        what = (arg or "all").lower()
        m = self.m
        if what in ("registers", "reg", "all"):
            self.out(m.cpu.dump_regs())
        if what in ("mtree", "all"):
            self.out(m.bus.mtree())
        if what in ("devices", "dev", "all"):
            for d in m.devices:
                self.out("  " + d.info())
        if what in ("irq", "all"):
            self.out("  中断线绑定（只有标注实测的才有硬件依据）：")
            for line, vec, lvl, note in m.icu.table():
                self.out(f"    线 0x{line:02X} → 向量 0x{vec:02X} / level {lvl}   {note}")
            hw = [r for r in m.cpu.irq_log if r[0] != 0x40]
            # int #0x40 有两种走法：被 μITRON 钩子截住（不产生 irq_log 记录）或
            # 没人挂钩子时真进向量表。所以不能只看 irq_log，要用 CPU 侧的计数器。
            traps = getattr(m.cpu, "int40", 0)
            self.out(f"    已受理硬件中断 {len(hw)} 次，待决 {len(m.cpu.irq_pending)} 条；"
                     f"μITRON 陷阱 int #0x40 {traps} 次")
        if what in ("rom", "firmware", "all"):
            self.out(m.info())
        if what not in ("registers", "reg", "all", "mtree", "devices", "dev",
                        "irq", "rom", "firmware"):
            self.out("info 支持：registers / mtree / devices / irq / rom / all")
        return True

    # ------------------------------------------------------------------ 内存
    def cmd_x(self, arg):
        mres = re.match(r"/(\d+)\s*([xbwdi])?\s+(.+)$", arg)
        n, fmt, rest = (int(mres.group(1)), mres.group(2) or "x", mres.group(3)) if mres \
            else (16, "x", arg)
        addr = self._num(rest)
        if addr is None:
            self.out("用法：x/16x 0x40000  （格式 x=字 b=字节 i=反汇编）")
            return True
        if fmt == "i":
            self.out(self.m.disasm_at(addr, n))
            return True
        per = 4 if fmt == "x" else (1 if fmt == "b" else 2)
        for row in range(0, n, 8 if per == 1 else 4):
            cols = []
            for i in range(8 if per == 1 else 4):
                k = row + i
                if k >= n:
                    break
                v = self.m.bus.rd(addr + k * per, per)
                cols.append(f"{v:0{per * 2}X}")
            self.out(f"0x{addr + row * per:08X}: " + " ".join(cols))
        return True

    # ------------------------------------------------------------------ 执行
    def cmd_step(self, arg=""):
        n = self._num(arg) or 1
        self.m.step(n)
        self.out(self.m.cpu.dump_regs())
        return True

    cmd_si = cmd_step

    def cmd_cont(self, arg=""):
        max_steps = self._num(arg) or 1000000
        m = self.m
        if m.cpu.halted:
            self.out(f"CPU 已停机：{m.cpu.halt_reason}")
            return True
        bps = set(self.bps)
        # 断点就压在当前位置时，先单步一次再跑，否则会立刻又「撞」上去
        if m.cpu.pc in bps:
            m.step(1)
            max_steps -= 1
        target = m.step_count + max(0, max_steps)
        m.spin = None
        m._spin_base, m._spin_count = None, 0
        while m.step_count < target and not m.cpu.halted:
            n = min(2048, target - m.step_count)
            done = m.run(n, stop_pcs=bps)
            if done < n:
                if m.spin:
                    base, cnt = m.spin
                    self.out(f"检测到自旋：0x{m.cpu.pc:08X} 附近连续 {cnt:,} 步未离开")
                else:
                    self.out(f"命中断点 0x{m.cpu.pc:08X}")
                break
        self.out(f"执行至 0x{m.cpu.pc:08X}，累计 {m.step_count:,} 步"
                 + (f"（停机：{m.cpu.halt_reason}）" if m.cpu.halted else ""))
        return True

    cmd_run = cmd_cont
    cmd_c = cmd_cont

    def cmd_fast(self, arg=""):
        """fast [N] —— 关掉自旋保护直接跑 N 步（喂事件时用，cont 会在自旋处停下）"""
        n = self._num(arg) or 1000000
        m = self.m
        if m.cpu.halted:
            self.out(f"CPU 已停机：{m.cpu.halt_reason}")
            return True
        m.run(max_steps=n, spin_guard=False)
        self.out(f"跑至 0x{m.cpu.pc:08X}，累计 {m.step_count:,} 步"
                 + (f"（停机：{m.cpu.halt_reason}）" if m.cpu.halted else ""))
        return True

    def cmd_reset(self, arg=""):
        self.m.reset()
        self.out(f"复位：PC=0x{self.m.cpu.pc:08X}（向量 0x00）")
        return True

    def cmd_load(self, arg=""):
        self.m.load(arg or None)
        self.out(f"已加载 {self.m.fw_path}，CRC16 "
                 f"{'OK' if self.m.fw_crc_ok else 'BAD'}，入口 0x{self.m.entry:08X}")
        return True

    # ------------------------------------------------------------------ 断点
    def cmd_bp(self, arg):
        a = self._num(arg)
        if a is None:
            self.out("用法：bp 0x40000")
            return True
        if a not in self.bps:
            self.bps.append(a)
        self.out(f"断点 0x{a:08X}（共 {len(self.bps)} 个）")
        return True

    cmd_b = cmd_bp

    def cmd_bps(self, arg=""):
        if not self.bps:
            self.out("没有断点")
        for a in sorted(self.bps):
            self.out(f"  0x{a:08X}")
        return True

    def cmd_bpc(self, arg):
        a = self._num(arg)
        if a in self.bps:
            self.bps.remove(a)
            self.out(f"删除断点 0x{a:08X}")
        else:
            self.out("没有这个断点")
        return True

    cmd_delete = cmd_bpc

    # ------------------------------------------------------------------ 中断
    def cmd_irq(self, arg=""):
        parts = arg.split()
        m = self.m
        if not parts or parts[0] == "list":
            return self.cmd_info("irq")
        if parts[0] == "table":
            rows = m.irq_table()
            real = [r for r in rows if r["kind"] == "stub"]
            self.out(f"向量表：{len(rows)} 条  ICR 已配置 "
                     f"{sum(1 for r in rows if r['level'])} 条  "
                     f"真 stub {len(real)} 条  未用 "
                     f"{sum(1 for r in rows if r['kind'] == 'unused')} 条")
            self.out("  vec  ICR L    ISR       槽        回调")
            for r in rows:
                cb = " ".join(f"0x{c:08X}" for c in r["callbacks"]) or "-"
                slot = ",".join(f"{s:05X}" for s in r["slots"]) or "-"
                mark = {"unused": " 未用", "special": " 特殊"}.get(r["kind"], "")
                self.out(f"  0x{r['vec']:02X}  {r['icr']:02X} {r['level']:02X}  "
                          f"{f"0x{r['isr']:08X}"}  {slot:<9} {cb}{mark}")
            return True
        if parts[0] == "fire" and len(parts) >= 2:
            vec = self._num(parts[1])
            if vec is None:
                self.out("用法：irq fire 0x13 [level]")
                return True
            lvl = self._num(parts[2]) if len(parts) > 2 else None
            limit = self._num(parts[3]) if len(parts) > 3 else 20000
            before = m.cpu.pc
            # 只打印将要用的级别，真正的请求交给 irq_return 发一次；
            # 早先这里先 raise_irq 再 irq_return，等于发了两遍，残留的那次
            # 会在下一条注入时先被受理，把观测结果整个搞混（V8 修）。
            want = (lvl & 0x1F) if lvl is not None else m.irq_level(vec)
            self.out(f"向量 0x{vec:02X} 请求 level 0x{(want or 0x1F):02X}"
                     f"（I={m.cpu.i} ILM=0x{m.cpu.ilm:02X}）")
            r = m.irq_return(vec, max_steps=limit, home=before, level=lvl)
            back = r["back"]
            self.out(f"跑了 {r['steps']} 步，经过 {len(set(r['pcs']))} 个不同 PC，"
                     f"PC=0x{m.cpu.pc:08X}"
                     f"{' 已回原位' if back else ' 未回原位'}"
                     f"，SP{'已' if r['sp_restored'] else '未'}复原"
                     f"，待决 {len(m.cpu.irq_pending)}")
            return True
        if parts[0] == "raise" and len(parts) >= 2:
            vec = self._num(parts[1])
            lvl = self._num(parts[2]) if len(parts) > 2 else 8
            if vec is None:
                self.out("用法：irq raise 0x1B [level]")
                return True
            ok = m.icu.raise_line(vec, lvl)
            self.out(f"拉高线 0x{vec:02X} → {'已排队' if ok else '未绑定，忽略'}"
                     f"（I={m.cpu.i} ILM={m.cpu.ilm}）")
            return True
        if parts[0] in ("rx", "serial") and len(parts) >= 2:
            data = bytearray()
            for tok in parts[1:]:
                tok = tok.replace("0x", "")
                data += bytes.fromhex(tok if len(tok) % 2 == 0 else "0" + tok)
            m.serial.push_rx(bytes(data))
            self.out(f"向串口注入 {len(data)} 字节，接收队列 {len(m.serial.rx)}")
            return True
        if parts[0] == "pend":
            self.out(f"待决：{m.cpu.irq_pending}  已受理 {len(m.cpu.irq_log)}")
            return True
        self.out("irq 支持：list / raise 0x1B [level] / rx 41 42 / pend")
        return True

    # ------------------------------------------------------------------ 其他
    def cmd_boottask(self, arg="", rest=""):
        """boottask 0x8133A:0x462EC:0:0x80506 [rp=0x81338] [table] —— 手工启动任务"""
        spec = arg or rest
        if not spec:
            self.out("用法：boottask ENTRY[:SP[:ARG[:STOPPC]]] [rp=ADDR] [table]\n"
                     " A 段任务 1：boottask 0x8133A:0x462EC:0:0x80506 rp=0x81338\n"
                     "   rp=    任务的 ret 落地处（不设会沿用引导阶段的残值）\n"
                     "   table  只给入口，栈顶从 REALOS 任务表取")
            return True
        opts = {"rp": None, "from_table": False}
        tokens = spec.split()
        spec = tokens[0] if tokens else ""
        for t in tokens[1:]:
            if t == "table":
                opts["from_table"] = True
            elif t.startswith("rp="):
                try:
                    opts["rp"] = int(t[3:], 0)
                except ValueError:
                    self.out(f"rp= 后面要跟地址，收到 {t}")
                    return True
            else:
                self.out(f"不认识的选项：{t}")
                return True
        try:
            parts = spec.split(":")
            entry = int(parts[0], 0)
            sp = int(parts[1], 0) if len(parts) > 1 and parts[1] else None
            a = int(parts[2], 0) if len(parts) > 2 and parts[2] else 0
            stop = int(parts[3], 0) if len(parts) > 3 and parts[3] else None
        except ValueError:
            self.out(f"格式错误：{spec}")
            return True
        e, s, aa = self.m.boot_task(entry, sp, a, stop_pc=stop,
                                    rp=opts["rp"], from_table=opts["from_table"])
        self.out(f"已启动任务：入口 0x{e:06X}  SP=0x{s:08X}  r4=0x{aa:X}"
                 f"{'  RP=0x%06X' % opts['rp'] if opts['rp'] is not None else ''}"
                 f"{'  （先跑到 0x%06X）' % stop if stop else ''}")
        if opts["rp"] is None:
            self.out("⚠️ 没给 rp=：任务的 ret 会跳到引导阶段留下的残值上。"
                     "正常返回请加 rp=0x81338（固件自带的 bra -1 停机占位）")
        return True

    def cmd_poke(self, arg):
        """poke 0x482=0x08[:1]  —— 往地址写值（模拟外设就绪）"""
        try:
            left, val = arg.split("=", 1)
            size = 1
            if ":" in val:
                val, sz = val.split(":", 1)
                size = int(sz, 0)
            self.m.poke(int(left, 0), size, int(val, 0))
            self.out(f"已写入 0x{int(left, 0):08X} ← 0x{int(val, 0):X}（{size} 字节）")
        except ValueError:
            self.out("用法：poke 0x482=0x08  或 poke 0x40000=0x1234:2")
        return True

    def cmd_latch(self, arg):
        """latch 0x482=0x88:2  —— 该地址第 2 次读之后返回 0x88（模拟对方置位握手）"""
        try:
            left, val = arg.split("=", 1)
            n = 1
            if ":" in val:
                val, nn = val.split(":", 1)
                n = int(nn, 0)
            a = int(left, 0)
            self.m.bus.io_after[a] = (n, int(val, 0))
            self.out(f"已设置：0x{a:08X} 第 {n} 次读之后返回 0x{int(val, 0):X}")
        except ValueError:
            self.out("用法：latch 0x482=0x88:2")
        return True

    def cmd_spin(self, arg=""):
        m = self.m
        a = arg.split()
        if not a:
            self.out(f"自旋检测：窗口 0x{m.spin_window:X}，阈值 {m.spin_limit:,} 步，"
                     f"结果 {('PC 0x%08X 停留 %d 步' % m.spin) if m.spin else '未触发'}")
            return True
        if a[0] == "off":
            m.spin_window = 0
            self.out("自旋检测已关闭")
        elif a[0] == "on":
            m.spin_window = 0x100
            self.out("自旋检测已开启（窗口 0x100）")
        else:
            v = self._num(a[0])
            if v:
                m.spin_limit = v
            self.out(f"阈值 = {m.spin_limit:,} 步")
        return True

    def cmd_autospin(self, arg=""):
        m = self.m
        a = arg.split()
        if not a or a[0] in ("st", "status"):
            self.out(f"自旋消除：{'开' if m.autospin_enabled else '关'}，"
                     f"阈值 {m.spin_breaker.threshold:,} 步，探针 {m.spin_breaker.probe} 步")
            self.out(m.spin_breaker.summary())
            for i, e in enumerate(m.spin_breaker.rows(), 1):
                self.out(f"  {i}. 轮询 0x{e['poll_pc']:06X}（自旋点 0x{e['spin_pc']:06X}）"
                         f" {e['where']} 0x{e['addr']:03X} 位 0x{e['mask']:02X}"
                         f"（0x{e['old']:02X}→0x{e['old'] | e['mask']:02X}）"
                         f" 第 {e['nth']} 次 @ {e['step']:,} 步")
            return True
        if a[0] in ("on", "off"):
            m.autospin_enabled = a[0] == "on"
            self.out(f"自旋消除已{'开启' if m.autospin_enabled else '关闭'}")
            return True
        if a[0] == "reset":
            m.spin_breaker.reset()
            self.out("自旋消除记录已清空")
            return True
        if a[0] == "threshold" and len(a) > 1:
            m.spin_breaker.threshold = self._num(a[1]) or m.spin_breaker.threshold
            self.out(f"阈值 = {m.spin_breaker.threshold:,} 步")
            return True
        self.out("用法：autospin [on|off|reset|status|threshold N]")
        return True

    # ------------------------------------------------- μITRON 半主机
    def cmd_svc(self, arg=""):
        m = self.m
        if m.kernel is None:
            self.out("还没挂 μITRON 半主机（用 --kernel 启动，或 attach_kernel()）")
            return True
        n = self._num(arg) if arg else 12
        rows = m.kernel.stats()
        if not rows:
            self.out("还没有服务调用")
            return True
        total = sum(v for _, v in rows)
        self.out(f"μITRON 服务调用共 {total:,} 次，{len(rows)} 种：")
        for name, c in rows[:n or 12]:
            self.out(f"  {name:<20} {c:>8,}")
        return True

    def cmd_tasks(self, arg=""):
        """tasks —— 13 条任务的状态 / 在等谁 / 当前 PC（V12：读 TCB 而不是任务表初始值）"""
        from . import realos as R
        m = self.m
        rows = R.states(m)
        if not rows:
            self.out("没有任务表")
            return True
        fl = {f["cb"]: f for f in R.flags(m)}
        cur = int.from_bytes(m.fetch_bytes(R.CUR_TCB, 4), "big")
        self.out("REALOS 任务表 @0x%06X（%d 条 × 0x%X 字节）  当前 TCB=0x%06X"
                 % (R.TASK_TABLE, len(rows), R.TASK_REC, cur))
        self.out("  id  激活 TCB      入口       当前 PC    在等什么")
        for t in rows:
            k = t["wait_kind"]
            if k == "flag" and t["wait_flag"]:
                f = fl.get(0x45C38 + (t["wait_flag"] - 1) * 0x10)
                want = "flag%d 位 0x%X（现 0x%X，%d 个在等）" % (
                    t["wait_flag"], t["wait_pat"] & 0xFFFFFFFF,
                    f["pattern"] if f else 0, f["waiters"] if f else 0)
            elif k == "sleep":
                want = "休眠（等 wup_tsk）"
            elif k == "msg":
                want = "等消息（邮箱对象未建模）"
            elif k == "dormant":
                want = "休眠/未启动"
            else:
                want = "—"
            self.out("  %-3d %-4s 0x%05X 0x%06X 0x%06X  %s%s"
                     % (t["id"], "是" if t["active"] else "否", t["tcb"],
                        t["entry"], t["pc"], want,
                        "  ◀ 在跑" if t["current"] else ""))
        parts = []
        for b in R.blockers(m):
            if b["kind"] == "flag":
                parts.append("任务 %d(flag%d/0x%X)" % (b["task"], b["flag"], b["wants"] & 0xFFFFFFFF))
            elif b["kind"] == "sleep":
                parts.append("任务 %d(休眠)" % b["task"])
            else:
                parts.append("任务 %d(等消息)" % b["task"])
        self.out("  在等的：%s" % ("、".join(parts) or "无"))
        return True

    def cmd_events(self, arg=""):
        """events [PERIOD] —— 看/挂「完成位 + 中断」事件源（无参数 = 看状态）"""
        m = self.m
        a = arg.split()
        if a:
            per = self._num(a[0])
            if per is None:
                self.out("用法：events [PERIOD]   例如 events 4096（0 = 按启动位触发）")
                return True
            ev = m.enable_events(period=per)
            self.out("事件源已挂：周期 %d 周期，状态位 0x%02X/0x%02X → vec 0x%02X"
                     % (ev.period, ev.status[0], ev.status[1], ev.vector))
            return True
        ev = getattr(m, "event", None)
        if ev is None:
            self.out("没有事件源（events 4096 挂一个，或命令行 --events）")
            return True
        st = ev.state()
        self.out("事件源状态：" + "  ".join("%s=%s" % (k, v) for k, v in st.items()))
        return True

    def cmd_bits(self, arg=""):
        """bits [MASK[:VECS[:PERIOD]]] —— 看/挂「事件位图 + 中断」源（V14）"""
        m = self.m
        a = arg.split()
        if a:
            spec = a[0].split(":")
            mask = self._num(spec[0])
            if mask is None:
                self.out("用法：bits 0x0110:0x13,0x19:2048   （只给掩码时向量默认 0x13,0x19）")
                return True
            vecs = ([self._num(x) for x in spec[1].split(",")]
                    if len(spec) > 1 and spec[1] else [0x13, 0x19])
            period = self._num(spec[2]) if len(spec) > 2 and spec[2] else 2048
            dev = m.enable_bits(mask=mask, vecs=vecs, period=period)
            self.out("事件位图源已挂：0x41A56 |= 0x%04X → %s，周期 %d"
                     % (dev.mask, ",".join("0x%02X" % v for v in dev.vecs), dev.period))
            return True
        devs = [d for d in getattr(m, "devices", []) if getattr(d, "name", "") == "bits"]
        if not devs:
            self.out("没有位图源（bits 0x0110 挂一个，或命令行 --bits）")
            return True
        for d in devs:
            self.out("位图源：" + "  ".join("%s=%s" % (k, v) for k, v in d.state().items()))
        self.out("位图现值 0x41A56 = 0x%04X" % m.bus.rd(0x41A56, 2))
        return True

    def cmd_msg(self, arg=""):
        """msg [send [W0..W7]|sample|queue] —— 信箱：借固件内核投一条消息（V17）

        `msg`              看信箱状态（投了几条 / 任务 5 在等什么 / 队列头）
        `msg send`         按样本投一条（样本 = 固件自己在 0x417DC 造的那条）
        `msg send 8000 0 c0 2000`  覆写前 4 个半字再投
        `msg queue`        沿控制块 +0x00 的消息链走一遍
        """
        m = self.m
        mb = getattr(m, "msg", None)
        a = arg.split()
        if not a:
            if mb is None:
                self.out("没有信箱源（msg send 会顺手挂一个）—— 需要 --realos 起来之后才行")
                return True
            st = mb.state()
            self.out("信箱 #%s  控制块 0x%05X  发送缓冲 0x%05X"
                     % (st["mbx"], 0x466E4, mb.buf))
            self.out("  已投 %d 条；任务 5 现在 %s" % (st["sent"], st["task5"]))
            q, tail = mb.queue()
            self.out("  队列头 %s（尾部 0x%05X）"
                     % (", ".join("0x%05X" % p for p in q) or "空", tail))
            return True
        if mb is None:
            mb = m.enable_msgs()
        sub = a[0]
        if sub == "send":
            words = [self._num(x) for x in a[1:9]]
            if words and any(w is None for w in words):
                self.out("用法：msg send 8000 0 c0 2000   （十六进制半字，最多 8 个）")
                return True
            r = mb.send(words or None)
            t5 = mb.task5_state() or {}
            self.out("snd_msg(mbx=%d, 0x%05X) → err=%s（%s 步）；任务 5 现在 %s pc=0x%08X"
                     % (mb.mbx, mb.buf, r.get("err"), r.get("steps"),
                        t5.get("kind"), t5.get("pc", 0)))
            return True
        if sub == "sample":
            ok = mb.load_sample()
            self.out("样本 0x%05X → 缓冲 0x%05X（%s）"
                     % (mb.sample, mb.buf, "已抄" if ok else "样本区不可用"))
            self.out("  " + " ".join("%08X" % w for w in mb.read_back()))
            return True
        if sub == "queue":
            q, tail = mb.queue()
            self.out("消息链：" + (" → ".join("0x%05X" % p for p in q) or "空")
                     + ("  （尾部 0x%05X）" % tail))
            return True
        self.out("用法：msg [send [W..]|sample|queue]")
        return True

    def cmd_cb(self, arg=""):
        """cb [tick [N]|chain [N]] —— 两个回调槽：替主循环扫一圈（V18）

        `cb`              看两个槽的指针 / 倒计时 / 标志位
        `cb tick [N]`     替主循环扫 N 圈（默认 1），打印真正执行了回调的那几圈
        `cb chain [N]`    一直扫到没有槽还装着指针（最多 N 圈，默认 240）

        ⚠️ V19 更正：0x80588 不是主循环，是 INT 0x2B 的处理函数（`irq2b` 才是
        把它接上的用法）。这里的 tick 是**手动替它转一圈**，等价于「外部注入一次
        分派」，不代表固件自己走过。
        """
        m = self.m
        a = arg.split()
        cb = getattr(m, "cb", None)
        if not a:
            if cb is None:
                self.out("没有回调槽：先 cb tick 1，或启动时加 --cb / --cb-tick N")
                return True
            self.out("0x80588（INT 0x2B 的处理函数）覆盖=%s"
                     % (m.cov.visited(0x80588) if getattr(m, "cov", None) else "?"))
            for st in cb.state():
                self.out("  %s 指针=0x%08X 倒计时=0x%04X 标志[0x%05X]=0x%02X%s"
                         % (st["name"], st["cb"], st["count"], st["flag_addr"],
                            st["flag"], "  *挂起*" if st["pending"] else ""))
            self.out("  已扫 %d 圈" % cb.ticks)
            return True
        if cb is None:
            cb = m.enable_callbacks()
        if getattr(m, "cov", None) is None:
            m.enable_cov()          # 没有覆盖率就看不出「哪一圈真的执行了回调」
        sub = a[0]
        if sub == "tick":
            n = self._num(a[1]) if len(a) > 1 else 1
            if not n:
                n = 1
            got = cb.tick(n)
            if not got:
                self.out("扫了 %d 圈：门控都没到点（倒计时还在减），没有回调被执行" % n)
            for t, name, r in got:
                self.out("  第 %-4d 圈 %s 执行 0x%05X：%d 步，覆盖 +%d%s → 下一站 0x%05X"
                         % (t, name, r["cb"], r["steps"], r["delta"],
                            "（没回停机桩：交权给调度器）" if r["cut"] else "",
                            r["next"]))
            self.out("  " + cb.summary())
            return True
        if sub == "chain":
            n = self._num(a[1]) if len(a) > 1 else 240
            if not n:
                n = 240
            ev = cb.chain(n)
            if not ev:
                self.out("链没动：没有槽挂着指针")
            for t, name, r in ev:
                self.out("  第 %-4d 圈 %s 0x%05X → 0x%05X（%d 步 / +%d）"
                         % (t, name, r["cb"], r["next"], r["steps"], r["delta"]))
            self.out("  " + cb.summary())
            return True
        self.out("用法：cb [tick [N]|chain [N]]")
        return True

    def cmd_irq2b(self, arg=""):
        """irq2b [N|answer N] —— 接上 INT 0x2B（V19；V20 改默认）

        `irq2b`         看这条线的接线情况：向量项、ICR、14 个待办标志字节
        `irq2b N`       请求 N 次中断，每次跑到 reti；有站推进就打印
        `irq2b answer N`同上，但在 0x9C97A 上硬写 0x174 的完成位（V19 的做法，对照用）

        V20：默认不再替外设答话 —— `0x174` 由 `XferHandshake` 建模
        （`D300_A_TABLE` 里 `0x174:(0x02,0x40)`），`tick()` 到点会自己置完成位。
        V19 那次「第 9 次中断卡死」是本模块绕过 `Machine.step()` 的时基分发造成的。

        与 `cb tick` 的区别：`cb tick` 是**我们手动替 0x80588 转一圈**；
        `irq2b` 是**把那根中断线接上**，让固件自己的分派器跑。V19 更正了 V18
        「0x80588 是没有调用者的主循环」这个说法 —— 它是 INT 0x2B 的处理函数。
        """
        m = self.m
        a = arg.split()
        h = getattr(m, "isr2b", None)
        if getattr(m, "cov", None) is None:
            m.enable_cov()
        if h is None:
            h = m.enable_isr2b()
        if not a:
            st = h.state()
            self.out("INT 0x2B → 0x%05X（向量表 0x%05X 第 0xD4 项）  ICR[0x45B]=0x%02X"
                     % (st["entry"], 0x000FFC00, st["icr"]))
            self.out("  处理函数范围 0x80588–0x80968，收尾 reti；覆盖=%s"
                     % m.cov.visited(0x80588))
            fl = " ".join("0x%05X=%02X" % (k, v) for k, v in st["flags"].items() if v)
            self.out("  待办标志 0x3F402–0x3F40F：%s" % (fl or "（全 0）"))
            self.out("  已请求 %d 次 / 硬写完成位 %d 次（默认 0：由设备模型给）"
                     % (st["count"], st["answers"]))
            if st["cb"]:
                self.out("  " + st["cb"])
            return True
        answer = False                      # V20：默认靠 XferHandshake 自己答话
        if a and a[0] == "answer":
            answer = True                   # 对照：像 V19 那样硬写 0x174 bit6
            a = a[1:]
        n = self._num(a[0]) if a else 1
        if not n:
            n = 1
        answered = [0]

        def on_ev(r):
            self.out("  第%4d 次：%6d 步  覆盖+%d 服务+%d%s  %s"
                     % (r["n"], r["steps"], r["delta"], r["svc"],
                        "  （没回到 reti）" if r["cut"] else "", r["cb"]))

        h.burst(n, answer=answer, on_event=on_ev)
        self.out("  " + h.info())
        return True

    def cmd_readyq(self, arg=""):
        """readyq —— 就绪位图 + 13 个优先级槽的队列（V15；空槽是 head=tail=槽自己）"""
        from . import realos as R
        m = self.m
        bm = int.from_bytes(m.fetch_bytes(0x45984, 4), "big")
        cur = int.from_bytes(m.fetch_bytes(0x45980, 4), "big")
        tcb2id = {t["tcb"]: t["id"] for t in R.states(m) if t["tcb"]}
        self.out("就绪位图 0x45984 = 0x%08X → 优先级 %s；当前 TCB 0x%06X（任务 %s）"
                 % (bm, [31 - i for i in range(32) if bm >> i & 1] or "无",
                    cur, tcb2id.get(cur, "?")))
        for p in range(13):
            head = int.from_bytes(m.fetch_bytes(0x45988 + p * 8, 4), "big")
            tail = int.from_bytes(m.fetch_bytes(0x45988 + p * 8 + 4, 4), "big")
            empty = head == tail == 0x45988 + p * 8
            mark = "（空）" if empty else ""
            self.out("  槽 %-2d head=0x%06X(任务%s) tail=0x%06X(任务%s) %s"
                     % (p, head, tcb2id.get(head, "?"), tail, tcb2id.get(tail, "?"), mark))
        return True

    def cmd_boot(self, arg=""):
        """boot [TASK,TASK…|dormant|all] / boot off TASK… —— 改「复位就激活」位（V16）

        改的是任务表记录 `+0x0C` 的最高位。改完要 `reset` 再走 REALOS 才生效。
        """
        from . import taskboot
        m = self.m
        a = arg.replace(",", " ").split()
        if not a:
            act = [r["id"] for r in taskboot.records(m) if r["state"] & taskboot.ACTIVE_BIT]
            dor = taskboot.dormant(m)
            self.out("复位就激活：%s" % (", ".join(str(t) for t in act) or "无"))
            self.out("休眠（开机不建）：%s" % (", ".join(str(t) for t in dor) or "无"))
            self.out("用法：boot 2,3,6 / boot dormant / boot all / boot off 2,3")
            return True
        off = a[0] in ("off", "0", "-")
        pool = a[1:] if off else a
        if not pool:
            self.out("boot off 后面要跟任务号")
            return True
        if len(pool) == 1 and pool[0] in ("dormant", "all"):
            done = taskboot.activate(m, pool[0], on=not off)
        else:
            done = taskboot.activate(m, [self._num(x) for x in pool], on=not off)
        self.out("已改 %s：%s" % ("关" if off else "开",
                                 ", ".join("任务%d" % t for t in done) or "（没有变化）"))
        self.out("⚠️ 这是改配置做的实验：要 reset 之后再走 REALOS 才生效")
        return True

    def cmd_unlock(self, arg=""):
        """unlock TASK [ROUNDS] —— 一站一站把任务往下推（V16）"""
        from . import unlock
        a = arg.split()
        if not a:
            self.out("用法：unlock TASK [ROUNDS]   —— 跑一段→看它等哪个 flag 的哪几位→置位→再跑")
            return True
        tid = self._num(a[0])
        rounds = self._num(a[1]) if len(a) > 1 else 12
        cov = getattr(self.m, "cov", None) or self.m.enable_cov()
        self.m.attach_flow()
        st = unlock.trace(self.m, tid, rounds=rounds, cov=cov)
        unlock.print_trace(st, out=self.out)
        return True

    def cmd_cov(self, arg=""):
        """cov [MINLEN] —— 覆盖率：跑了多少 / 还差哪些从没执行过的代码段（V15）"""
        m = self.m
        cov = getattr(m, "cov", None)
        a = arg.split()
        if cov is None:
            cov = m.enable_cov()
            self.out("覆盖率记录已开（从现在开始记）")
        minlen = self._num(a[0]) if a else 64
        s = cov.summary()
        self.out("覆盖：%d 个指令地址被执行过（ROM 共 %d 个 2B 槽，占 %.1f%%）"
                 % (s["已执行指令地址"], s["ROM 字数"], s["占比"]))
        holes = cov.holes(min_len=minlen)[:12]
        self.out("从没执行过的区间（≥%dB，共 %d 段）：" % (minlen, len(cov.clusters(min_len=minlen))))
        for a0, a1 in holes:
            self.out("  0x%06X–0x%06X  %6d B" % (a0, a1, a1 - a0))
        if len(holes) == 0:
            self.out("  （没有：全 ROM 都被踩过了）")
        elif len(holes) < len(cov.clusters(min_len=minlen)):
            self.out("  ……只列最长的 12 段")
        return True

    def cmd_flow(self, arg=""):
        """flow [N] —— 最近 N 条服务调用流水（被动记录，需先跑过一段）"""
        m = self.m
        fl = getattr(m, "flow", None)
        if fl is None:
            self.out("还没挂流水记录器：先 realos 1 再 cont，或 --flow 启动")
            return True
        n = self._num(arg) or 24
        self.out("服务调用 %d 次，当前 TCB 换过 %d 次"
                 % (len(fl.calls), len(fl.switches)))
        self.out("  切换时间线：" + " → ".join("0x%05X@%d" % (t, s)
                                              for s, t in fl.switches[-12:]))
        for line in fl.dump(limit=n).splitlines():
            self.out(line)
        return True

    def cmd_realos(self, arg=""):
        """realos [TASKID] —— 固件自带的 REALOS：看 13 条记录 / 让内核调度某个任务"""
        from . import realos as R
        m = self.m
        a = arg.split()
        if a and a[0] not in ("list", "ls", "info"):
            tid = self._num(a[0])
            if tid is None:
                self.out("用法：realos [TASKID]   例如 realos 1")
                return True
            try:
                info = m.boot_realos(tid)
            except KeyError:
                ids = ", ".join(str(r["id"]) for r in R.records(m))
                self.out(f"任务表里没有 id={tid}；可用：{ids}")
                return True
            rec, ctx = info["record"], info["context"]
            self.out(f"原生 REALOS 已就位：{info['steps']:,} 步跑到派发点 0x{R.DISPATCH_LD:06X}")
            self.out(f"  派发任务 {tid}：TCB=0x{rec['tcb']:06X} 入口=0x{rec['entry']:06X} "
                     f"栈顶=0x{rec['sp']:06X}")
            self.out(f"  上下文 PC=0x{ctx['pc']:06X} PS=0x{ctx['ps']:08X}"
                     f"{'（还没建）' if not ctx['pc'] else ''}")
            self.out(f"  *(0x{R.CUR_TCB:06X}) 0x{info['before']:06X} → 0x{rec['tcb']:06X}"
                     f"{'  改派生效' if info['switched'] else '  本来就是它'}")
            self.out("  接着 cont N 跑；int #0x40 从这里开始由固件内核接管，不要再挂 --kernel")
            return True
        # ---- 列表：13 条记录 + 当前任务 + 各任务上下文
        n_user = int.from_bytes(m.fetch_bytes(R.TASK_TABLE, 4), "big")
        if not (0 < n_user <= 64):
            self.out(f"*(0x{R.TASK_TABLE:06X}) = 0x{n_user:08X}，不像 REALOS 任务表头 —— "
                     "这张表只在 A 段（IO 协处理器固件）里")
            return True
        rows = R.records(m)
        if not rows:
            self.out("读不到任务表（机器还没 load？）")
            return True
        cur = int.from_bytes(m.fetch_bytes(R.CUR_TCB, 4), "big")
        idle = R.find(m, 0)
        self.out(f"REALOS 任务表 @0x{R.TASK_TABLE:06X}：头部记 {n_user} 个用户任务，"
                 f"实际 {len(rows)} 条记录 × 0x{R.TASK_REC:X} 字节")
        self.out(f"  当前 TCB = 0x{cur:06X}"
                 f"{'（空闲任务）' if idle and cur == idle['tcb'] else ''}"
                 f"{'（内核还没写回，cont 之后才有）' if not cur else ''}")
        self.out("  id  属性  TCB      入口       栈顶       状态     上下文 PC")
        for r in rows:
            c = R.saved_context(m, r["tcb"]) if r["tcb"] else {"pc": 0}
            tag = " ◀ 当前" if r["tcb"] == cur else ""
            self.out("  %-3d 0x%02X  0x%06X 0x%06X  0x%06X  %-8s 0x%06X%s"
                     % (r["id"], r["attr"], r["tcb"], r["entry"], r["sp"],
                        "已激活" if r["active"] else "未启动", c["pc"], tag))
        built = [r["id"] for r in rows if r["tcb"] and R.saved_context(m, r["tcb"])["pc"]]
        self.out(f"  已建上下文的：{('、'.join('任务 %d' % i for i in built)) or '无'}"
                 f"（跑 realos 1 之后内核会先建任务 1 / 5 / 0）")
        self.out(f"  启动阶段标记 *(0x003FBB0) = "
                 f"{int.from_bytes(m.fetch_bytes(0x3FBB0, 4), 'big')}"
                 f"（2 = 已进调度循环）")
        return True

    def cmd_flag(self, arg=""):
        m = self.m
        if m.kernel is None:
            m.attach_kernel()
        a = arg.split()
        if not a or a[0] in ("list", "ls"):
            for fid, pat in m.kernel.flag_table():
                if pat:
                    self.out("  flag %-3d = 0x%08X" % (fid, pat))
            if not any(p for _, p in m.kernel.flag_table()):
                self.out("  （17 个事件标志全为 0）")
            return True
        if a[0] in ("set", "raise") and len(a) > 1:
            spec = a[1]
            if "=" in spec:
                k, v = spec.split("=", 1)
                fid, bits = self._num(k), self._num(v)
            else:
                fid, bits = self._num(spec), 0xFFFFFFFF
            v = m.flag_set(fid, bits)
            self.out("flag %d 置 0x%X → 现在 0x%08X" % (fid, bits, v))
            return True
        if a[0] in ("clr", "clear") and len(a) > 1:
            fid = self._num(a[1])
            bits = self._num(a[2]) if len(a) > 2 else None
            v = m.flag_clear(fid, bits)
            self.out("flag %d 清 → 现在 0x%08X" % (fid, v))
            return True
        self.out("用法：flag [list | set ID=BITS | clr ID [BITS]]")
        return True

    def cmd_resume(self, arg=""):
        m = self.m
        if m.blocked is None:
            self.out("当前没有阻塞的任务")
            return True
        rec = m.resume()
        self.out("从 0x%06X 的 %s 重新执行" % (rec["pc"], rec["svc"]))
        limit = self._num(arg) if arg else 200000
        n = m.run(max_steps=limit)
        self.out("跑了 %s 步，PC=0x%08X" % (format(n, ","), m.cpu.pc))
        if m.blocked is not None:
            from .itron import ItronKernel
            self.out("仍然阻塞：" + ItronKernel.describe(m.blocked))
        return True

    def cmd_audit(self, arg=""):
        self.out(self.m.bus.audit())
        return True

    def cmd_dev(self, arg=""):
        for d in self.m.devices:
            self.out("  " + d.info())
        return True

    def cmd_dump(self, arg=""):
        st = self.m.stats()
        for k, v in st.items():
            self.out(f"  {k:<22}{v}")
        return True

    # ------------------------------------------------------------ 快照
    def cmd_savevm(self, arg=""):
        from . import snapshot
        if not arg:
            self.out("用法：savevm PATH（例如 savevm a-43k.snap）")
            return True
        try:
            n = snapshot.save(self.m, arg)
        except OSError as e:
            self.out(f"保存失败：{e}")
            return True
        self.snap_path = arg
        self.out(f"已保存快照 {arg}（{n:,} 字节，{self.m.step_count:,} 步，"
                 f"PC=0x{self.m.cpu.pc:08X}）")
        return True

    def cmd_loadvm(self, arg=""):
        from . import snapshot
        path = arg or getattr(self, "snap_path", "")
        if not path:
            self.out("用法：loadvm PATH")
            return True
        try:
            meta = snapshot.load(self.m, path)
        except (OSError, ValueError) as e:
            self.out(f"读取失败：{e}")
            return True
        self.out("已恢复：" + snapshot.describe(meta))
        return True

    def cmd_info_snap(self, arg=""):
        self.out(f"上一次快照：{getattr(self, 'snap_path', '（无）')}")
        return True

    def cmd_machines(self, arg=""):
        for n, c in sorted(MACHINES.items()):
            self.out(f"  {n:<8} {c.desc}")
        return True

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _num(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            return int(s, 0)
        except ValueError:
            try:
                return int(s, 16)
            except ValueError:
                return None
