# -*- coding: utf-8 -*-
"""FR 解释器自检：用真实指令序列验证「算出来的结果」对不对。

不依赖反汇编器猜，而是：
 1) 在指令表里穷举 65536 个 16 位字，找出目标助记符+操作数的编码（编码由 opc.c 决定）
 2) 把编好的指令写进 ROM，让 CPU 真执行
 3) 检查寄存器/内存/标志位的期望值

覆盖：有符号除法全序列（DIV0S+32×DIV1+DIV2+DIV3+DIV4S）、加减法标志位、
      LSL/LSR/ASR、@(disp,R14) 负偏移、XCHB、LDI:8 无符号、PS 位域布局。
"""
import sys
sys.path.insert(0, r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp")
sys.stdout.reconfigure(encoding="utf-8")
from fr_emu import Fr, Mem, TABLE, M32, sx

ROM_BASE = 0x00080000


def encode(mnem, **want):
    """穷举 16 位字，返回第一条匹配 mnem + 指定字段值的编码"""
    for w in range(0x10000):
        ins = TABLE.decode(w.to_bytes(2, "big"), 0)
        if not ins or ins[0] != mnem:
            continue
        vals = ins[2]
        if all(vals.get(k) == v for k, v in want.items()):
            return w
    raise LookupError(f"找不到编码 {mnem} {want}")


class Machine:
    def __init__(self, words):
        self.m = Mem()
        data = b"".join(w.to_bytes(2, "big") for w in words)
        self.m.load_rom(ROM_BASE, data + b"\x00" * 16)
        self.cpu = Fr(self.m, TABLE)
        self.cpu.pc = ROM_BASE

    def run(self, n):
        for _ in range(n):
            self.cpu.step()
        return self.cpu


FAIL = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    print(f"  {'OK  ' if ok else 'FAIL'}  {name:<42} got=0x{got:08X} want=0x{want:08X}")


# ---------------------------------------------------------------- 1. 有符号除法
def test_div():
    print("[1] 有符号除法：DIV0S + 32×DIV1 + DIV2 + DIV3 + DIV4S")
    w0s = encode("div0s", ri=2, rj=2) if "rj" in (TABLE.decode(encode("div0s", ri=2).to_bytes(2, "big"), 0)[2]) else encode("div0s", ri=2)
    w1 = encode("div1", ri=2)
    w2 = encode("div2", ri=2)
    w3 = encode("div3")
    w4 = encode("div4s")
    seq = [w0s] + [w1] * 32 + [w2, w3, w4]

    cases = [(1000, 7), (-1000, 7), (1000, -7), (-1000, -7),
             (0x7FFFFFFF, 3), (-1, 1), (123456, -789), (0, 5), (-2147483648, 2)]
    for a, b in cases:
        c = Machine(seq)
        c.cpu.mdl = a & M32
        c.cpu.r[2] = b & M32
        c.run(len(seq))
        q = sx(c.cpu.mdl, 32)
        rem = sx(c.cpu.mdh, 32)
        exp_q = int(a / b)                     # 向零截断，与 FR 一致
        exp_r = a - exp_q * b
        tag = f"{a} / {b}"
        check(f"{tag} 商", q & M32, exp_q & M32)
        check(f"{tag} 余数", rem & M32, exp_r & M32)


# ---------------------------------------------------------------- 2. 算术标志位
def test_flags():
    print("[2] 加减法与标志位")
    w = encode("add", rj=1, ri=0)
    c = Machine([w])
    c.cpu.r[0], c.cpu.r[1] = 0x7FFFFFFF, 1
    c.run(1)
    check("0x7FFFFFFF+1 结果", c.cpu.r[0], 0x80000000)
    check("0x7FFFFFFF+1 V", c.cpu.v, 1)
    check("0x7FFFFFFF+1 N", c.cpu.n, 1)

    c = Machine([encode("cmp", rj=1, ri=0)])
    c.cpu.r[0], c.cpu.r[1] = 5, 7
    c.run(1)
    check("5-7 借位 C", c.cpu.c, 1)
    check("5-7 N", c.cpu.n, 1)

    c = Machine([encode("subn", rj=1, ri=0)])
    c.cpu.r[0], c.cpu.r[1] = 5, 7
    c.cpu.z = 0
    c.run(1)
    check("SUBN 结果", c.cpu.r[0], 0xFFFFFFFE)
    check("SUBN 不改 Z", c.cpu.z, 0)

    c = Machine([encode("and", rj=1, ri=0)])
    c.cpu.r[0], c.cpu.r[1] = 0xF0F0F0F0, 0x0F0F0F0F
    c.cpu.v = 1
    c.run(1)
    check("AND 清 V", c.cpu.v, 0)
    check("AND 置 Z", c.cpu.z, 1)


# ---------------------------------------------------------------- 3. 移位
def test_shift():
    print("[3] 移位")
    for mn, x, amt, exp in (("lsl", 1, 4, 16), ("lsr", 0x80000000, 4, 0x08000000),
                            ("asr", 0x80000000, 4, 0xF8000000)):
        w = encode(mn, ri=0, u4=amt)
        c = Machine([w])
        c.cpu.r[0] = x
        c.run(1)
        check(f"{mn} #{amt}", c.cpu.r[0], exp)


# ---------------------------------------------------------------- 4. 负偏移寻址
def test_disp():
    print("[4] @(disp,R14) 负偏移（编译器存取局部变量全靠它）")
    w_ld = encode("ld", ri=1, disp10=0xFF)          # disp10 字段=0xFF → -4
    c = Machine([w_ld])
    c.cpu.r[14] = 0x00040000
    c.m.wr(0x0003FFFC, 4, 0xDEADBEEF)
    c.run(1)
    check("ld @(-4,R14),R1", c.cpu.r[1], 0xDEADBEEF)

    w_st = encode("st", ri=1, disp10=0xFF)
    c = Machine([w_st])
    c.cpu.r[14] = 0x00040000
    c.cpu.r[1] = 0x12345678
    c.run(1)
    check("st R1,@(-4,R14)", c.m.rd(0x0003FFFC, 4), 0x12345678)


# ---------------------------------------------------------------- 5. 其它
def test_misc():
    print("[5] 其它：XCHB / LDI:8 / PS 布局")
    c = Machine([encode("xchb", ri=0, rj=1)])
    c.cpu.r[0], c.cpu.r[1] = 0x11223344, 0x00040000
    c.m.wr(0x00040000, 1, 0xAA)
    c.run(1)
    check("XCHB 后 R0（高 24 位保留）", c.cpu.r[0], 0x112233AA)
    check("XCHB 后内存字节", c.m.rd(0x00040000, 1), 0x44)

    c = Machine([encode("ldi:8", ri=0, i8=0xFF)])
    c.run(1)
    check("LDI:8 #0xFF（无符号）", c.cpu.r[0], 0x000000FF)

    c = Machine([])
    c.cpu.ps = (0x05 << 16) | (0x02 << 8) | 0x23    # ILM=5, SCR=2(D0=1), CCR=0x23(C,V,S)
    check("PS 回读", c.cpu.ps, (0x05 << 16) | (0x02 << 8) | 0x23)
    check("PS.S 位 (bit5)", c.cpu.s, 1)
    check("PS.D0 位 (SCR bit1)", c.cpu.d0, 1)


# ---------------------------------------------------------------- 6. EIT / 中断
def test_eit():
    print("[6] EIT / 中断：入栈顺序、ILM 屏蔽、SSP-USP 切换")
    TBR = 0x0007FC00                     # 放在 ROM 里，方便验证向量槽地址
    VEC = 0x0B                           # 假想某条硬件中断
    HANDLER = 0x00080100
    RET_SITE = 0x00080200

    c = Machine([])
    cpu, m = c.cpu, c.m
    m.load_rom(ROM_BASE, m.rom + b"\x00" * (HANDLER - ROM_BASE + 8))   # 扩到 handler
    # 向量槽 = TBR + (255-VEC)*4
    slot = TBR + (255 - VEC) * 4
    m.wr(slot, 4, HANDLER)
    win = m.wr(slot, 4, HANDLER)          # 记下来（ROM 内写入会被丢弃）
    check("向量槽地址公式", slot, 0x0007FC00 + 244 * 4)

    # 手工把 handler 写进 ROM（ROM 只读，用 ram 不行 → 直接改 rom 字节）
    off = HANDLER - ROM_BASE
    handlers = [encode("reti")]
    m.rom = m.rom[:off] + b"".join(w.to_bytes(2, "big") for w in handlers) + m.rom[off + 2:]

    # 场景：S=1（用户态，用 USP），I=1，ILM=7（级别号越小优先级越高，
    # 受理条件是 level < ILM，所以 level 5 放得进来）
    cpu.dr[0] = TBR
    cpu.dr[2] = 0x00060000                # SSP
    cpu.dr[3] = 0x00061000                # USP
    cpu.s = 1
    cpu.r[15] = 0x00061000
    cpu.i = 1
    cpu.ilm = 7
    cpu.pc = RET_SITE
    cpu.ps = cpu.ps                        # 触发一次同步

    check("S=1 时 R15 指向 USP", cpu.r[15], 0x00061000)
    cpu.request_irq(VEC, level=5)
    cpu.step()
    check("受理后跳到 handler", cpu.pc, HANDLER)
    check("入栈后 SSP 下移 8", cpu._get_sp(0), 0x0005FFF8)
    check("栈顶(SP) = 返回地址", m.rd(0x0005FFF8, 4), RET_SITE)
    check("SP+4 = 旧 PS 的 S 位仍是 1",
          (m.rd(0x0005FFFC, 4) >> 5) & 1, 1)
    check("进中断后 I 位被清", cpu.i, 0)
    check("进中断后 S=0（走系统栈）", cpu.s, 0)
    check("USP 被保存", cpu.dr[3], 0x00061000)

    cpu.step()                             # reti
    check("reti 回到断点", cpu.pc, RET_SITE)
    check("reti 恢复 S=1", cpu.s, 1)
    check("reti 后 R15 回到 USP", cpu.r[15], 0x00061000)
    check("reti 后 SSP 复原", cpu._get_sp(0), 0x00060000)

    # ILM 屏蔽：level 5 >= ILM 5 → 不受理（受理条件是 level < ILM）
    cpu.i = 1
    cpu.ilm = 5
    cpu.pc = 0x00090000
    n_eit = len(cpu.irq_log)
    cpu.request_irq(VEC, level=5)
    cpu.step()
    check("level>=ILM 被屏蔽（没进 EIT）", len(cpu.irq_log), n_eit)
    check("被屏蔽的请求仍在队列", len(cpu.irq_pending), 1)

    # I=0 时不受理
    cpu.i = 0
    cpu.ilm = 0
    cpu.request_irq(VEC, level=5)
    cpu.step()
    check("I=0 时不受理", len(cpu.irq_pending), 2)



# ------------------------------------------------- 7. CCR 的 S 位与双栈换支
def test_bank():
    print("[7] andccr / orccr 改 S 位时必须跟着换 R15（SSP ↔ USP）")
    seq = [encode("orccr", u8=0x20), encode("andccr", u8=0xDF),
           encode("orccr", u8=0x20)]
    c = Machine(seq)
    c.cpu.dr[2] = 0x0007F000          # SSP
    c.cpu.dr[3] = 0x0004A000          # USP
    c.cpu.ps = 0x00100000             # S=0 → 用 SSP
    c.cpu.r[15] = 0x0007F000
    check("起始用 SSP，R15 = SSP", c.cpu.r[15], 0x0007F000)

    c.cpu.step()                       # orccr #0x20 → S=1
    check("orccr #20 把 S 置 1", c.cpu.s, 1)
    check("R15 换到 USP", c.cpu.r[15], 0x0004A000)
    check("旧 R15 存回 SSP", c.cpu.dr[2], 0x0007F000)

    c.cpu.r[15] = 0x0004A010          # USP 往下用一点
    c.cpu.step()                       # andccr #0xDF → S=0
    check("andccr #DF 把 S 清 0", c.cpu.s, 0)
    check("R15 换回 SSP", c.cpu.r[15], 0x0007F000)
    check("用过的 USP 被存回去", c.cpu.dr[3], 0x0004A010)

    c.cpu.step()
    check("再切回 USP 时取的是存回去的值", c.cpu.r[15], 0x0004A010)

    # ILM / T 位不能被 andccr/orccr 顺手改掉
    c2 = Machine([encode("andccr", u8=0xFF)])
    c2.cpu.ps = 0x001B0000
    before_ilm = c2.cpu.ilm
    c2.cpu.step()
    check("andccr 不动 ILM", c2.cpu.ilm, before_ilm)


for fn in (test_div, test_flags, test_shift, test_disp, test_misc, test_eit, test_bank):
    fn()
print()
print(f"通过 {len(FAIL) == 0 and '全部' or '部分'}；失败项 {FAIL if FAIL else '无'}")
