# -*- coding: utf-8 -*-
"""自旋消除器：把「等硬件就绪位」的忙等用运行期寄存器实值解出来。

D300 A 段的启动链上有三处同构忙等：

    ldi:20 #0x0AF, r12      ; 目标寄存器地址
    borl   #2, @r12         ; 置 bit2 = 启动
    ...
    btsth  #7, @r12         ; 等 bit7 = 就绪        ← 卡住这里
    beq    -4               ; 没就绪就回去

前两处（0x0DCD2C 等 0x0AF bit7、0x09C97C 等 0x174 bit6）地址是 `ldi:20` 直接写死的，
静态扫一遍就能认出来。第三处（0x0CE136）的地址是算出来的：

    0x0CE132  borl  #2, r8     ; r8 = 基址 | 2，运行时才知道
    0x0CE134  btsth #4, @r8    ; 等 bit6
    0x0CE136  beq   -2

静态扫拿不到 r8，所以本模块改成「运行期解密」：

  1. machine.run() 发现某个 0x100 窗口里连续走了 threshold 步没离开；
  2. 再单步 probe 步，把这批 PC 收下来（它们就是循环体）；
  3. 循环体里找 btsth/btstl，读它的 u4 立即数得到掩码，读它用的那个寄存器
     在**当前**时刻的值 —— 这就是要等的寄存器地址；
  4. 校验该地址的掩码位确实是 0（不 0 说明不是卡点，换下一个候选）；
  5. 把这一位置 1：< 0x1000 走直接页影子（硬件寄存器），否则直接写内存。

记录下来的每一条都进 `events`，report 里可以照抄。
"""


class SpinBreaker:
    """运行期破解「等就绪位」自旋。"""

    def __init__(self, machine, threshold=3000, probe=96,
                 max_per_site=8, max_total=200):
        self.m = machine
        self.threshold = threshold
        self.probe = probe
        self.max_per_site = max_per_site
        self.max_total = max_total
        self.events = []                 # 每次注入一条记录
        self.per_site = {}               # poll_pc -> 注入次数
        self.give_up = set()             # 到上限的 poll_pc
        self.hits = 0

    # ------------------------------------------------------------------ 解码
    def _insn(self, pc):
        """返回 (助记符, 操作数字典) 或 None。"""
        m = self.m
        try:
            data = m.fetch_bytes(pc, 6)
        except Exception:
            return None
        if not data:
            return None
        from fr_emu import TABLE
        ins = TABLE.decode(data, 0)
        if ins is None:
            return None
        mn, _op, vals, _size, _w = ins
        return mn, vals

    def _candidates(self, pcs):
        """从循环体的 PC 里挑出所有 btsth/btstl，按出现次数排序。"""
        cnt = {}
        for p in pcs:
            cnt[p] = cnt.get(p, 0) + 1
        out = []
        for p, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])):
            ins = self._insn(p)
            if ins is None:
                continue
            mn, vals = ins
            if mn not in ("btsth", "btstl"):
                continue
            u4 = vals.get("u4")
            if not isinstance(u4, int) or not u4:
                continue
            reg = None
            for key in ("ri", "rj"):
                v = vals.get(key)
                if isinstance(v, int):
                    reg = v
                    break
            if reg is None:
                continue
            mask = (u4 << 4) if mn == "btsth" else u4
            out.append((p, reg, mask, n))
        return out

    # -------------------------------------------------------------- 注入一次
    def break_spin(self, pc0):
        """在自旋点尝试消掉一个就绪位；成功返回 True。"""
        m = self.m
        c = m.cpu
        before = m.step_count
        loop = []
        for _ in range(self.probe):
            loop.append(c.pc)
            m.step(1)
        for poll_pc, reg, mask, _n in self._candidates(loop):
            addr = c.r[reg] & 0xFFFFFFFF
            if addr < 0x1000:
                old = m.dp.shadow.get(addr, 0) & 0xFF
            else:
                old = m.bus.rd(addr, 1) & 0xFF
            if old & mask:              # 这一位本来就是 1，不是卡点
                continue
            n = self.per_site.get(poll_pc, 0) + 1
            self.per_site[poll_pc] = n
            if n > self.max_per_site:
                self.give_up.add(poll_pc)
                return False
            if addr < 0x1000:
                m.dp.shadow[addr] = old | mask
                where = "直接页"
            else:
                m.bus.wr(addr, 1, old | mask, pc=poll_pc)
                where = "内存"
            self.events.append({
                "spin_pc": pc0, "poll_pc": poll_pc, "reg": reg,
                "addr": addr, "mask": mask, "old": old,
                "nth": n, "where": where,
                "step": m.step_count, "cost": m.step_count - before,
            })
            self.hits += 1
            return True
        return False

    # ---------------------------------------------------------------- 报告
    def reset(self):
        self.events = []
        self.per_site = {}
        self.give_up = set()
        self.hits = 0

    def rows(self):
        return list(self.events)

    def summary(self):
        if not self.events:
            return "自旋消除器：没有触发"
        sites = len({e["poll_pc"] for e in self.events})
        kinds = len({(e["addr"], e["mask"]) for e in self.events})
        return (f"自旋消除器：{sites} 处轮询 / {kinds} 个不同就绪位 / "
                f"共注入 {self.hits} 次")
