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

# Update vmg_screen.c: use fd_drm_screen_create + disable Valhall ISA runtime branch
p_scr = f"{gpu_dir}/driver/vmg_screen.c"
s = open(p_scr).read()
decls = (
    '#include "freedreno/drm/freedreno_drm_public.h"\n'
    'struct renderonly;\n'
    'struct pipe_screen *fd_drm_screen_create(int fd, struct renderonly *ro, const struct pipe_screen_config *config);\n'
    'int vgpu_open(void);\n'
    'int vgpu_close(int fd);\n'
    'const char *vgpu_backend_name(void);\n'
    'void panfrost_screen_bo_cache_empty(struct pipe_screen *screen) { (void)screen; }\n'
    'size_t panfrost_screen_bo_cache_bytes(struct pipe_screen *screen) { (void)screen; return 0; }\n'
)
assert '#include "panfrost/pan_public.h"' in s
s = s.replace('#include "panfrost/pan_public.h"', decls, 1)
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
open(p_scr, "w").write(s)

# Update ios/build.sh: build Mesa with freedreno for iOS, skip panfrost_compile
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
b = b.replace('mapple_gpu_uapi.h', 'sweet_msm.h')
b = b.replace('"$GPU/vgpu/vgpu_mock.c" ', '')
b = b.replace('-I"$GPU/driver/metal" -I"$GPU/driver"', '-include "$GPU/driver/metal/vgpu_shim.h" -I"$GPU/driver/metal" -I"$GPU/driver"')
b = b.replace('  local ih=("$GPU"/driver/isa/*.h)', '  local ih=("$GPU"/driver/isa/*.h)')
b = b.replace('  local ih=("$GPU"/driver/isa/*.h)', '  local ih=("$GPU"/driver/isa/*.h)')
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
open(p_vc, "w").write(open(p_vc).read().replace("&vgpu_backend_mock", "&vgpu_backend_iokit"))


# Replace driver/isa/*.c with a single clean stub file
for f in os.listdir(f"{gpu_dir}/driver/isa"):
    if f.endswith(".c"):
        os.remove(f"{gpu_dir}/driver/isa/{f}")
open(f"{gpu_dir}/driver/isa/vmg_isa_stub.c", "w").write("""
#include "vmg_isa.h"
#include "vmg_isa_api.h"
#include <string.h>
const char *const vmg_isa_feature_name[VMG_ISA_FEATURE_COUNT] = { 0 };
void vmg_isa_options_from_env(vmg_isa_options *o) { memset(o, 0, sizeof(*o)); }
void vmg_isa_options_key(const vmg_isa_options *o, char *buf, size_t size) { (void)o; if (size) buf[0] = 0; }
void vmg_isa_get_stats(vmg_isa_stats *out) { memset(out, 0, sizeof(*out)); }
struct vmg_isa_program *vmg_isa_compile(const vmg_air_input *in, const vmg_isa_options *opt, uint64_t gpu_id, uint32_t attached,
                                        vmg_reflection *refl, struct util_dynarray *pool, char **error) {
    (void)in; (void)opt; (void)gpu_id; (void)attached; (void)refl; (void)pool; (void)error; return NULL;
}
void vmg_isa_program_free(struct vmg_isa_program *p) { (void)p; }
struct nir_shader *vmg_isa_shell(struct vmg_isa_program *p, const struct nir_shader_compiler_options *options, const vmg_reflection *refl) {
    (void)p; (void)options; (void)refl; return NULL;
}
void vmg_isa_install_hooks(void) {}
const char *vmg_isa_program_name(const struct vmg_isa_program *p) { (void)p; return ""; }
int vmg_isa_program_stage(const struct vmg_isa_program *p) { (void)p; return 0; }
uint64_t vmg_isa_program_features(const struct vmg_isa_program *p) { (void)p; return 0; }
const void *vmg_isa_program_binary(const struct vmg_isa_program *p, size_t *size) { (void)p; if (size) *size = 0; return NULL; }
const struct pan_shader_info *vmg_isa_program_info(const struct vmg_isa_program *p) { (void)p; return NULL; }
void *vmg_isa_program_serialize(const struct vmg_isa_program *p, size_t *size) { (void)p; if (size) *size = 0; return NULL; }
struct vmg_isa_program *vmg_isa_program_deserialize(const void *data, size_t size) { (void)data; (void)size; return NULL; }
void vmg_isa_note(bool ok, const char *fn, const char *why, uint64_t us, bool cached) { (void)ok; (void)fn; (void)why; (void)us; (void)cached; }
void vmg_isa_set_input_hook(vmg_isa_input_hook h) { (void)h; }
void vmg_isa_program_set_input(struct vmg_isa_program *p, void *input) { (void)p; (void)input; }
""")


# Provide VGPU_SEL_* constants and vgpu_counters declaration for VMCommands.m / VMShmStats.m
open(f"{gpu_dir}/driver/metal/vgpu_shim.h", "w").write("""
#pragma once
#include <stdint.h>
enum {
    VGPU_SEL_CREATE_BO = 1,
    VGPU_SEL_GEM_CLOSE = 2,
    VGPU_SEL_WAIT_BO = 3,
    VGPU_SEL_SYNCOBJ_WAIT = 4,
    VGPU_SEL_IMPORT_IOSURFACE = 5,
    VGPU_SEL_COUNT = 32
};
void vgpu_counters(uint64_t calls[VGPU_SEL_COUNT], uint64_t *jobs);
""")

print("Prepared mapple_gpu_adreno successfully!")
