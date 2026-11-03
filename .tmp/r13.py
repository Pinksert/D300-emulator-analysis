import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
h=m.enable_isr2b()
for _ in range(12): m.run(max_steps=5000, spin_guard=False)
m.bus.watch.add(0x174); m.bus.access_log.clear()
for k in range(9):
    m.raise_irq(0x2B); steps=0
    while steps<20000:
        m.step(1); steps+=1
        if m.cpu.pc==0x80968: m.step(1); break
    if k>=7:
        print("--- 第%d次结束 [0x174]=0x%02X ---" % (k+1, m.bus.rd(0x174,1)))
print("0x174 访问 %d 条（近 30 条）：" % len(m.bus.access_log))
for kind,a,n,v,pc in m.bus.access_log[-30:]:
    print("  %s pc=0x%05X %s = 0x%X" % (kind,pc,"wr" if kind=="W" else "rd",v))
