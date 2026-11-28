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

# Write self-contained vgpu/vgpu.c so all _vgpu_* symbols are guaranteed present
open(f"{gpu_dir}/vgpu/vgpu.c", "w").write("""
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

enum vgpu_selector { VGPU_SEL_DUMMY = 0 };
struct vgpu_backend {
    const char *name;
    int (*open)(void **conn);
    void (*close)(void *conn);
    int (*ioctl)(void *conn, enum vgpu_selector sel, void *arg, size_t size);
    void *(*mmap)(void *conn, uint64_t token, size_t size);
    int (*munmap)(void *conn, void *addr, size_t size);
};
extern const struct vgpu_backend vgpu_backend_iokit;

#define MAX_CONNS 256
typedef struct {
    bool used;
    uint64_t id;
    dev_t dev;
    ino_t ino;
    void *conn;
} ConnEntry;

static ConnEntry g_conns[MAX_CONNS];
static uint64_t g_next_id = 1;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static _Atomic uint64_t g_cnt[24], g_jobs;

const char *vgpu_backend_name(void) { return vgpu_backend_iokit.name; }

static int open_anon_fd(struct stat *out_st)
{
    char buf[256];
    const char *tmp = getenv("TMPDIR");
    snprintf(buf, sizeof(buf), "%s/vgpu-dev-XXXXXX", tmp ? tmp : "/tmp");
    int fd = mkstemp(buf);
    if (fd < 0) {
        snprintf(buf, sizeof(buf), "/tmp/vgpu-dev-XXXXXX");
        fd = mkstemp(buf);
    }
    if (fd < 0) return -errno;
    unlink(buf);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    if (fstat(fd, out_st) < 0) {
        int e = errno;
        close(fd);
        return -e;
    }
    return fd;
}

int vgpu_open(void)
{
    void *conn = NULL;
    int err = vgpu_backend_iokit.open(&conn);
    if (err) return err;

    struct stat st;
    int fd = open_anon_fd(&st);
    if (fd < 0) {
        vgpu_backend_iokit.close(conn);
        return fd;
    }

    pthread_mutex_lock(&g_lock);
    int slot = -1;
    for (int i = 0; i < MAX_CONNS; i++) {
        if (!g_conns[i].used) { slot = i; break; }
    }
    if (slot < 0) {
        pthread_mutex_unlock(&g_lock);
        close(fd);
        vgpu_backend_iokit.close(conn);
        return -EMFILE;
    }
    g_conns[slot] = (ConnEntry){
        .used = true,
        .id = g_next_id++,
        .dev = st.st_dev,
        .ino = st.st_ino,
        .conn = conn,
    };
    pthread_mutex_unlock(&g_lock);
    return fd;
}

static void *lookup_entry(int fd, uint64_t *out_id)
{
    if (fd < 0) return NULL;
    struct stat st;
    if (fstat(fd, &st) < 0) return NULL;

    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < MAX_CONNS; i++) {
        if (g_conns[i].used && g_conns[i].dev == st.st_dev && g_conns[i].ino == st.st_ino) {
            void *c = g_conns[i].conn;
            if (out_id) *out_id = g_conns[i].id;
            pthread_mutex_unlock(&g_lock);
            return c;
        }
    }
    pthread_mutex_unlock(&g_lock);
    return NULL;
}

static void *lookup(int fd) { return lookup_entry(fd, NULL); }

uint64_t vgpu_conn_id(int fd)
{
    uint64_t id = 0;
    lookup_entry(fd, &id);
    return id;
}

bool vgpu_is_fd(int fd) { return lookup(fd) != NULL; }

int vgpu_close(int fd)
{
    if (fd < 0) return -EBADF;
    struct stat st;
    if (fstat(fd, &st) < 0) return -EBADF;

    close(fd);

    /* Check if any dup()'d fd still references the same inode */
    bool still_open = false;
    for (int other = 0; other < 1024; other++) {
        struct stat ost;
        if (fstat(other, &ost) == 0 && ost.st_dev == st.st_dev && ost.st_ino == st.st_ino) {
            still_open = true;
            break;
        }
    }

    void *conn = NULL;
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < MAX_CONNS; i++) {
        if (g_conns[i].used && g_conns[i].dev == st.st_dev && g_conns[i].ino == st.st_ino) {
            if (!still_open) {
                conn = g_conns[i].conn;
                g_conns[i].used = false;
                g_conns[i].conn = NULL;
            }
            break;
        }
    }
    pthread_mutex_unlock(&g_lock);

    if (conn) vgpu_backend_iokit.close(conn);
    return 0;
}

void vgpu_counters(uint64_t calls[32], uint64_t *jobs)
{
    for (unsigned i = 0; i < 24; i++) calls[i] = atomic_load_explicit(&g_cnt[i], memory_order_relaxed);
    *jobs = atomic_load_explicit(&g_jobs, memory_order_relaxed);
}

int vgpu_ioctl(int fd, enum vgpu_selector sel, void *arg)
{
    void *conn = lookup(fd);
    if (!conn || !arg) return conn ? -EINVAL : -EBADF;
    return vgpu_backend_iokit.ioctl(conn, sel, arg, 0);
}

int vgpu_ioctl_raw(int fd, uint32_t sel, void *arg, size_t size)
{
    void *conn = lookup(fd);
    if (!conn) return -EBADF;
    if (sel == 0x46) atomic_fetch_add_explicit(&g_jobs, 1, memory_order_relaxed);
    return vgpu_backend_iokit.ioctl(conn, (enum vgpu_selector)sel, arg, size);
}

void *vgpu_mmap(int fd, uint64_t token, size_t size)
{
    void *conn = lookup(fd);
    if (!conn) { errno = EBADF; return NULL; }
    return vgpu_backend_iokit.mmap(conn, token, size);
}

int vgpu_munmap(int fd, void *addr, size_t size)
{
    void *conn = lookup(fd);
    if (!conn) return -EBADF;
    return vgpu_backend_iokit.munmap(conn, addr, size);
}
""")


# Update vmg_screen.c: use fd_drm_screen_create_renderonly + disable Valhall ISA runtime branch
p_scr = f"{gpu_dir}/driver/vmg_screen.c"
s = open(p_scr).read()
decls = (
    '#include "freedreno/drm/freedreno_drm_public.h"\n'
    'struct renderonly;\n'
    'struct pipe_screen *fd_drm_screen_create_renderonly(int fd, struct renderonly *ro, const struct pipe_screen_config *config);\n'
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
        dev->screen = fd_drm_screen_create_renderonly(dev->fd, NULL, &config);
        dev->panfrost = true;''',
    s, count=1, flags=re.S)
s = s.replace('if (dev->panfrost) {\n        vmg_isa_options iopt;', 'if (0) {\n        vmg_isa_options iopt;')
s = s.replace('if (d->fragment && d->fragment->isa_in)', 'if (0 && d->fragment && d->fragment->isa_in)')
open(p_scr, "w").write(s)

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
void vmg_isa_counters(uint64_t *a, uint64_t *b, uint64_t *c, char *d, size_t e) { (void)a; (void)b; (void)c; if (d && e) d[0] = 0; }
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
    src/freedreno/fdl/libfreedreno_layout.a
    src/freedreno/isa/libir3encode.a
    src/freedreno/ir2/libfreedreno_ir2.a
    src/gallium/winsys/freedreno/drm/libfreedrenowinsys.a
    src/gallium/drivers/freedreno/libfreedreno.a
  "'''
assert old_targets in b, "targets block not found in ios/build.sh"
b = b.replace(old_targets, new_targets, 1)
b = b.replace('"$GPU/mesa/out/macos/cflags.txt"', '"$OUT/cflags.txt"')
b = b.replace('mapple_gpu_uapi.h', 'sweet_msm.h')
b = b.replace('"$GPU/vgpu/vgpu_mock.c" ', '')
b = b.replace('-I"$GPU/driver/metal" -I"$GPU/driver"', '-I"$GPU/driver/metal" -I"$GPU/driver"')
open(p_sh, "w").write(b)

print("Prepared mapple_gpu_adreno successfully!")
