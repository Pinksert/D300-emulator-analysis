import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
from frbox import taskboot, realos

def mk(all_on=False, xfer=False):
    m=create("d300-a"); m.load()
    if all_on: taskboot.activate(m,"dormant")
    m.reset(); m.enable_cov(); m.boot_realos(1); m.attach_flow(); m.enable_callbacks()
    x=m.enable_xfer() if xfer else None
    h=m.enable_isr2b()
    for _ in range(12): m.run(max_steps=5000, spin_guard=False)
    return m,x,h

def drive(m,h,answer,N=240):
    c0=m.cov.words(); s0=len(m.flow.calls); stuck=None
    for k in range(N):
        r=h.raise_once(answer=answer)
        if r["cut"]:
            stuck=(r["n"],m.cpu.pc); break
    return c0,s0,stuck

for tag,ans,xf,ao in [("A 什么都不挂（V19 的墙）",True,False,False),
                      ("B 硬写完成位（V19 做法）",True,False,False),
                      ("C 设备模型自己答（V20）",False,True,False),
                      ("D 设备模型 + 全开 dormant",False,True,True)]:
    pass
# 分开跑，A 是 answer=False 无 xfer
cases=[("A answer=False, 无 xfer", False, False, False),
       ("B answer=True,  无 xfer", True,  False, False),
       ("C answer=False, 有 xfer", False, True,  False),
       ("D answer=False, 有 xfer, 全开", False, True, True)]
for tag,ans,xf,ao in cases:
    m,x,h=mk(ao,xf)
    c0,s0,stuck=drive(m,h,ans)
    print("%-28s 覆盖 %6d→%-6d (+%-5d) 服务 %-4d→%-4d 请求 %d 答话 %d %s"
          % (tag,c0,m.cov.words(),m.cov.words()-c0,s0,len(m.flow.calls),
             h.count,h.answers, ("卡在 第%d次 0x%05X"%(stuck[0],stuck[1])) if stuck else "全部 reti"))
    print("      任务:", {t["id"]:t["wait_kind"] for t in realos.states(m)})
