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
idx={ad:i for i,(ad,*_r) in enumerate(L)}
def callers(tgt):
    out=[]
    for i,(ad,mn,v) in enumerate(L):
        t=None
        if mn in ("call","call:d"):
            if "rel12" in v:
                r=v["rel12"]; r-=0x1000 if r&0x800 else 0
                t=(ad+2+r*2)&0xFFFFFFFF
            elif "ri" in v:
                for j in range(i-1,max(i-4,-1),-1):
                    if L[j][1]=="ldi:20" and L[j][2].get("ri")==v["ri"]:
                        t=L[j][2].get("i20"); break
        if t==tgt: out.append(ad)
    return out
print("0x8A500 的调用者:",[hex(x) for x in callers(0x8A500)])
print("\n=== 0x8A500 函数入口前 6 条 ===")
print(m.disasm_at(0x8A4F0,8))
print("\n=== 读通道函数尾部 ===")
print(m.disasm_at(0x8A5B8,14))
