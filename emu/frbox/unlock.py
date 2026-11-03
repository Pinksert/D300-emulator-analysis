# -*- coding: utf-8 -*-
"""unlock.py —— 一站一站把「卡在某个 flag」的任务往下推（V16）

V15 的覆盖率只能回答「跑没跑过」，回答不了「为什么停在这儿」。V16 换了个走法：
**不再猜是哪个外设，而是直接看任务在等哪个 flag 的哪几位，然后用固件自己的
`sys_set_flg` 把位置上，看它走不走。**

    from frbox import unlock
    st = unlock.trace(m, 11)          # 任务 11 的整条轨迹
    unlock.print_trace(st)

每一轮做三件事：跑 `batch` 步 → 读该任务当前状态 → 如果是等 flag 就用
`realos.set_flag()` 置位（走固件自己的内核，不是改内存）。

判据与边界：

    · 「有进展」= PC 变了 / 服务调用数涨了 / 覆盖地址涨了。三者都不动就判死。
    · `msg` 与 `sleep` 不自动解 —— 消息对象要真外设来发，`slp_tsk` 要 wup_tsk，
      都不是「置一个位」能代替的。命中就停下并标出来。
    · 同一个 `(flag, pat)` 出现第三次就停：说明有人在 `clr_flg` 把它又清了，
      继续置只会绕圈（任务 12 就是这种情况）。
    · ⚠️ 这是**注入外部事件**。报告里不能写成「相机响应了按键」。
"""
from . import realos

DEFAULT_VECS = (0x13, 0x19)
DP90 = 0x90
DP90_RUN = 0x02          # 任务 8 忙等环的唯一放行位（V15 逐位实测）


def tick(m, batch=4000, allow90=True, vecs=DEFAULT_VECS):
    """跑一小段，顺手喂两路已知事件（与 V15 的 D 场景同口径）。"""
    m.run(max_steps=batch, spin_guard=False)
    if allow90:
        m.bus.wr(DP90, 1, m.bus.rd(DP90, 1) | DP90_RUN)
    for v in vecs:
        m.raise_irq(v)


def task_state(m, tid):
    for t in realos.states(m):
        if t["id"] == tid:
            return t
    return None


def _svc_of(m, tcb):
    fl = getattr(m, "flow", None)
    if fl is None or not tcb:
        return None
    return len([c for c in fl.calls if len(c) > 8 and c[8] == tcb])


def trace(m, tid, rounds=24, batch=4000, allow90=True, vecs=DEFAULT_VECS, cov=None,
          idle_max=6):
    """把一个任务推到「走不动 / 退出 / 等真外设」为止，返回站点表。

    `cov` 传 `Coverage` 时才有「跑过 / 没跑过」的区分（dormant 的两种含义靠它分开）。
    `idle_max` 是「还没被派发」最多容忍几轮 —— 固件里 sta_tsk 有的一直到 5 万步才发生，
    阈值给太小会把「还没轮到」误判成「起不来」。
    """
    stations = []
    seen = {}
    prev_pc = None
    idle = 0
    end = "轮次用尽"

    for r in range(1, rounds + 1):
        tick(m, batch, allow90, vecs)
        t = task_state(m, tid)
        if t is None:
            end = "任务表里没有这条记录"
            break

        svc = _svc_of(m, t["tcb"])
        words = cov.words() if cov is not None else None
        st = {"round": r, "pc": t["pc"], "kind": t["wait_kind"],
              "flag": t["wait_flag"], "pat": t["wait_pat"],
              "state": t["wait_state"], "svc": svc, "words": words,
              "act": "", "err": None}

        # dormant 有两种，靠覆盖率分开：
        #   跑过 → 自己 ext_tsk 退出了（正常收尾）
        #   没跑过 → 还没被派发过（别人还没 sta_tsk 它）
        if t["wait_kind"] == "dormant":
            ran = cov.visited(t["entry"]) if cov is not None else True
            if ran:
                st["act"] = "已退出（dormant）"
                stations.append(st)
                end = "任务自己 ext_tsk 退出"
                break
            idle += 1
            st["act"] = "还没被派发（入口没执行过）"
            stations.append(st)
            if idle >= idle_max:
                end = "连续 %d 轮都没被派发 —— 启动它的那条路径没被触发" % idle_max
                break
            continue

        if t["wait_kind"] in ("msg", "sleep"):
            st["act"] = "停在 %s，需要真外设" % t["wait_kind"]
            stations.append(st)
            end = "等 %s —— 超出「置一个位」能解决的范围" % t["wait_kind"]
            break

        if t["wait_kind"] == "ready":
            st["act"] = "在跑"
            stations.append(st)
            prev_pc = t["pc"]
            idle = 0
            continue

        # wait_kind == "flag"
        key = (t["wait_flag"], t["wait_pat"])
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= 3:
            st["act"] = "同一等待点第 3 次（有人在清位），停止"
            stations.append(st)
            end = "同一 (flag%d, 0x%X) 反复出现 —— 被别处 clr_flg 清掉了" % key
            break

        res = realos.set_flag(m, t["wait_flag"], t["wait_pat"])
        st["act"] = "set_flag(%s, 0x%X)" % (t["wait_flag"], t["wait_pat"])
        st["err"] = res["err"]
        stations.append(st)

        if res["err"] != 0:
            end = "set_flg 返回 %d，停下" % res["err"]
            break

        # 置位之后再看一眼有没有动静，没动静就判死
        tick(m, batch, allow90, vecs)
        t2 = task_state(m, tid)
        moved = (t2 is not None and t2["pc"] != t["pc"])
        if cov is not None and st["words"] is not None and cov.words() > st["words"]:
            moved = True
        if t2 is not None and _svc_of(m, t2["tcb"]) is not None and st["svc"] is not None:
            if _svc_of(m, t2["tcb"]) > st["svc"]:
                moved = True
        if not moved:
            stations.append({"round": r, "pc": t2["pc"] if t2 else 0,
                             "kind": t2["wait_kind"] if t2 else "?", "flag": None,
                             "pat": 0, "state": 0, "svc": None, "words": None,
                             "act": "置位后没动", "err": None})
            end = "置位后 PC/服务/覆盖三者都没动"
            break
        prev_pc = t2["pc"] if t2 else None

    return {"task": tid, "stations": stations, "end": end,
            "svc_total": _svc_of(m, (task_state(m, tid) or {}).get("tcb")),
            "words": cov.words() if cov is not None else None}


def print_trace(st, out=print):
    """把 trace() 的结果打成一张站点表。"""
    out("任务 %s 的轨迹（共 %d 站）" % (st["task"], len(st["stations"])))
    out("%-5s %-9s %-9s %-9s %-6s %s"
        % ("站", "PC", "在等", "要的位", "服务", "动作"))
    for s in st["stations"]:
        what = s["kind"]
        if s["flag"] is not None:
            what = "flag%d" % s["flag"]
        want = ("0x%X" % s["pat"]) if s["pat"] else ""
        out("%-5d 0x%06X %-9s %-9s %-6s %s"
            % (s["round"], s["pc"] or 0, what, want,
               "-" if s["svc"] is None else s["svc"], s["act"]))
    out("收尾：%s" % st["end"])
