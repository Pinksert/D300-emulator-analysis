import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
m.enable_cov(); m.boot_realos(1); m.attach_flow(); cb=m.enable_callbacks()
h=m.enable_isr2b()
for _ in range(12): m.run(max_steps=5000, spin_guard=False)

log=[]
owr, ord_ = m.bus.wr, m.bus.rd
def wr(a,n,v,pc=0):
    if 0x170<=a<=0x18F: log.append(("W",pc,a,n,v))
    return owr(a,n,v,pc)
def rd(a,n,pc=0):
    r=ord_(a,n,pc)
    if 0x170<=a<=0x18F: log.append(("R",pc,a,n,r))
    return r
m.bus.wr=wr; m.bus.rd=rd
POLL=0x9C97A
for k in range(3):
    m.raise_irq(0x2B); n=0
    while n<80000:
        m.cpu.step(); m.step_count+=1; n+=1
        if m.cpu.pc==POLL: m.bus.wr(0x174,1,0x40)
        if m.cpu.pc==0x80968: m.cpu.step(); m.step_count+=1; break
print("事务日志 %d 条（前 3 次中断）" % len(log))
for kind,pc,a,n,v in log[:70]:
    print("  %s pc=0x%05X %s 0x%03X%s%s" % (kind,pc,"wr" if kind=="W" else "rd",a,
        "" if n==1 else "[%d]"%n, " = 0x%X"%v))
