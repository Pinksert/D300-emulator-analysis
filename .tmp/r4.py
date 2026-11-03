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
print("=== 0x180-0x19F 引用 ===")
for i,(ad,mn,v) in enumerate(L):
    if mn=="ldi:20" and 0x180<=v.get("i20",0)<=0x19F:
        nxt=" / ".join(x[1] for x in L[i+1:i+3])
        print("  0x%05X → 0x%03X r%-2s %s" % (ad,v["i20"],v.get("ri"),nxt))
