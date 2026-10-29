# -*- coding: utf-8 -*-
"""指令覆盖率审计：把 65536 个可能的 16 位字逐个喂给解释器执行一次，
统计「16 位 / 32 位首字 / 48 位首字 / 未定义」与「能执行 / 抛未实现」。
这是模拟器完整度的客观分数，不靠感觉。"""
import sys, collections
import os
sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
_EMU = os.path.dirname(_HERE) if os.path.basename(_HERE) == "probe" else _HERE
_ROOT = os.path.dirname(_EMU)
sys.path.insert(0, _EMU)
FW_DIR = os.environ.get("FRBOX_FW_DIR") or os.path.join(_ROOT, "data")

from fr_emu import Fr, Mem, TABLE, Unimpl

BASE = 0x00080000
kind = collections.Counter()
unimpl = collections.Counter()
bad = collections.Counter()
mnem_ok = set()

PAD = b"\x00" * 10                       # 让 4/6 字节指令也能解出来

for w in range(0x10000):
    data = w.to_bytes(2, "big") + PAD
    ins = TABLE.decode(data, 0)
    if ins is None:
        kind["未定义"] += 1
        continue
    kind[{2: "16 位", 4: "32 位首字", 6: "48 位首字"}[ins[3]]] += 1
    m = Mem()
    m.load_rom(BASE, data)
    cpu = Fr(m, TABLE)
    cpu.pc = BASE
    cpu.r = [0x00041000 + 0x40 * i for i in range(15)] + [0x00048000]
    for i in range(6):
        cpu.dr[i] = 0x00030000
    try:
        cpu.step()
        mnem_ok.add(ins[0])
    except Unimpl as e:
        unimpl[str(e.args[0])] += 1
    except Exception as e:
        bad[type(e).__name__ + ":" + str(e)[:30]] += 1

total = sum(kind.values())
decodable = total - kind["未定义"]
print("16 位编码空间 65536 个字：")
for k in ("16 位", "32 位首字", "48 位首字", "未定义"):
    print(f"  {k:<10}{kind[k]:6d}")
ok = decodable - sum(unimpl.values()) - sum(bad.values())
print(f"\n  可执行的编码      {ok:6d}  ({ok / decodable * 100:.2f}% / 可解码 {decodable})")
print(f"  未实现指令      {sum(unimpl.values()):6d}")
print(f"  其它异常        {sum(bad.values()):6d}")
print(f"  覆盖助记符      {len(mnem_ok)} 种")
if unimpl:
    print("\n未实现清单：")
    for k, v in unimpl.most_common():
        print(f"  {k:<10} {v}")
if bad:
    print("\n异常明细：")
    for k, v in bad.most_common(10):
        print(f"  {k:<40} {v}")
