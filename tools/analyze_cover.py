# -*- coding: utf-8 -*-
"""B 段覆盖率 + 静态调用图前沿分析。
用法: python analyze_cover.py <asm> <cov.bin> [mode]
"""
import io, os, re, sys, bisect
from collections import defaultdict

ASM = sys.argv[1] if len(sys.argv) > 1 else None
COV = sys.argv[2] if len(sys.argv) > 2 else None
CODE_LO = 0x00040000

ROOT = r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp\nft\Java\Emulator"
if ASM is None: ASM = os.path.join(ROOT, "d300_b_full.asm")
if COV is None: COV = os.path.join(ROOT, "cover0.bin")

# ---------- 1. 解析反汇编 ----------
pat = re.compile(r'^([0-9A-F]{8})\s+([0-9A-F]{4}(?: [0-9A-F]{4})*)\s+(.*)$')
addr_list = []      # 有序地址
text = {}           # addr -> 指令文本
for line in io.open(ASM, encoding='utf-8', errors='replace'):
    line = line.rstrip('\n').rstrip('\r')
    m = pat.match(line)
    if not m:
        continue
    a = int(m.group(1), 16)
    addr_list.append(a)
    text[a] = m.group(3).strip()

addr_list.sort()
print("指令条数 %d   范围 0x%06X - 0x%06X" % (len(addr_list), addr_list[0], addr_list[-1]))

# ---------- 2. 覆盖率位图 ----------
cov = io.open(COV, 'rb').read()
print("位图字节 %d (覆盖 0x%X 字节代码)" % (len(cov), len(cov) * 2))

def ran(a):
    k = (a - CODE_LO) >> 1
    return 0 <= k < len(cov) and cov[k] != 0

n_exec = sum(1 for a in addr_list if ran(a))
print("执行过的指令槽 %d / %d  (%.3f%%)" % (n_exec, len(addr_list), 100.0 * n_exec / len(addr_list)))

# ---------- 3. 函数调用边 ----------
call_direct = re.compile(r'^CALL\s+0x([0-9A-F]{8})$')
call_indirect = re.compile(r'^CALL\s+@R\d+\s*;\s*0x([0-9A-F]{8})$')
jmp_indirect = re.compile(r'^JMP(?::D)?\s+@R\d+\s*;\s*0x([0-9A-F]{8})$')
call_any = re.compile(r'^CALL')

edges = []          # (src, dst, kind)
for a in addr_list:
    t = text[a]
    if not t.startswith('CALL'):
        continue
    m = call_direct.match(t) or call_indirect.match(t)
    if m:
        edges.append((a, int(m.group(1), 16), 'call'))
    else:
        edges.append((a, None, 'indirect-unknown'))

targets = set(d for _, d, _ in edges if d is not None)
print("静态调用边 %d 条，其中解析出目标 %d 条；不同被调用地址 %d 个"
      % (len(edges), sum(1 for e in edges if e[1] is not None), len(targets)))

# ---------- 4. 函数入口 ----------
ret_like = ('RET', 'RET:D', 'RETI', 'JMP', 'JMP:D')
entries = set(targets)
entries.add(addr_list[0])
for i in range(1, len(addr_list)):
    prev = addr_list[i - 1]
    tp = text[prev]
    if tp.split()[0] in ret_like:
        entries.add(addr_list[i])
# 任务表 28 条（入口在第 0 个 u32）
TASK_TBL = 0x54C10 - 0x40000
firm = io.open(os.path.join(ROOT, "B3000111.bin"), 'rb').read()
import struct
for i in range(28):
    o = TASK_TBL + i * 0x1C
    e = struct.unpack('>I', firm[o:o + 4])[0]
    if 0x40000 <= e < 0x740000:
        entries.add(e)

entries = sorted(a for a in entries if a in text)
print("函数入口候选 %d 个" % len(entries))

def func_of(a):
    i = bisect.bisect_right(entries, a) - 1
    return entries[i] if i >= 0 else None

# ---------- 5. 每个函数执行了多少条指令 ----------
fcount = defaultdict(int)
for a in addr_list:
    if ran(a):
        f = func_of(a)
        if f is not None:
            fcount[f] += 1

executed_funcs = sorted(fcount.items(), key=lambda kv: -kv[1])
print("执行过的函数 %d 个" % len(executed_funcs))
print()
print("--- 执行最多的前 30 个函数 ---")
for f, c in executed_funcs[:30]:
    print("  0x%06X  %5d 条" % (f, c))

# ---------- 6. 调用图：正向边 ----------
callees = defaultdict(set)     # func -> set(func)
callers = defaultdict(set)
site_of = defaultdict(list)    # (srcfunc,dstfunc) -> [call site addr]
for src, dst, kind in edges:
    if dst is None:
        continue
    fs, fd = func_of(src), func_of(dst)
    if fs is None or fd is None:
        continue
    if fs == fd:
        continue
    callees[fs].add(fd)
    callers[fd].add(fs)
    site_of[(fs, fd)].append(src)

print()
print("--- 前沿：跑过的函数里，哪些调用点从未进入（按调用者执行量排序，前 40）---")
rows = []
for f, c in executed_funcs:
    dead = [(d, site_of[(f, d)][0]) for d in callees[f] if fcount.get(d, 0) == 0]
    if dead:
        rows.append((c, f, dead))
rows.sort(key=lambda r: -r[0])
for c, f, dead in rows[:40]:
    ds = ", ".join("0x%06X@0x%06X" % (d, s) for d, s in sorted(dead)[:4])
    more = "" if len(dead) <= 4 else " …(+%d)" % (len(dead) - 4)
    print("  调用者 0x%06X (执行 %4d 条) -> 从未进入: %s%s" % (f, c, ds, more))

# ---------- 7. 专项：谁会产生 set_flg(3,0x10) ----------
print()
print("--- bit4 生产者：沿调用链往上找第一个跑过的祖先 ---")
PROD = [0x0012F794, 0x0012F964, 0x0012FB10, 0x0012FDB0]
seen = set()
for p in PROD:
    f = func_of(p)
    print()
    print("set_flg(3,0x10) @ 0x%06X  ->  函数 0x%06X (执行 %d 条)" % (p, f, fcount.get(f, 0)))
    # 广度优先往上找，最多 4 层
    frontier = [(f, 0, [f])]
    found = []
    while frontier and len(found) < 6:
        nf = []
        for cur, depth, path in frontier:
            if depth >= 4:
                continue
            for up in sorted(callers.get(cur, ())):
                np = path + [up]
                if fcount.get(up, 0) > 0:
                    found.append((depth + 1, np))
                else:
                    nf.append((up, depth + 1, np))
        frontier = nf
        if found:
            break
    if found:
        for d, path in found[:6]:
            chain = " <- ".join("0x%06X(%d)" % (x, fcount.get(x, 0)) for x in path)
            print("    第 %d 层出现跑过的祖先: %s" % (d, chain))
    else:
        print("    4 层以内没有跑过的祖先（调用者整条链都没执行）")
        # 打印直接调用者
        ups = sorted(callers.get(f, ()))
        if ups:
            print("    直接调用者: " + ", ".join("0x%06X(%d)" % (u, fcount.get(u, 0)) for u in ups[:10]))
        else:
            print("    没有静态调用者（可能通过函数指针/中断向量进入）")

# ---------- 8. 每 4KB 页覆盖（按执行条数排序）----------
print()
print("--- 有执行的 4KB 页（按条数排序，前 40）---")
pages = defaultdict(int)
for a in addr_list:
    if ran(a):
        pages[a & ~0xFFF] += 1
for p, c in sorted(pages.items(), key=lambda kv: -kv[1])[:40]:
    print("  0x%06X  %5d 条" % (p, c))
print("  有执行的页数 %d" % len(pages))
