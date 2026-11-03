import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load()
print("########## A: 0x8A220 ##########")
print(m.disasm_at(0x8A220,34))
print("\n########## B: 0xAF370 ##########")
print(m.disasm_at(0xAF370,30))
