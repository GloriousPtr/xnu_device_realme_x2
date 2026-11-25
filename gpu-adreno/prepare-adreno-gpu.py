import os, re, shutil, sys

gpu_dir = sys.argv[1]
overlay = os.path.dirname(os.path.abspath(__file__))

# Replace patches, uapi, vgpu_iokit.c, xf86drm_xnu.c
for f in os.listdir(f"{gpu_dir}/mesa/patches"):
    os.remove(f"{gpu_dir}/mesa/patches/{f}")
shutil.copy(f"{overlay}/0001-freedreno-xnu.patch", f"{gpu_dir}/mesa/patches/0001-freedreno-xnu.patch")
shutil.copy(f"{overlay}/sweet_msm.h", f"{gpu_dir}/uapi/sweet_msm.h")
shutil.copy(f"{overlay}/vgpu_iokit.c", f"{gpu_dir}/vgpu/vgpu_iokit.c")
shutil.copy(f"{overlay}/xf86drm_xnu.c", f"{gpu_dir}/libdrm-xnu/src/xf86drm_xnu.c")

# Update vmg_screen.c: use fd_drm_screen_create + disable Valhall ISA path
p_scr = f"{gpu_dir}/driver/vmg_screen.c"
s = open(p_scr).read()
s = s.replace('#include "panfrost/pan_public.h"',
              '#include "freedreno/drm/freedreno_drm_public.h"\n'
              'void panfrost_screen_bo_cache_empty(struct pipe_screen *screen) { (void)screen; }\n'
              'size_t panfrost_screen_bo_cache_bytes(struct pipe_screen *screen) { (void)screen; return 0; }')
s = s.replace('#include "isa/vmg_isa.h"\n#include "isa/vmg_isa_api.h"', '')
s = s.replace('if (!which) which = "panfrost";', 'if (!which) which = "freedreno";')
s = re.sub(
    r'if \(!strcmp\(which, "panfrost"\)\) \{.*?g_gpu_product_id = 0x9093;',
    '''if (!strcmp(which, "freedreno") || !strcmp(which, "panfrost")) {
        dev->fd = vgpu_open();
        if (dev->fd < 0) {
            snprintf(error, error_size, "vgpu_open failed (%s back-end): %s", vgpu_backend_name(), strerror(-dev->fd));
            free(dev);
            return NULL;
        }
        struct pipe_screen_config config = { 0 };
        dev->screen = fd_drm_screen_create(dev->fd, NULL, &config);
        dev->panfrost = true;''',
    s, count=1, flags=re.S)
s = s.replace('if (dev->panfrost) {\n        vmg_isa_options iopt;', 'if (0) {\n        vmg_isa_options iopt;')
s = s.replace('if (d->fragment && d->fragment->isa_in)', 'if (0 && d->fragment && d->fragment->isa_in)')
s = s.replace('vmg_isa_program_free(sh->isa);', '')
s = s.replace('vmg_isa_program_free(p->fs_variant_isa);', '')
open(p_scr, "w").write(s)

# Update ios/build.sh: build Mesa with freedreno for iOS, skip panfrost_compile and driver/isa/*.c
p_sh = f"{gpu_dir}/ios/build.sh"
b = open(p_sh).read()
b = b.replace('  [ -x "$GPU/mesa/build-macos/src/panfrost/clc/panfrost_compile" ] || bash "$GPU/mesa/build.sh" macos-nosmoke\n',
              '  bash "$GPU/mesa/build.sh" fn setup_venv fetch_and_patch\n')
b = b.replace('-Dgallium-drivers=panfrost', '-Dgallium-drivers=freedreno')
b = b.replace('-Dmesa-clc=system -Dprecomp-compiler=system', '-Dmesa-clc=auto -Dprecomp-compiler=auto')
old_targets = '''  local targets
  targets="$(bash "$GPU/mesa/build.sh" fn mesa_libs 2>/dev/null | grep -v -i 'llvmpipe\\|gallivm\\|null_sw' || true)"'''
new_targets = '''  local targets="
    src/c11/impl/libmesa_util_c11.a
    src/util/libmesa_util.a
    src/util/libmesa_util_simd.a
    src/util/blake3/libblake3.a
    src/util/libxmlconfig.a
    src/util/libparson.a
    src/compiler/libcompiler.a
    src/compiler/nir/libnir.a
    src/compiler/spirv/libvtn.a
    src/compiler/isaspec/libisaspec.a
    src/gallium/auxiliary/libgallium.a
    src/freedreno/common/libfreedreno_common.a
    src/freedreno/drm/libfreedreno_drm.a
    src/freedreno/ir3/libfreedreno_ir3.a
    src/freedreno/perfcntrs/libfreedreno_perfcntrs.a
    src/gallium/winsys/freedreno/drm/libfreedrenowinsys.a
    src/gallium/drivers/freedreno/libfreedreno.a
  "'''
assert old_targets in b, "targets block not found in ios/build.sh"
b = b.replace(old_targets, new_targets, 1)
b = b.replace('"$GPU/mesa/out/macos/cflags.txt"', '"$OUT/cflags.txt"')
b = b.replace('  # Direct AIR -> Valhall ISA compiler (driver/isa)\n', '  if false; then\n')
b = b.replace('  if stale "$OBJ/vmg_formats.o"', '  fi\n  if stale "$OBJ/vmg_formats.o"')
b = b.replace('mapple_gpu_uapi.h', 'sweet_msm.h')
open(p_sh, "w").write(b)

# Add vgpu_ioctl_raw to vgpu.h and vgpu.c for Adreno msm selectors (0x40..0x73)
p_vh = f"{gpu_dir}/vgpu/vgpu.h"
open(p_vh, "w").write(open(p_vh).read().replace(
    "int vgpu_ioctl(int fd, enum vgpu_selector sel, void *arg);",
    "int vgpu_ioctl(int fd, enum vgpu_selector sel, void *arg);\n"
    "int vgpu_ioctl_raw(int fd, uint32_t sel, void *arg, size_t size);"))
p_vc = f"{gpu_dir}/vgpu/vgpu.c"
open(p_vc, "w").write(open(p_vc).read().replace(
    "void *vgpu_mmap(int fd, uint64_t token, size_t size)",
    "int vgpu_ioctl_raw(int fd, uint32_t sel, void *arg, size_t size)\n{\n"
    "    void *conn = lookup(fd);\n"
    "    if (!conn) return -EBADF;\n"
    "    return backend()->ioctl(conn, (enum vgpu_selector)sel, arg, size);\n"
    "}\n\n"
    "void *vgpu_mmap(int fd, uint64_t token, size_t size)"))


open(f"{gpu_dir}/vgpu/vgpu_mock.c", "w").write(
    '#include "vgpu.h"\n'
    'const struct vgpu_backend vgpu_backend_mock = { "mock", NULL, NULL, NULL, NULL, NULL };\n'
)

print("Prepared mapple_gpu_adreno successfully!")
