import os, re, sys
# Edits the Mali tree's Metal driver sources so they build against freedreno instead of panfrost.
# usage: prepare-driver.py <mapple_gpu_mali checkout>
gpu_dir = sys.argv[1]

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
        static const driOptionDescription freedreno_driconf[] = {
            DRI_CONF_SECTION_DEBUG
            DRI_CONF_SECTION_END
        };
        static driOptionCache info, options;
        static bool parsed = false;
        if (!parsed) {
            driConfigFileParseParams params = {0};
            params.driverName = "msm";
            driParseOptionInfo(&info, freedreno_driconf, ARRAY_SIZE(freedreno_driconf));
            driParseConfigFiles(&options, &info, &params);
            parsed = true;
        }
        struct pipe_screen_config config = {
            .driver_name_is_inferred = false,
            .options = &options,
            .options_info = &info,
        };
        dev->screen = fd_drm_screen_create_renderonly(dev->fd, NULL, &config);
        dev->panfrost = false;''',
    s, count=1, flags=re.S)
s = s.replace('if (dev->panfrost) {\n        vmg_isa_options iopt;', 'if (0) {\n        vmg_isa_options iopt;')
s = s.replace('if (d->fragment && d->fragment->isa_in)', 'if (0 && d->fragment && d->fragment->isa_in)')
open(p_scr, "w").write(s)

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


# vmg_exec.c: Panfrost's fence struct (pan_fence.h) is only used to read f->syncobj on a path that needs a promise,
# and promises exist only for Panfrost (vmg_surface_promise returns 0 otherwise). Drop the header.
p_ex = f"{gpu_dir}/driver/vmg_exec.c"
e = open(p_ex).read()
e, n = re.subn(r'#include "panfrost/pan_fence.h"[^\n]*\n', '', e, count=1)
assert n == 1, "pan_fence.h include not found in vmg_exec.c"
assert 'f->syncobj' in e
e = e.replace('f->syncobj', '0u')
open(p_ex, "w").write(e)

print("driver sources prepared")
