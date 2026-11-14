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
    frbox.flaggraph 事件标志位级服务图（V59，纯函数；按 (flgid,bit) 配对
                   等 / 置 / 清，clr_flg 按 mask 为 0 的位算，mode 不解释）
    frbox.ablink   A-B 跨机事件链 + 联合画面取证（V60，模型化假设；
                   AbLink / OutputTap / LinkedPair 可跑可计数可证伪，
                   render_screen 产结构重建图、像素非固件）
    frbox.addrform 访存地址形态分类：dir*=直接页、disp*=栈帧相对（V61，
                   纯静态；修「把 ldub #252 当成直接页 0xFC」的口径盲区）
    frbox.dispatch62  vec 0x2B 业务分派器解剖：6 个状态字 / 15 条 call /
                   按位派发表（V62，纯静态；rel12 按 fr_emu.py:777 算）
    frbox.isrfam63 256 槽中断向量表 / 四条同族门 / 无 enter 的 ISR 序言 /
                   显示状态查询函数（V63，纯静态；判 ISR 归属先反查向量表）
    frbox.taskchain64 任务表 29 条 / 14 个显示任务 / sta_tsk 启动链 /
                   对象池 70 槽布局 / 写点归属（V64，纯静态；
                   同时更正 dispatch62.rel9_target 的 8 位有符号口径）
    frbox.bootchain65 任务 2 开机装配函数 / 18 连串 call → sta_tsk 启动器 /
                   ROM 表扫描循环（V65，纯静态；add2 的 m4 是 m4-16 的
                   负立即数，bgt 条件来自 add2 的 CCR 而非 ld 读值）
    frbox.stastat66 sta_tsk 调用四层判定：受理 / 已激活 / 上过 CPU /
                   收敛状态（V66，纯函数；判「已激活」用 state≠DORMANT
                   +有栈，不能用 state==0 或「在就绪队列上」）
    frbox.waitstat67 等待原因按立即数宽度 / 16 位补码解码、对象队列归属
                   闭环、唤醒条件（V67，纯函数；0xFF56 = −170 不是 +86，
                   TCB+0x14 在 flag 等待时是位模式而非剩余 tick）
    frbox.setsrc68 等待位的静态置位源 / 覆盖缺口 / ISR 归属 / 反事实差分
                   （V68，纯函数；存在性 / 执行性 / 触发性三层必须一起报，
                   clr_flg 清 mask 里为 0 的位，反事实须等形对照 + 归因分离）
    frbox.descgate69 描述符派发链的门判据（V69，纯函数；0x12E752 是
                   27 个槽位指向的裸 ret 空实现、不是第二道门；
                   收敛态写数据门无效，与 V25 的主循环存活机不可混比）
    frbox.caller70 四形态调用边 / r10 局部标志 / 执行前沿（V70，纯函数；
                   call:d @rN 与 call rel12 是两套直接边，edges 0 要先问口径；
                   0x12E1EC 的调用者是任务 5 入口 0x12DFAE，收敛态卡在
                   0x12DFBA 的 wai_flg，不是没人调用）
    frbox.wait271 第二道等待的掩码判据与四级状态机（V71，纯函数；
                   0x74 是等待模式 + 检查掩码，不是要被置的位；
                   通过条件是 (flgptn & 0x74) == 0x10，
                   0x12DFD0 是 0x12DFC4 bne 的直接目标；
                   服务号 ldi:32 #0xFFFFFF56 = -170，取低 8 位报 86 是假象）
    frbox.level72  五级链与 0x12E1EC 两条数据早退路径（V72，纯函数；
                   0x12E1EC 返回 0 是 0x9F58DA84==0 的早退，不是空实现；
                   DA83>=70 是描述符越界早退；正常路径把 0x9F58D858 填成
                   0x001BC020 + idx*0x10；四级全过后 0x12DFFE 才调
                   0x12E674（门 0x12E6E0 / 间接调用 0x12E6F0）；
                   enclosing() 函数边界不等于真实函数边界）
    frbox.cursor73 KI#282 第一条前提证伪与真机事件链定位（V73，纯函数；
                   DA84 不是等真机填，固件自己在 0x12E0F8 写三个点，
                   收敛态 0x12E140 已命中并写查询函数 0x12E636 的返回值 0；
                   DA83 由回调 0x12F77A 写，该函数同时清 DA86 并
                   set_flg(3,0x10)；事件链 = vec 0x20 -> ISR 0x12FE54
                   -> 读 DA85 查 0x1BC480 回调表 -> call @r0；
                   向量表是倒序映射 vector(n)=TBR+(255-n)*4（规矩 60）；
                   报「某字节等真机填」前先扫固件写点（规矩 61））
    frbox.cursor74 KI#286 第一条结案与 24 项回调表语义（V74，纯函数；
                   回调 0x12F77A 写 DA83 用的是中断现场 r4，派发器 A 不赋值；
                   全 ROM flag3 无 0x74 置位源，判据 (pat&0x74)==0x10 下
                   0x20/0x40 是干扰位；D858 算式的 idx 实际来自 DA82 中转
                   0x12E242 stb -> 0x12E268 ldub -> lsl #4（规矩 62）；
                   L4 首次不靠外部写字节由固件自身事件链算出 D858=0x001BC070）
    frbox.gate75   L4 恒过、r10 门默认开与处理函数首次命中（V75，纯函数；
                   0x12E48A ret:d 延迟槽 0x12E48C ldi:8 #1,r4 ⇒ 第四级
                   无条件通过；跨 L3 才需第三级补 0x10；0x12E69E 在
                   beq:d 延迟槽给 r10=1 ⇒ 0x12E6E0 门默认开；
                   描述符 +0x0C 决定处理函数，Q2 首次命中 0x12E806
                   （1/44），Q1/Q5 命中裸 ret 空槽；规矩 63 要求报
                   「某级是条件」前先查延迟槽；规矩 54 分清 312 加载点
                   与 302 调用点两种 PC 口径）
    frbox.submit76 提交函数身份、0x41 等待置位源、门字节与 8 槽填充源
                   （V76，纯函数；0x12E6F8 是 sem17 保护的队列生产者，
                   不是 V63 说的「公共状态查询函数」；312 加载点全在
                   未执行路径；门字节 0x9F58D08F 有 0x12ECF4 写 0 /
                   0x12F180 写 1 两个固件写点，规矩 61；0x41 是第五道
                   twai_flg 的 waiptn，V75 拿它套 L3 检查常量是跨级串味，
                   置位源是 vec 0x2D ISR 0x12FE84 的 set_flg(3,0x40)
                   + 3 点 set_flg(3,0x01)，规矩 60 倒序；两个状态字 /
                   8 槽静态可定址写点 0，但有 224489 条不可定址 store，
                   只能报「无精确写点」不能报「固件没写」；8 槽全 0
                   由 0x1758EC 的 24 轮开机清零循环解释）
    frbox.reduce77 不可定址 store 形态分类 / 结构体字段写归约 /
                   vec 0x2D ISR 设备面（V77，纯函数；224489 条 unknown
                   里 188255 条是栈帧写，与全局状态字无关；按 ret 拆边界
                   做局部寄存器传播，把 0x9F5A0284 / 0x9F59EF50 /
                   0x9F59F7C8 的字段写归约成 base+off；规矩 65 分清
                   基址写 / 字段写 / 栈写，规矩 66 认清 borh 的 u4 是
                   高半字节掩码不是位号；8 槽另有 0x175958 写回槽 0）
    frbox.owner78  函数边界口径 / 静态直接调用边四形态 / 字段写与
                   提交调用点的任务归属（V78，纯函数；enter 序言起点与
                   ret 多出口要分开，Sweep.edges 只收 call:d、漏掉
                   8593 个只被 call rel12 调到的入口；等待 0x175876 /
                   清零循环 0x1758EC / 写回 0x175908 是三个相邻函数，
                   -37=tslp_tsk，规矩 67 / 68 / 69）
    frbox.tick79   真时基 tick 链 / 调用形态七分法（V79，纯函数；
                   vec 0x18 ISR 0x04046E → 0x040482 → 0x040A44；
                   call:d rel12 是第五形态，只被它解出的入口 7616 个，
                   0x1758EC 有 5 条入边；规矩 70 七套口径 /
                   规矩 71 8 槽全 0 不能证明清零循环没跑）
    frbox.wb80     写回段唯一入口与 flag 12 通信面（V80，纯函数；
                   ICR 0x448=0x12 配置链开机全命中但 TICK_ISR 0 命中；
                   0x17574C call rel12=221 是 0x175908 唯一入边，
                   前置 r4==-85；清零段 set_flg(12,1) 已执行、
                   补数据无增益、强制入口 39/75；规矩 72 参数寄存器 /
                   规矩 73 配置≠器件、强制入口≠真链）
    frbox.cmd81    主循环返回码与命令分发段的两道串联判据（V81，纯函数；
                   0x175270 的 ldi:32 #0xFFFFFF56 按 8 位=86 / 32 位=-170，
                   全 ROM 91 处、r5=12 r6=0xA2A05E1C r7=2；
                   r4 非 0 才 jmp 0x17573C，r4==-85 才 0x17574C call
                   rel12=221 到 0x175908；等形补位四组改变执行但
                   MAIN_JMP/CMD_SEG/WB_CALL 全 False；强制 r4=-85
                   写回段 11/75；规矩 74 双宽度口径 /
                   规矩 75 补位时机与两道门不可合并）
    frbox.svc82    真内核服务分发表索引规整口径与返回码出口（V82，纯函数；
                   int #0x40 → vec 0x40 槽 0x000EFEFC → 陷阱 0x040668；
                   表项槽 = (0x040660 + (r12 << 1)) & 2^32，r12 是完整
                   32 位有符号值、负服务号往低地址索引，0x040660 是零点；
                   主循环 0xFFFFFF56 = -170 → 槽 0x04050C 半字 0x158A →
                   处理函数 0x041BEA（单步实测），返回码 0 使第一道门恒关；
                   0x041CA0 ldi:8 #171,r12 写 -85 正是第二道门的码；
                   等形反事实在 0x0406B4 写 -85 让两道门全开并命中写回函数；
                   规矩 76 真内核用 32 位有符号口径 / 规矩 77 相邻服务
                   共享尾部、不许并函数边界）
    frbox.svc83    返回码写在 ret:d 延迟槽里、-85 出口的归属与触发条件（V83，纯函数；
                   内核区 167 条 ret:d / 105 条返回码延迟槽 / 7 处 -85，
                   V82 的「2 个」是段内口径；0x041CA0 是 0x041C9E ret:d 的
                   延迟槽、没有直接入边，真正入口是 0x041C9A；
                   两条 -85 入边 0x041BC6/0x041BCC 在 -106 号服务 0x041B92 内，
                   真实 -170 调用却走 -46 号尾部的 0x041B84 r12=0；
                   等形反事实两道内部条件 r13==0 与 (r6&r8)==0 让服务自己
                   返回 -85 并命中写回函数；规矩 78 延迟槽≠基本块 /
                   规矩 79 入边来源≠地址就近，仍非真机器件事件）
    frbox.svc84    两道内部条件的数据源与干预层级（V84，纯函数；
                   r13 来自 0x041BFE ld disp10=13,r13 → 单元 0x000083E4，
                   是陷阱分发器 0x040692 stm1 的栈帧保存槽（71 次 / 14 种值），
                   不是器件状态字；r8 来自 0x041BB8 ld disp10=2,r8 →
                   单元 0x68000970，由 0x041A60 or 置位 / 0x041AFE and 清位
                   维护的内核对象位标志字；writers_of 两处为 0 但动态
                   all_writes 锚点 76/4 次、收敛态 182/5 次（静态假阴性）；
                   7 台等形反事实只补内存单元、不改寄存器：早补 A 被 stm1
                   覆盖，晚补 A + 补 B 时服务自然返回 0xAB 并命中命令分发链；
                   规矩 80 分清数据源类型与静态/动态写点 / 规矩 81
                   分清干预层级与写入时机，仍非真机器件事件）
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

__version__ = "0.84.0"
