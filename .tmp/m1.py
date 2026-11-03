import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
h=m.enable_isr2b()
for _ in range(12): m.run(max_steps=5000, spin_guard=False)
s0=m.step_count
for k in range(20):
    r=h.raise_once(max_steps=4000)
print("20 次累计步数 =", m.step_count-s0)
print("history 前 12 条的 delta:", [d for _n,d,_cb in h.history][:12])
