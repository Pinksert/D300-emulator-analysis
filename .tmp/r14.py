import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
h=m.enable_isr2b()
for _ in range(12): m.run(max_steps=5000, spin_guard=False)
print("io_after:", m.bus.io_after, " io_value:", m.bus.io_value)
print("dp.owner(0x174):", m.dp.owner(0x174))
print("dp.children:", [(hex(a),hex(b),d.name) for a,b,d,_ in m.dp.children])
print("shadow[0x174]:", m.dp.shadow.get(0x174))
print("autospin_enabled:", m.autospin_enabled, " spin_breaker events:", len(m.spin_breaker.events))
for k in range(9):
    m.raise_irq(0x2B); steps=0
    while steps<20000:
        m.step(1); steps+=1
        if m.cpu.pc==0x80968: m.step(1); break
print("第9次后 shadow[0x174]=", m.dp.shadow.get(0x174), " io_after:", m.bus.io_after)
print("spin events:", [(hex(e["poll"]),hex(e["addr"]),hex(e["mask"])) if isinstance(e,dict) else e for e in m.spin_breaker.events])
