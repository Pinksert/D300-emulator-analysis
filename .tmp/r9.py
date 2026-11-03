import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
x=m.enable_xfer(); h=m.enable_isr2b()
for _ in range(12): m.run(max_steps=5000, spin_guard=False)
c0=m.cov.words()
print("xfer:", x.info())
for k in range(12):
    m.raise_irq(0x2B); n=0
    while n<80000:
        m.cpu.step(); m.step_count+=1; n+=1
        if m.cpu.pc==0x80968: m.cpu.step(); m.step_count+=1; break
    print("  第%2d 次 %6d 步 覆盖+%4d  xfer=%s" % (k+1,n,m.cov.words()-c0,x.info()))
print("总 %d (+%d)  卡在 pc=0x%05X" % (m.cov.words(), m.cov.words()-c0, m.cpu.pc))
