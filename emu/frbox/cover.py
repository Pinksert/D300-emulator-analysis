# -*- coding: utf-8 -*-
"""覆盖率（V15）—— 「固件到底有多少代码被我们跑到过」

做法极简：CPU 每执行一条指令，把 `(pc - rom_base) >> 1` 在 bytearray 里置 1。
3.75 MB 的 A 段只要 1.8 MB 内存，一条指令的代价是「两次比较 + 一次字节写」。

用途不是刷百分比（ROM 里混着字符串/表格，分母没意义），而是**反向找洞**：
哪些 ≥64 字节的连续区间从头到尾一条指令都没执行过 —— 那些就是「还缺哪块硬件」的清单。
`clusters()` 给区间，`diff()` 给「只有某个场景才踩到 / 从这个场景开始才踩到」的区间，
配合 `probe/exp_coverage.py` 里那句反汇编，一眼就能判是代码还是数据。
"""

__all__ = ["Coverage", "attach"]


def _runs(n, pred, base, min_len, gap):
    """把 pred(i) 为真的下标流并成 [start, end) 字节区间。"""
    out = []
    start = last = None
    for i in range(n):
        if not pred(i):
            continue
        a = base + (i << 1)
        if start is None:
            start = a
        elif a - last > gap:
            if last + 2 - start >= min_len:
                out.append((start, last + 2))
            start = a
        last = a
    if start is not None and last + 2 - start >= min_len:
        out.append((start, last + 2))
    return out


class Coverage:
    def __init__(self, m, label=""):
        b = m.bus
        self.m = m
        self.label = label
        self.base = b.rom_base
        self.end = b.rom_end
        self.n = max(0, (self.end - self.base) >> 1)
        self.buf = bytearray(self.n)
        self.steps = 0                   # 统计用：跑过多少步

    # ---------------- 绑定 ----------------
    def bind(self, cpu=None):
        c = cpu or self.m.cpu
        c.cov_buf = self.buf
        c.cov_base = self.base
        c.cov_end = self.end
        return self

    def detach(self):
        self.m.cpu.cov_buf = None
        return self

    def reset(self):
        self.buf[:] = bytes(self.n)
        self.bind()
        return self

    # ---------------- 查询 ----------------
    def visited(self, addr):
        i = (addr - self.base) >> 1
        return 0 <= i < self.n and self.buf[i] != 0

    def words(self):
        return self.buf.count(1)                  # C 层扫一遍，1.8 MB 也很快

    def percent(self):
        return (100.0 * self.words() / self.n) if self.n else 0.0

    def clusters(self, min_len=64, gap=16):
        """从没执行过的区间（按字节，end 不含），≥ min_len，间隔 < gap 的并成一段。"""
        return _runs(self.n, lambda i: not self.buf[i], self.base, min_len, gap)

    def holes(self, min_len=64, gap=16):
        """按长度倒序的未执行区间。"""
        return sorted(self.clusters(min_len, gap), key=lambda t: t[1] - t[0], reverse=True)

    def diff(self, other, min_len=16, gap=16):
        """`self` 踩到、`other` 没踩到的区间 —— 「这段是谁解锁的」。"""
        n = min(self.n, other.n)
        return _runs(n, lambda i: self.buf[i] and not other.buf[i],
                     self.base, min_len, gap)

    def summary(self):
        return {
            "label": self.label,
            "已执行指令地址": self.words(),
            "ROM 字数": self.n,
            "占比": round(self.percent(), 2),
            "未跑区间(≥64B)": len(self.clusters()),
        }


def attach(m, label=""):
    m.cov = Coverage(m, label)
    m.cov.bind()
    return m.cov
