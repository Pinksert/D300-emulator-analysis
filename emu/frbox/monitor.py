# -*- coding: utf-8 -*-
"""HMP 式命令行（QEMU monitor 的简化版）。

命令刻意对齐 QEMU 的肌肉记忆：
    info registers / info mtree / info devices / info irq
    x/16x 0x40000      x/8i 0x000FC000
    step [N]           cont [MAX]         bp 0x40000        bps / bpc ADDR
    reset              load [PATH]        irq raise 0x1B    irq rx 41 42
    savevm PATH        loadvm PATH         info snap
    boottask ENTRY[:SP[:ARG[:STOPPC]]] [rp=ADDR] [table]
                                            手工启动任务（跳过 REALOS 调度器）
    autospin [on|off|reset|status]          自旋消除器（等硬件就绪位）
    svc [N]            服务调用直方图（μITRON 半主机）
    flag [list|set ID=BITS|clr ID]          事件标志（注入外部事件）
    resume             从被阻塞的 int 指令继续
    tasks               REALOS 任务表（入口 / 栈顶 / 是否激活）
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
        m = self.m
        rows = m.task_table()
        if not rows:
            self.out("没有任务表")
            return True
        self.out("REALOS 任务表 @0x%06X（每条 0x%X 字节，共 %d 条）"
                 % (m.TASK_REC0, m.TASK_REC, len(rows)))
        self.out("  #   TCB     任务号  状态        入口       栈顶       是否已启动")
        for r in rows:
            self.out("  %-3d %06X   %-5d  0x%08X  0x%06X   0x%06X   %s"
                     % (r["index"], r["tcb"], r["id"], r["state"],
                        r["entry"], r["sp"], "已激活" if r["active"] else "-"))
        self.out("  想看上下文 / 让内核原生调度：realos（不带参数列表，realos 1 启动任务 1）")
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
            c = R.context(m, r["sp"]) if r["sp"] else {"pc": 0}
            tag = " ◀ 当前" if r["tcb"] == cur else ""
            self.out("  %-3d 0x%02X  0x%06X 0x%06X  0x%06X  %-8s 0x%06X%s"
                     % (r["id"], r["attr"], r["tcb"], r["entry"], r["sp"],
                        "已激活" if r["active"] else "未启动", c["pc"], tag))
        built = [r["id"] for r in rows if r["sp"] and R.context(m, r["sp"])["pc"]]
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
