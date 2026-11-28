#!/bin/bash
# MappleMTLDriver.bundle for the phone (iOS 16, arm64) on Mesa's freedreno: cross-builds Mesa and the msm libdrm stand-in,
# compiles the Mali tree's Metal driver (after tools/prepare-driver.py) and links the bundle, as the Mali tree's ios/build.sh does.
#   MALI=mapple_gpu_mali checkout  SRC=Mesa source with the patches applied  OUT=output dir
set -euo pipefail
: "${MALI:?}" "${SRC:?}" "${OUT:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
CC="$(xcrun --sdk iphoneos -f clang)"
TARGET="-target arm64-apple-ios16.0 -isysroot $SDK"
B="$OUT/build-ios"
OBJ="$OUT/obj"
DRM="$OUT/libdrm"
mkdir -p "$OBJ" "$DRM" "$OUT/pc" "$OUT/dist/tests"
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

log "driver sources"
python3 "$HERE/tools/prepare-driver.py" "$MALI"

# ---------------------------------------------------------------- libdrm-xnu (msm) for iOS
log "libdrm-xnu for iOS"
# the iOS SDK has no IOKit headers: msm_gpu.c gets the Mali tree's compat declarations instead
sed 's|#include <IOKit/IOKitLib.h>|#include "iokit_ios.h"|' "$HERE/libdrm/msm_gpu.c" > "$DRM/msm_gpu_ios.c"
grep -q iokit_ios.h "$DRM/msm_gpu_ios.c"
DRMCF="-I$MALI/libdrm-xnu/include -I$MALI/libdrm-xnu/include/libdrm -I$HERE/libdrm -I$MALI/vgpu"
for f in "$HERE/libdrm/xf86drm_msm.c" "$DRM/msm_gpu_ios.c"; do
  $CC $TARGET -O2 -g -Wall -Wno-unused-parameter -fPIC $DRMCF -c "$f" -o "$DRM/$(basename "${f%.c}").o"
done
rm -f "$DRM/libdrm-xnu.a"
xcrun --sdk iphoneos libtool -static -o "$DRM/libdrm-xnu.a" "$DRM/xf86drm_msm.o" "$DRM/msm_gpu_ios.o"
cat > "$OUT/pc/libdrm.pc" <<EOT
prefix=$MALI/libdrm-xnu
includedir=\${prefix}/include

Name: libdrm
Description: libdrm-xnu (msm) for iOS
Version: 2.4.134
Libs: -L$DRM -ldrm-xnu
Cflags: -I\${includedir} -I\${includedir}/libdrm
EOT

# ---------------------------------------------------------------- Mesa (freedreno) for iOS
log "Mesa cross file"
cat > "$OUT/ios-arm64.ini" <<EOT
[constants]
sdk = '$SDK'
arch_flags = ['-arch', 'arm64', '-isysroot', sdk, '-mios-version-min=16.0']

[binaries]
c = ['xcrun', '--sdk', 'iphoneos', 'clang']
cpp = ['xcrun', '--sdk', 'iphoneos', 'clang++']
ar = ['xcrun', '--sdk', 'iphoneos', 'ar']
strip = ['xcrun', '--sdk', 'iphoneos', 'strip']
pkg-config = '$(command -v pkg-config)'

[built-in options]
c_args = arch_flags + ['-target', 'arm64-apple-ios16.0', '-g0', '-include', 'dlfcn.h']
cpp_args = arch_flags + ['-target', 'arm64-apple-ios16.0', '-g0']
c_link_args = arch_flags + ['-target', 'arm64-apple-ios16.0']
cpp_link_args = arch_flags + ['-target', 'arm64-apple-ios16.0']
default_library = 'static'
buildtype = 'release'

[properties]
pkg_config_libdir = ['$OUT/pc']
needs_exe_wrapper = true

[host_machine]
system = 'darwin'
subsystem = 'ios'
kernel = 'xnu'
cpu_family = 'aarch64'
cpu = 'arm64'
endian = 'little'
EOT

log "configure Mesa"
meson setup "$B" "$SRC" --cross-file "$OUT/ios-arm64.ini" -Db_ndebug=true \
  -Dplatforms= -Dgallium-drivers=freedreno -Dvulkan-drivers= -Dopengl=false -Dgles1=disabled -Dgles2=disabled -Degl=disabled \
  -Dglx=disabled -Dgbm=disabled -Dllvm=disabled -Ddraw-use-llvm=false -Dexpat=disabled -Dvalgrind=disabled \
  -Dlibunwind=disabled -Dlmsensors=disabled -Dbuild-tests=false -Dtools= -Dvideo-codecs= -Dgallium-va=disabled -Dperfetto=false \
  -Dhtml-docs=disabled -Dzstd=disabled -Dshader-cache=disabled

log "build Mesa archives"
T=$(ninja -C "$B" -t targets all | sed -n 's/^\(src\/.*\.a\): .*/\1/p')
ninja -C "$B" -k 0 $T 2>&1 | tee "$OUT/mesa-build.log"
test "${PIPESTATUS[0]}" -eq 0
LIBS=$(cd "$B" && for t in $T; do echo "$B/$t"; done)

# ---------------------------------------------------------------- the driver
log "driver objects"
I="$SRC"
CF="-DNDEBUG -D__APPLE__ -DHAVE_STRUCT_TIMESPEC -DHAVE_PTHREAD -DHAVE_ENDIAN_H -DHAVE_DLFCN_H -DHAVE_SYS_SYSCTL_H -include dlfcn.h
 -I$I/include -I$I/src -I$B/src -I$B/include
 -I$I/src/gallium/include -I$I/src/gallium/auxiliary -I$B/src/gallium/auxiliary
 -I$I/src/gallium/drivers -I$I/src/gallium/winsys
 -I$I/src/compiler -I$B/src/compiler -I$I/src/compiler/nir -I$B/src/compiler/nir
 -I$I/src/freedreno -I$B/src/freedreno -I$I/src/freedreno/common -I$B/src/freedreno/registers"
CF=$(echo $CF)
for f in vmg_air vmg_screen vmg_exec vmg_bounds; do
  $CC $TARGET -std=gnu11 -O2 -g -DVMG_HAVE_LLVMPIPE=0 $CF -I"$MALI/driver" -I"$MALI/driver/isa" -c "$MALI/driver/$f.c" -o "$OBJ/$f.o"
done
for f in "$MALI"/driver/isa/*.c; do
  $CC $TARGET -std=gnu11 -O2 -g -DVMG_HAVE_LLVMPIPE=0 $CF -I"$MALI/driver" -I"$MALI/driver/isa" -c "$f" -o "$OBJ/isa_$(basename "${f%.c}").o"
done
$CC $TARGET -fobjc-arc -O2 -g -DVMG_HAVE_LLVMPIPE=0 $CF -c "$MALI/driver/vmg_formats.m" -o "$OBJ/vmg_formats.o"
$CC $TARGET -O2 -g -Wall -Wno-unused-parameter $DRMCF -c "$HERE/libdrm/vgpu_compat.c" -o "$OBJ/vgpu_compat.o"

log "link bundle"
BUNDLE="$OUT/dist/MappleMTLDriver.bundle"
rm -rf "$BUNDLE"; mkdir -p "$BUNDLE"
$CC $TARGET -fobjc-arc -O2 -g -bundle -Wno-deprecated-declarations -I"$MALI/driver/metal" -I"$MALI/driver" \
    "$MALI"/driver/metal/*.m "$OBJ"/*.o "$DRM/libdrm-xnu.a" $LIBS -lz -lc++ \
    -framework Foundation -framework Metal -framework IOSurface -framework IOKit -framework QuartzCore -framework CoreGraphics \
    -o "$BUNDLE/MappleMTLDriver" 2>&1 | tee "$OUT/link.log"
test "${PIPESTATUS[0]}" -eq 0
sed "s/BUILD_VERSION/$(date +%Y%m%d.%H%M)/" "$MALI/ios/Info.plist" > "$BUNDLE/Info.plist"
rm -rf "$BUNDLE/MappleMTLDriver.dSYM"
codesign -s - -f "$BUNDLE"

# the probe that checks the bundle on the phone (ios/mkuserdata.sh copies tests/ next to it)
$CC $TARGET -fobjc-arc -O1 -g "$MALI/ios/vmprobe.m" -framework Foundation -framework Metal -o "$OUT/dist/tests/vmprobe"
codesign -s - -f "$OUT/dist/tests/vmprobe"
log "bundle: $BUNDLE"
