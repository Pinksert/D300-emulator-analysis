#!/bin/bash
# 编译 NikonEmulator（Temurin JDK 8，无需 ant）
set -e
cd "C:/Users/admin/WorkBuddy/D300固件/.workbuddy/tmp"
export JAVA_HOME="$PWD/jdk/jdk8u504-b01"
export PATH="$JAVA_HOME/bin:$PATH"

EMU="$PWD/nft/Java/Emulator"
cd "$EMU"

CLASSDIR=batch_build/classes
GENDIR=batch_build/gen
# 注意：不要 rm -rf batch_build（1300 个文件会触发批量删除保护），直接覆盖重编即可
mkdir -p "$CLASSDIR" "$GENDIR/com/nikonhacker"

# ---- 1. 模板替换（@APPNAME@ / @APPVERSION@ / @BUILDTIME@） ----
"$JAVA_HOME/bin/java" -version 2>/dev/null
python - <<'PY'
import time, os
src = r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp\nft\Java\Emulator\src\template\com\nikonhacker\ApplicationInfo.java"
dst = r"C:\Users\admin\WorkBuddy\D300固件\.workbuddy\tmp\nft\Java\Emulator\batch_build\gen\com\nikonhacker\ApplicationInfo.java"
os.makedirs(os.path.dirname(dst), exist_ok=True)
s = open(src, encoding='utf-8').read()
s = s.replace('@APPNAME@', 'NikonEmulator').replace('@APPVERSION@', '2.54').replace('@BUILDTIME@', time.strftime('%Y-%m-%d %H:%M:%S'))
open(dst, 'w', encoding='utf-8').write(s)
print('template ->', dst)
PY

# ---- 2. 全量编译 ----
CP="lib/commons-io-2.1.jar;lib/commons-lang3-3.1.jar;lib/jacksum.jar;lib/xstream-1.4.2.jar;lib/jgraphx.jar;lib/rsyntaxtextarea.jar;lib/miglayout-4.0.jar;lib/glazedlists-1.8.0_java15.jar"

find src/main -name "*.java" > /tmp/srcs.txt
find batch_build/gen -name "*.java" >> /tmp/srcs.txt
echo "源文件数: $(wc -l < /tmp/srcs.txt)"

javac -nowarn -g -encoding UTF-8 -d "$CLASSDIR" -sourcepath "src/main;batch_build/gen" -classpath "$CP" @/tmp/srcs.txt 2>&1 | grep -vE "^Note:" | head -40
echo "javac exit: ${PIPESTATUS[0]}"

# ---- 3. 复制资源 & 打 jar ----
cp -f src/main/com/nikonhacker/*.properties "$CLASSDIR"/com/nikonhacker/ 2>/dev/null || true
find src/main -name "*.png" -exec cp {} "$CLASSDIR"/ \; 2>/dev/null || true
find src/main -name "*.jpg" -exec cp {} "$CLASSDIR"/ \; 2>/dev/null || true
"$JAVA_HOME/bin/jar" -cf batch_build/NikonEmulator.jar -C "$CLASSDIR" .
echo "jar: $(ls -l batch_build/NikonEmulator.jar)"
