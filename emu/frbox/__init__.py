# -*- coding: utf-8 -*-
"""frbox —— D300 固件的全系统模拟器（QEMU 式分层）。

分层的目的是把「CPU 会算」变成「机器能跑」：

    frbox.bus      地址空间：ROM / RAM / 外设三类区域 + 未映射访问审计
    frbox.icu      中断控制器：把设备的中断线聚合成 CPU 的向量请求
    frbox.device   设备基类（QEMU DeviceState 的简化版）
    frbox.machine  机器模型：内存映射 + 复位向量 + 运行循环
    frbox.monitor  HMP 式命令行（info mtree / x / step / bp / irq ...）
    frbox.bstatic  全 ROM 静态普查：调用点 / 调用边 / store 地址（V48，V51 修地址形态）
    frbox.bpix     「有没有像素」的判据复核：面积 / 值多样 / 重画（V49）
    frbox.btick    补上缺失的 tick：延时队列 / 到期 / 整机为何停（V50，
                   V52 加严格到期 + 通用对象等待队列）
    frbox.bpatch   只读打桩：改指定 ld 的读返回值，不改内存/时基（V53，反事实）
    frbox.dev_tick18  INT 0x18 系统节拍：实证的 tick 向量，只补硬件拉线（V54）
    frbox.bobserve 纯观察钩子：只统计读写、返回值原样透传（V55，不改任何值）
    frbox.abservice μITRON 服务分发表解析：两段通用的纯函数（V56，只读 ROM 数据）
    frbox.devaudit 设备策略审计 + 忙等假墙判据（V57，纯观察、不改机器状态）
    frbox.svcgraph 两段通用的函数地址引用图（V58，纯静态；补齐 ldi:20 / rel12 /
                   4 字节函数指针三种形态，修 A 段 refs_to 的口径盲区）
    frbox.cli      QEMU 式启动参数（-M / -kernel / -S / -d / -D / -monitor）

CPU 核心仍然复用 fr_emu.Fr —— 它只依赖「有 rd/wr 的对象」，因此换成
AddressSpace 之后一行都不用改。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

__version__ = "0.58.0"
