# -*- coding: utf-8 -*-
"""整机快照：savevm / loadvm（对应 QEMU 的 savevm/loadvm）。

有了它，调固件就不用每次从头跑 4 万步：
一次跑到位 → 存快照 → 之后反复从同一点试不同假设，且能验证「同一状态再跑 N 步
结果完全一样」——这是判断「是不是解释器不确定」的唯一硬证据。

快照里有什么：
    cpu     r0–r15、dr0–dr3(TBR/RP/SSP/USP)、PC、PS 各标志、延迟槽、停机原因
    ram     每一块 ram 区域的字节（ROM 不存，重新 load 固件即可）
    devices 每个设备的可序列化字段（寄存器影子、计数、状态机）
    misc    步数、poke 列表、时钟锁定 tick 数

不做的事：不存未映射访问审计与 trace 日志（那些是统计，不是状态）。
"""
import base64
import json
import os
import struct

MAGIC = b"FRBOXSNP"
VERSION = 1

_SKIP = {"machine", "trace", "irq", "children", "dev", "out"}


def _dev_snap(dev):
    out = {}
    for k, v in dev.__dict__.items():
        if k in _SKIP or k.startswith("__"):
            continue
        if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v
        elif isinstance(v, (bytes, bytearray)):
            out[k] = {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
        elif isinstance(v, dict):
            try:
                json.dumps({str(a): b for a, b in v.items()})
                out[k] = dict(v)
            except (TypeError, ValueError):
                pass
        elif isinstance(v, list):
            try:
                json.dumps(v)
                out[k] = list(v)
            except (TypeError, ValueError):
                pass
    return out


def _unwrap(v):
    if isinstance(v, dict) and "__b64__" in v and len(v) == 1:
        return base64.b64decode(v["__b64__"])
    return v


def _fix_keys(old, v):
    """JSON 会把字典的整数键变成字符串，恢复时要还原，否则 shadow.get(0x482) 取不到"""
    if not isinstance(old, dict) or not isinstance(v, dict) or not v:
        return v
    if all(isinstance(k, str) and k.lstrip("-").isdigit() for k in v):
        return {int(k): val for k, val in v.items()}
    return v


def _dev_restore(dev, snap):
    for k0, v0 in snap.items():
        k, v = k0, _unwrap(v0)
        old = dev.__dict__.get(k)
        v = _fix_keys(old, v)
        if isinstance(old, bytearray) and isinstance(v, bytes):
            dev.__dict__[k] = bytearray(v)
        else:
            dev.__dict__[k] = v


def cpu_snapshot(cpu):
    return {
        "r": list(cpu.r), "dr": list(cpu.dr), "pc": cpu.pc,
        "ps": cpu.ps, "delay": cpu.delay, "halted": cpu.halted,
        "halt_reason": cpu.halt_reason, "ilm": cpu.ilm,
        "irq_pending": list(cpu.irq_pending),
    }


def cpu_restore(cpu, s):
    cpu.r[:] = list(s["r"])
    cpu.dr[:] = list(s["dr"])
    cpu.pc = s["pc"]
    cpu.ps = s["ps"]
    cpu.delay = s["delay"]
    cpu.halted = s["halted"]
    cpu.halt_reason = s["halt_reason"]
    cpu.ilm = s["ilm"]
    cpu.irq_pending = list(s["irq_pending"])


def save(machine, path):
    ram = []
    for r in machine.bus.regions:
        if r.kind == "ram":
            ram.append({"name": r.name, "base": r.base, "data": bytes(r.buf)})
    meta = {
        "magic": MAGIC.decode(), "version": VERSION,
        "machine": machine.name, "steps": machine.step_count,
        "cpu": cpu_snapshot(machine.cpu),
        "ram_index": [(x["name"], x["base"], len(x["data"])) for x in ram],
        "devices": dict({d.name: _dev_snap(d) for d in machine.devices},
                        **{machine.icu.name: _dev_snap(machine.icu)}),
        "clk_lock_ticks": machine.clk_lock_ticks,
        "pokes": [list(p) for p in machine.pokes],
    }
    blob = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(blob)))
        f.write(blob)
        for x in ram:
            f.write(x["data"])
    return os.path.getsize(path)


def load(machine, path):
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("不是 frbox 快照文件")
        (n,) = struct.unpack(">I", f.read(4))
        meta = json.loads(f.read(n).decode("utf-8"))
        if meta.get("version") != VERSION:
            raise ValueError(f"快照版本 {meta.get('version')} 与本程序 {VERSION} 不一致")
        if meta.get("machine") != machine.name:
            raise ValueError(f"快照属于 {meta.get('machine')}，当前机器是 {machine.name}")
        cpu_restore(machine.cpu, meta["cpu"])
        for name, base, size in meta["ram_index"]:
            r = machine.bus.find(base)
            if r is None or r.kind != "ram":
                raise ValueError(f"快照里的 RAM {name}@0x{base:08X} 在当前机器上不存在")
            r.buf[:] = f.read(size)
        for d in list(machine.devices) + [machine.icu]:
            s = meta["devices"].get(d.name)
            if s is not None:
                _dev_restore(d, s)
        machine.step_count = meta["steps"]
        machine.pokes = [tuple(p) for p in meta["pokes"]]
        machine._spin_base = None
        machine._spin_count = 0
        machine.spin = None
    return meta


def describe(meta):
    c = meta["cpu"]
    return (f"快照：{meta['machine']}  {meta['steps']:,} 步  "
            f"PC=0x{c['pc']:08X}  PS=0x{c['ps']:08X}  "
            f"RAM {sum(x[2] for x in meta['ram_index']):,} 字节 / {len(meta['ram_index'])} 块")
