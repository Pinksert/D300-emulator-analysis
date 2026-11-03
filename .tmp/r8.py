import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load(); m.reset()
for a in (0x170,0x174,0x176,0x178,0x186,0x53C,0x53E):
    r=m.bus.find(a)
    print("0x%03X -> %s" % (a, (r.kind, hex(r.base), hex(r.end)) if r else None))
print("regions:")
for r in m.bus.regions:
    print("  %-6s 0x%05X-0x%05X %s" % (r.kind, r.base, r.end, getattr(r.dev,'name','') if r.dev else ''))
