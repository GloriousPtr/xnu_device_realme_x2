// SPDX-License-Identifier: GPL-3.0-or-later
#include <errno.h>
#include <stdbool.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <time.h>
#include <IOSurface/IOSurfaceRef.h>
#include "xf86drm.h"
#include "libdrm/drm.h"
#include "../../uapi/sweet_msm.h"
int vgpu_open(void);
int vgpu_ioctl_raw(int fd, uint32_t sel, void *arg, size_t size);
void *vgpu_mmap(int fd, uint64_t token, size_t size);
int vgpu_munmap(int fd, void *addr, size_t size);

int vgpu_open(void);
int vgpu_ioctl_raw(int fd, uint32_t sel, void *arg, size_t size);
void *vgpu_mmap(int fd, uint64_t token, size_t size);
int vgpu_munmap(int fd, void *addr, size_t size);



static int ret_errno(int r) { if (r < 0) { errno = -r; return -1; } return r; }

typedef struct { int fd; uint32_t handle; uint32_t in_page; uint32_t surface, plane; } BOInfo;
static BOInfo g_bo[2048];
static unsigned g_bo_count;
static pthread_mutex_t g_bo_lock = PTHREAD_MUTEX_INITIALIZER;

typedef struct { void *addr; size_t size; int fd; void *base; size_t map_size; uint64_t token; bool idle; } Mapping;
static Mapping *g_maps;
static size_t g_map_count, g_map_cap;
static pthread_mutex_t g_map_lock = PTHREAD_MUTEX_INITIALIZER;

static void map_cache_drop(int fd, uint32_t handle)
{
    pthread_mutex_lock(&g_map_lock);
    for (size_t i = 0; i < g_map_count;) {
        Mapping m = g_maps[i];
        if (m.fd != fd || m.token != handle) { i++; continue; }
        g_maps[i] = g_maps[--g_map_count];
        vgpu_munmap(m.fd, m.base, m.map_size);
    }
    pthread_mutex_unlock(&g_map_lock);
}

static void bo_forget(int fd, uint32_t handle)
{
    pthread_mutex_lock(&g_bo_lock);
    for (unsigned i = 0; i < g_bo_count; i++) {
        if (g_bo[i].fd == fd && g_bo[i].handle == handle) {
            g_bo[i] = g_bo[--g_bo_count];
            break;
        }
    }
    pthread_mutex_unlock(&g_bo_lock);
}

typedef struct { bool used; uint32_t fence; } SoftSyncobj;
static SoftSyncobj g_syncobjs[512];
static pthread_mutex_t g_sync_lock = PTHREAD_MUTEX_INITIALIZER;
static uint32_t g_last_submit_fence[1024];

int drmIoctl(int fd, unsigned long request, void *arg)
{
    unsigned nr = DRM_IOCTL_NR(request);
    if (request == DRM_IOCTL_GEM_CLOSE) {
        uint32_t h = ((struct drm_gem_close *)arg)->handle;
        map_cache_drop(fd, h);
        bo_forget(fd, h);
        return 0;
    }
    if (nr >= DRM_COMMAND_BASE) {
        unsigned msm_cmd = nr - DRM_COMMAND_BASE;
        uint32_t sel = SWEET_MSM_BASE + msm_cmd;
        int r = vgpu_ioctl_raw(fd, sel, arg, (size_t)((request >> 16) & 0x3fffu));
        if (!r && msm_cmd == DRM_MSM_GEM_SUBMIT && fd >= 0 && fd < 1024) {
            g_last_submit_fence[fd] = ((struct drm_msm_gem_submit *)arg)->fence;
        }
        return ret_errno(r);
    }
    errno = ENOSYS;
    return -1;
}

drmVersionPtr drmGetVersion(int fd)
{
    (void)fd;
    drmVersionPtr d = calloc(1, sizeof(*d));
    d->version_major = 1;
    d->version_minor = 10;
    d->version_patchlevel = 0;
    d->name = strdup("msm");
    d->name_len = 3;
    d->date = strdup("20261124");
    d->date_len = 8;
    d->desc = strdup("Qualcomm Adreno 618 (SweetGPU)");
    d->desc_len = (int)strlen(d->desc);
    return d;
}

void drmFreeVersion(drmVersionPtr v)
{
    if (!v) return;
    free(v->name); free(v->date); free(v->desc); free(v);
}

int drmCloseBufferHandle(int fd, uint32_t handle)
{
    map_cache_drop(fd, handle);
    bo_forget(fd, handle);
    return 0;
}

int drmMap(int fd, drm_handle_t handle, drmSize size, drmAddressPtr address)
{
    pthread_mutex_lock(&g_map_lock);
    for (size_t i = 0; i < g_map_count; i++) {
        Mapping *m = &g_maps[i];
        if (m->idle && m->fd == fd && m->token == (uint64_t)handle && m->size == size) {
            m->idle = false;
            *address = m->addr;
            pthread_mutex_unlock(&g_map_lock);
            return 0;
        }
    }
    pthread_mutex_unlock(&g_map_lock);
    uint32_t in_page = 0;
    pthread_mutex_lock(&g_bo_lock);
    for (unsigned i = 0; i < g_bo_count; i++)
        if (g_bo[i].fd == fd && g_bo[i].handle == (uint32_t)handle) { in_page = g_bo[i].in_page; break; }
        pthread_mutex_unlock(&g_bo_lock);
    size_t map_size = size + in_page;
    void *p = vgpu_mmap(fd, (uint64_t)handle, map_size);
    if (!p) return -errno;
    pthread_mutex_lock(&g_map_lock);
    if (g_map_count == g_map_cap) { g_map_cap = g_map_cap ? g_map_cap * 2 : 256; g_maps = realloc(g_maps, g_map_cap * sizeof(*g_maps)); }
    g_maps[g_map_count++] = (Mapping){ (uint8_t *)p + in_page, size, fd, p, map_size, (uint64_t)handle, false };
    pthread_mutex_unlock(&g_map_lock);
    *address = (uint8_t *)p + in_page;
    return 0;
}

int drmUnmap(drmAddress address, drmSize size)
{
    (void)size;
    pthread_mutex_lock(&g_map_lock);
    for (size_t i = 0; i < g_map_count; i++) {
        if (g_maps[i].addr != address || g_maps[i].idle) continue;
        g_maps[i].idle = true;
        pthread_mutex_unlock(&g_map_lock);
        return 0;
    }
    pthread_mutex_unlock(&g_map_lock);
    return -EINVAL;
}

typedef struct { int fd; uint32_t surface, plane; bool is_bo; uint32_t bo_handle; } SurfaceFd;
static SurfaceFd g_surf[256];
static pthread_mutex_t g_surf_lock = PTHREAD_MUTEX_INITIALIZER;

int vmg_drm_surface_fd(int device_fd, uint32_t surface_id, unsigned plane)
{
    (void)device_fd;
    IOSurfaceRef s = IOSurfaceLookup(surface_id);
    if (!s) return -1;
    size_t total = IOSurfaceGetAllocSize(s);
    size_t plane_off = IOSurfaceGetPlaneCount(s)
    ? (size_t)((uint8_t *)IOSurfaceGetBaseAddressOfPlane(s, plane) - (uint8_t *)IOSurfaceGetBaseAddress(s)) : 0;
    size_t size = total - plane_off;
    CFRelease(s);
    char buf[256];
    const char *tmp = getenv("TMPDIR");
    snprintf(buf, sizeof(buf), "%s/vgpu-surface-XXXXXX", tmp ? tmp : "/tmp");
    int fd = mkstemp(buf);
    if (fd < 0) return -1;
    unlink(buf);
    if (ftruncate(fd, (off_t)size)) { close(fd); return -1; }
    pthread_mutex_lock(&g_surf_lock);
    for (unsigned i = 0; i < 256; i++) if (!g_surf[i].fd) { g_surf[i] = (SurfaceFd){ fd, surface_id, plane }; break; }
    pthread_mutex_unlock(&g_surf_lock);
    return fd;
}

int vmg_drm_bo_fd(int device_fd, uint32_t handle)
{
    (void)device_fd;
    int fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    pthread_mutex_lock(&g_surf_lock);
    for (unsigned i = 0; i < 256; i++) if (!g_surf[i].fd) { g_surf[i] = (SurfaceFd){ fd, 0, 0, true, handle }; break; }
    pthread_mutex_unlock(&g_surf_lock);
    return fd;
}

void vmg_drm_surface_fd_close(int fd)
{
    pthread_mutex_lock(&g_surf_lock);
    for (unsigned i = 0; i < 256; i++) if (g_surf[i].fd == fd) g_surf[i].fd = 0;
    pthread_mutex_unlock(&g_surf_lock);
    if (fd >= 0) close(fd);
}

int drmPrimeFDToHandle(int fd, int prime_fd, uint32_t *handle)
{
    SurfaceFd sf = {0};
    pthread_mutex_lock(&g_surf_lock);
    for (unsigned i = 0; i < 256; i++) if (g_surf[i].fd == prime_fd) sf = g_surf[i];
    pthread_mutex_unlock(&g_surf_lock);
    if (!sf.fd) { errno = EBADF; return -EBADF; }
    if (sf.is_bo) { *handle = sf.bo_handle; return 0; }

    pthread_mutex_lock(&g_bo_lock);
    for (unsigned i = 0; i < g_bo_count; i++) {
        if (g_bo[i].fd == fd && g_bo[i].surface == sf.surface && g_bo[i].plane == sf.plane) {
            *handle = g_bo[i].handle;
            pthread_mutex_unlock(&g_bo_lock);
            return 0;
        }
    }
    pthread_mutex_unlock(&g_bo_lock);

    struct sweet_gpu_import_iosurface im = { .surface_id = sf.surface, .plane = sf.plane };
    int r = vgpu_ioctl_raw(fd, SWEET_GPU_SEL_IMPORT_IOSURFACE, &im, sizeof(im));
    if (r) { errno = -r; return r; }
    pthread_mutex_lock(&g_bo_lock);
    if (g_bo_count < 2048) {
        g_bo[g_bo_count++] = (BOInfo){ fd, im.handle, (uint32_t)(im.offset & 0x3fffu), sf.surface, sf.plane };
    }
    pthread_mutex_unlock(&g_bo_lock);
    *handle = im.handle;
    return 0;
}

int drmPrimeHandleToFD(int fd, uint32_t handle, uint32_t flags, int *prime_fd)
{
    (void)fd; (void)handle; (void)flags; (void)prime_fd;
    errno = ENOSYS; return -ENOSYS;
}

int drmSyncobjCreate(int fd, uint32_t flags, uint32_t *handle)
{
    (void)flags;
    pthread_mutex_lock(&g_sync_lock);
    for (uint32_t i = 1; i < 512; i++) {
        if (!g_syncobjs[i].used) {
            g_syncobjs[i] = (SoftSyncobj){ true, (fd >= 0 && fd < 1024) ? g_last_submit_fence[fd] : 0 };
            *handle = i;
            pthread_mutex_unlock(&g_sync_lock);
            return 0;
        }
    }
    pthread_mutex_unlock(&g_sync_lock);
    errno = ENOMEM; return -ENOMEM;
}

int drmSyncobjDestroy(int fd, uint32_t handle)
{
    (void)fd;
    pthread_mutex_lock(&g_sync_lock);
    if (handle > 0 && handle < 512) g_syncobjs[handle].used = false;
    pthread_mutex_unlock(&g_sync_lock);
    return 0;
}

int drmSyncobjWait(int fd, uint32_t *handles, unsigned num_handles, int64_t timeout_nsec, unsigned flags, uint32_t *first_signaled)
{
    (void)flags;
    for (unsigned i = 0; i < num_handles; i++) {
        uint32_t h = handles[i], seq = 0;
        pthread_mutex_lock(&g_sync_lock);
        if (h > 0 && h < 512 && g_syncobjs[h].used) seq = g_syncobjs[h].fence;
        pthread_mutex_unlock(&g_sync_lock);
        if (seq) {
            struct drm_msm_wait_fence w = {
                .fence = seq,
                .timeout = { .tv_sec = timeout_nsec / 1000000000LL, .tv_nsec = timeout_nsec % 1000000000LL },
            };
            int r = vgpu_ioctl_raw(fd, SWEET_MSM_BASE + DRM_MSM_WAIT_FENCE, &w, sizeof(w));
            if (r) { errno = -r; return r; }
        }
        if (first_signaled) *first_signaled = i;
    }
    return 0;
}

int drmSyncobjReset(int fd, const uint32_t *handles, uint32_t count) { (void)fd; (void)handles; (void)count; return 0; }
int drmSyncobjSnapshotXNU(int fd, uint32_t src, uint32_t *handle) { (void)src; return drmSyncobjCreate(fd, 0, handle); }
int drmSyncobjReleaseXNU(int fd, uint32_t handle) { return drmSyncobjDestroy(fd, handle); }
int drmSurfacePromiseXNU(int fd, const uint32_t *surface_ids, uint32_t count, uint32_t timeout_ms, uint32_t *handle) { (void)surface_ids; (void)count; (void)timeout_ms; return drmSyncobjCreate(fd, 0, handle); }
int drmSyncobjCopyXNU(int fd, uint32_t dst, uint32_t src)
{
    (void)fd;
    pthread_mutex_lock(&g_sync_lock);
    if (dst > 0 && dst < 512 && src > 0 && src < 512) g_syncobjs[dst].fence = g_syncobjs[src].fence;
    pthread_mutex_unlock(&g_sync_lock);
    return 0;
}
int drmSyncobjExportSyncFile(int fd, uint32_t handle, int *sync_file_fd) { (void)fd; (void)handle; (void)sync_file_fd; errno = ENOSYS; return -ENOSYS; }
int drmSyncobjImportSyncFile(int fd, uint32_t handle, int sync_file_fd) { (void)fd; (void)handle; (void)sync_file_fd; errno = ENOSYS; return -ENOSYS; }
int drmSyncobjFDToHandle(int fd, int obj_fd, uint32_t *handle) { (void)fd; (void)obj_fd; (void)handle; errno = ENOSYS; return -ENOSYS; }
int drmSyncobjTimelineWait(int fd, uint32_t *handles, uint64_t *points, unsigned num_handles, int64_t timeout_nsec, unsigned flags, uint32_t *first_signaled) { return drmSyncobjWait(fd, handles, num_handles, timeout_nsec, flags, first_signaled); }
int drmSyncobjTransfer(int fd, uint32_t dst_handle, uint64_t dst_point, uint32_t src_handle, uint64_t src_point, uint32_t flags) { (void)dst_point; (void)src_point; (void)flags; return drmSyncobjCopyXNU(fd, dst_handle, src_handle); }

int drmCommandWrite(int fd, unsigned long drmCommandIndex, void *data, unsigned long size)
{
    return ret_errno(vgpu_ioctl_raw(fd, (uint32_t)(SWEET_MSM_BASE + drmCommandIndex), data, (size_t)size));
}

int drmCommandWriteRead(int fd, unsigned long drmCommandIndex, void *data, unsigned long size)
{
    return ret_errno(vgpu_ioctl_raw(fd, (uint32_t)(SWEET_MSM_BASE + drmCommandIndex), data, (size_t)size));
}

int drmGetCap(int fd, uint64_t capability, uint64_t *value)
{
    (void)fd;
    if (!value) { errno = EINVAL; return -1; }
    *value = (capability == 0x13 || capability == 0x14) ? 1 : 0;
    return 0;
}

int drmOpenWithType(const char *name, const char *busid, int type)
{
    (void)name; (void)busid; (void)type;
    int fd = vgpu_open();
    return ret_errno(fd);
}
