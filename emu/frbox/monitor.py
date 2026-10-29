# -*- coding: utf-8 -*-
"""HMP 式命令行（QEMU monitor 的简化版）。

命令刻意对齐 QEMU 的肌肉记忆：
    info registers / info mtree / info devices / info irq
    x/16x 0x40000      x/8i 0x000FC000
    step [N]           cont [MAX]         bp 0x40000        bps / bpc ADDR
    reset              load [PATH]        irq raise 0x1B    irq rx 41 42
    savevm PATH        loadvm PATH         info snap
    boottask ENTRY[:SP[:ARG[:STOPPC]]]      手工启动任务（跳过 REALOS 调度器）
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
            self.out(f"    已受理中断 {len(m.cpu.irq_log)} 次，待决 "
                     f"{len(m.cpu.irq_pending)} 条")
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
        """boottask 0x8133A:0x4616C:0:0x80506 —— 手工启动任务"""
        spec = arg or rest
        if not spec:
            self.out("用法：boottask ENTRY[:SP[:ARG[:STOPPC]]]\n"
                     "  A 段任务 1：boottask 0x8133A:0x4616C:0:0x80506")
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
        e, s, aa = self.m.boot_task(entry, sp, a, stop_pc=stop)
        self.out(f"已启动任务：入口 0x{e:06X}  SP=0x{s:08X}  r4=0x{aa:X}"
                 f"{'  （先跑到 0x%06X）' % stop if stop else ''}")
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
