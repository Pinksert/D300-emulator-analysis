# -*- coding: utf-8 -*-
"""日志分类（对应 QEMU 的 -d / -D）。

类别：
    in_asm   每执行一条就打印反汇编（最啰嗦）
    exec     每 N 步打印一次进度
    int      中断/异常进入与返回
    dev      外设寄存器访问
    unimp    未实现指令 / 无法解码
    svc      μITRON 服务调用
    guest    guest 内部错误（未映射访问等）
"""
import sys


class Trace:
    ALL = ("in_asm", "exec", "int", "dev", "unimp", "svc", "guest")

    def __init__(self, cats=(), out=None, prefix=""):
        self.cats = set()
        for c in cats:
            self.enable(c)
        self.out = out if out is not None else sys.stdout
        self.prefix = prefix
        self.counts = {}

    def enable(self, name):
        name = name.strip()
        if not name:
            return
        if name == "all":
            self.cats.update(self.ALL)
            return
        if name not in self.ALL:
            raise ValueError(f"未知日志类别 {name}，可选：{', '.join(self.ALL)}")
        self.cats.add(name)

    def has(self, cat):
        return cat in self.cats

    def log(self, cat, msg):
        if cat not in self.cats:
            return
        self.counts[cat] = self.counts.get(cat, 0) + 1
        self.out.write(f"{self.prefix}[{cat}] {msg}\n")

    def summary(self):
        if not self.counts:
            return ""
        return "日志条数：" + "，".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
