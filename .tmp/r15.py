import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
from frbox import realos
CH=[0x178,0x17A,0x17C,0x17E,0x180,0x182,0x184,0x186]
def go(vals, N=240, all_on=False):
    m=create("d300-a"); m.load()
    if all_on:
        from frbox import taskboot; taskboot.activate(m,"dormant")
    m.reset(); m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
    for c,v in zip(CH,vals): m.dp.shadow[c]=v
    h=m.enable_isr2b()
    for _ in range(12):
        for c,v in zip(CH,vals): m.dp.shadow[c]=v
        m.run(max_steps=5000, spin_guard=False)
    c0=m.cov.words(); stuck=None
    for k in range(N):
        for c,v in zip(CH,vals): m.dp.shadow[c]=v
        r=h.raise_once(answer=False)
        if r["cut"]: stuck=(r["n"],m.cpu.pc); break
    return c0, m.cov.words(), len(m.flow.calls), stuck
for tag,vals in [("全 0（现状）",[0]*8),
                 ("全 0x200（半量程）",[0x200]*8),
                 ("全 0x3FF（满量程）",[0x3FF]*8),
                 ("渐变 0x40*N",[0x40*(i+1) for i in range(8)])]:
    c0,c1,sv,st=go(vals)
    print("%-22s 覆盖 %d→%d (+%d) 服务 %d %s" % (tag,c0,c1,c1-c0,sv,
          ("卡 第%d次 0x%05X"%st) if st else "全 reti"))
