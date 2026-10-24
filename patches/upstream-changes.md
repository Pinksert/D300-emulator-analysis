# 对 NikonEmulator 上游的修改

上游工程原本面向更早的机型，跑 D300 B 段（固件 1.11）时要么直接崩，要么静默失效。**以下 5 处必须改**，`drivers/` 下的驱动依赖它们。

本目录只记录改动内容与原因。上游文件本身是第三方代码，不在这里复制。

---

## 1. 边沿触发中断的撤销是空实现 → ISR 无限重入

**文件**：`peripherials/interruptController/fr/FrInterruptController.java:180`

```java
public void removeEdgeTriggeredRequest(InterruptRequest interruptRequest) {
    // empty, because all FR interrupts are all level-triggered
}
```

**症状**：注入一次中断后请求永远撤不掉，ISR 被反复重入，点火计数饱和在 64,160 不再增长，CPU 实际卡在 ISR 里出不来。

**修法**：不要依赖这个方法。在驱动里**等 CPU 接受该中断后立刻手动撤销**：

```java
// D300Press.java / D300Isr.java 里的做法
if (platform.getInterruptController().hasActiveInterruptRequest() /* 或等效判据 */) {
    platform.getInterruptController().request(IRQ_N);   // 置请求
    fired = true;
}
// 检测到 CPU 已进入 ISR（PC 落在 ISR 地址范围）后：
platform.getInterruptController().removeRequest(IRQ_N);
fired = false;
```

---

## 2. 引脚监听返回寄存器值 → 引脚注入无效

**文件**：`memory/listener/fr/ExpeedPinIoListener.java:30`

```java
case PORT_VALUE_ADDRESS:
    return port.getValue();     // ← 只用寄存器的值，完全忽略内存里的实际内容
```

**症状**：往引脚写的值根本进不了固件，按键注入"看起来成功"但固件毫无反应。

**修法**：驱动侧改用 `Pin.setInputValue()` 直接注入，绕开这条返回值路径：

```java
// D300Press.java:96 / :182
platform.getIoPorts()[7].getPin(3).setInputValue(1);   // 真正被固件看到的按键电平
```

---

## 3. EXPEED 代理页的地址守卫判据写错

**文件**：`memory/listener/fr/ExpeedProxyIoListener.java:32`

原逻辑用完整地址判断，把高位 EXPEED 页全跳过了，于是 `0x7000xxxx` 的读写一次也抓不到。

**修法**：

```java
private IoActivityListener get(int address) {
    int page = address >>> 16;
    if (routeAll) {                                    // 新增开关
        if (page == 0x0000 && (address & 0xFFFF) >= IO_ADDR_LIMIT) return null;
        return listeners.get(page);
    }
    // 原逻辑：address >= IO_ADDR_LIMIT 一律放行（副作用：高位 EXPEED 页全被跳过）
    if (page == 0x7000) return listeners.get(page);    // 新增：只开 0x7000 探针
    if (address >= IO_ADDR_LIMIT) return null;
    return listeners.get(page);
}
```

正确判据是 **`page == 0x0000 && (addr & 0xFFFF) >= 0x1000`**（只排除 0x0000 页里的片上 RAM），其余页正常路由。

---

## 4. 串口 FIFO 为 null 时抛 NPE

**文件**：`peripherials/serialInterface/fr/FrSerialInterface.java:855 / :870`

**修法**：加空保护。

```java
public int getFbyte1() {
    return fifo1 == null ? 0 : fifo1.size();
}

public int getFbyte2() {
    return fifo2 == null ? 0 : fifo2.size();
}
```

---

## 5. 串口模式只放行 MD=0b000/0b010 → 固件在 3,119,524 步崩溃

**文件**：`peripherials/serialInterface/fr/FrSerialInterface.java:209`

**症状**：显示管线打通之后跑到第 312 万步抛 `Serial Interface 0: cannot be configured in mode MD=0b001`。

**修法**：把 **MD=0b001**（异步模式 1，多机/带第 9 位）也纳入 UART 旁路，与 0b000 走同一条路径。

```java
public void setSmr(int smr) {
    int md = smr >>> 5;
    if (md != 0b010) {
        // D300 把串口配成异步 UART（MD=0b000），而本仿真器只实现了同步 CSIO。
        // 与其抛异常中止，这里放行并切到最小 UART 旁路（见 setTdr / getSsr）。
        // MD=0b001 = 异步模式 1（多机/带第 9 位）。同样只做旁路：
        // 把写出去的字节记录下来，好在固件不被卡死的同时留下证据。
        if (md == 0b000 || md == 0b001) {      // ← 原来是 md == 0b000
            uartMode = true;
            this.smr = smr;
            return;
        }
        throw new EmulationException(
            "Emulator: Serial Interface " + index + ": cannot be configured in mode MD=0b"
                + Integer.toBinaryString(md));
    }
    ...
}
```

---

## 编译注意

- 用 **JDK 8** 编译
- **不要** `rm -rf batch_build` 后重编 —— 该目录 1300+ 文件会触发批量删除保护。直接覆盖重编。
