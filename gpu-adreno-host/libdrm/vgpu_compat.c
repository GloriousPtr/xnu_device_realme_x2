// What the Mali tree's Metal driver still calls (vgpu.h, libdrm-xnu's XNU extensions), served on the msm client (msm_gpu.h).
// The sync/IOSurface extensions have no Adreno counterpart yet: they fail cleanly, and the driver's paths that use
// them are Panfrost-only (dev->panfrost is false).
#include <errno.h>
#include <string.h>
#include <unistd.h>
#include "vgpu.h"
#include "msm_gpu.h"

int vgpu_open(void) { return msm_open(); }
int vgpu_close(int fd) { return msm_close(fd); }
bool vgpu_is_fd(int fd) { return msm_is_fd(fd); }
const char *vgpu_backend_name(void) { return msm_backend_name(); }

void vgpu_counters(uint64_t calls[VGPU_SEL_COUNT], uint64_t *jobs)
{
	memset(calls, 0, sizeof(uint64_t) * VGPU_SEL_COUNT);
	*jobs = 0;
}

int drmSurfacePromiseXNU(int fd, const uint32_t *surface_ids, uint32_t count, uint32_t timeout_ms, uint32_t *handle)
{ (void)fd; (void)surface_ids; (void)count; (void)timeout_ms; (void)handle; return -ENOSYS; }
int drmSyncobjCopyXNU(int fd, uint32_t dst, uint32_t src) { (void)fd; (void)dst; (void)src; return -ENOSYS; }
int drmSyncobjReleaseXNU(int fd, uint32_t handle) { (void)fd; (void)handle; return -ENOSYS; }

// IOSurface import is not implemented in the msm client yet: -1 makes the driver fall back to its copy paths.
int vmg_drm_surface_fd(int device_fd, uint32_t surface_id, unsigned plane) { (void)device_fd; (void)surface_id; (void)plane; return -1; }
int vmg_drm_bo_fd(int device_fd, uint32_t handle) { (void)device_fd; (void)handle; return -1; }
void vmg_drm_surface_fd_close(int fd) { if (fd >= 0) close(fd); }
