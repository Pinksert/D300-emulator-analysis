import os,sys
sys.path.insert(0,os.path.abspath("emu"))
sys.stdout.reconfigure(encoding="utf-8")
from frbox.machine import create
m=create("d300-a"); m.load()
print("=== 0x9C94A 起 40 条 ===")
print(m.disasm_at(0x9C94A,40))
