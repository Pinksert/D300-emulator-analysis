import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
h=m.enable_isr2b()
for _ in range(12): m.run(max_steps=5000, spin_guard=False)
for a in range(0x170,0x190): m.bus.watch.add(a)
m.bus.access_log.clear()
for k in range(240):
    r=h.raise_once(answer=False)
    if r["cut"]: break
log=m.bus.access_log
print("240 次中断后访问记录 %d 条" % len(log))
from collections import Counter
print("按地址：", Counter(("%s 0x%03X"%(k,a)) for k,a,n,v,pc in log).most_common(20))
print("\n0x178-0x18F 的记录：")
for k,a,n,v,pc in log:
    if 0x178<=a<=0x18F: print("  %s pc=0x%05X 0x%03X = 0x%X" % (k,pc,a,v))
