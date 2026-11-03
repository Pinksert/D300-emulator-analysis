import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create, TABLE
m=create("d300-a"); m.load()
rom=m.bus.rom; base=m.bus.rom_base
L=[];a=base
while a<0x100000:
    ins=TABLE.decode(rom[a-base:a-base+8],0)
    if ins is None: a+=2; continue
    mn,_,v,size,_w=ins; L.append((a,mn,v)); a+=size
TGT=set(range(0x170,0x180))
# 找 ldi:20 到 0x17x，且 3 条内出现 访存指令
hits={}
for i,(ad,mn,v) in enumerate(L):
    if mn=="ldi:20" and v.get("i20") in TGT:
        t=v["i20"]
        ctx=[]
        for j in range(i,min(i+3,len(L))):
            ad2,mn2,v2=L[j]
            ctx.append((ad2,mn2,v2))
        hits.setdefault(t,[]).append((ad,v.get("ri"),ctx))
for t in sorted(hits):
    print("=== 0x%03X : %d 处引用 ===" % (t,len(hits[t])))
    for ad,ri,ctx in hits[t]:
        ops=[x[1] for x in ctx[1:]]
        print("   0x%05X r%-2s → %s" % (ad,ri," / ".join(ops)))
