import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create

def run(run_tick, xfer, N=9):
    m=create("d300-a"); m.load(); m.reset()
    m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
    if xfer: m.enable_xfer()
    h=m.enable_isr2b()
    for _ in range(12): m.run(max_steps=5000, spin_guard=False)
    hit97A=0
    for k in range(N):
        m.raise_irq(0x2B); steps=0
        while steps<20000:
            c=m.cpu; pc_before=c.pc
            if run_tick: m.step(1)
            else: c.step(); m.step_count+=1
            steps+=1
            if c.pc==0x9C97A: hit97A+=1
            if c.pc==0x80968:
                if run_tick: m.step(1)
                else: c.step(); m.step_count+=1
                break
        else:
            return ("卡死 第%d次"% (k+1), steps, hit97A, hex(m.cpu.pc), m.bus.rd(0x174,1), m.bus.rd(0x482,1))
        if k==N-1:
            return ("ok", steps, hit97A, hex(m.cpu.pc), m.bus.rd(0x174,1), m.bus.rd(0x482,1))
for tag,rt,xf in [("cpu.step 无xfer",False,False),("cpu.step 有xfer",False,True),
                  ("m.step   无xfer",True,False),("m.step   有xfer",True,True)]:
    print("%-20s -> %s" % (tag, run(rt,xf)))
