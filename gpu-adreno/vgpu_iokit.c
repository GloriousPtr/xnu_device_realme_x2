// SPDX-License-Identifier: GPL-3.0-or-later
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include "../uapi/sweet_msm.h"
#include "iokit_compat.h"

enum vgpu_selector { VGPU_SEL_DUMMY = 0 };
struct vgpu_backend {
    const char *name;
    int (*open)(void **conn);
    void (*close)(void *conn);
    int (*ioctl)(void *conn, enum vgpu_selector sel, void *arg, size_t size);
    void *(*mmap)(void *conn, uint64_t token, size_t size);
    int (*munmap)(void *conn, void *addr, size_t size);
};

typedef struct {
    io_connect_t conn;
    pthread_mutex_t lock;
    struct { mach_vm_address_t addr; uint32_t type; } *maps;
    size_t mapCount, mapCap;
} IOKitConn;

static int errno_for(kern_return_t kr)
{
    switch (kr) {
    case kIOReturnSuccess: return 0;
    case kIOReturnNoMemory: case kIOReturnNoResources: return -ENOMEM;
    case kIOReturnBadArgument: return -EINVAL;
    case kIOReturnNotPrivileged: case kIOReturnNotPermitted: return -EPERM;
    case kIOReturnNotFound: case kIOReturnNoDevice: return -ENOENT;
    case kIOReturnTimeout: return -ETIME;
    case kIOReturnBusy: return -EBUSY;
    case kIOReturnUnsupported: return -ENOTSUP;
    case kIOReturnAborted: return -EINTR;
    default: return -EIO;
    }
}

static int iokit_open(void **out)
{
    io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("SweetGPU"));
    if (!svc) return -ENODEV;
    io_connect_t conn = MACH_PORT_NULL;
    kern_return_t kr = IOServiceOpen(svc, mach_task_self(), 0x76677075u, &conn);
    IOObjectRelease(svc);
    if (kr != kIOReturnSuccess) return errno_for(kr);

    struct sweet_gpu_abi_info info = {0};
    size_t sz = sizeof(info);
    kr = IOConnectCallStructMethod(conn, SWEET_GPU_SEL_INFO, NULL, 0, &info, &sz);
    if (kr != kIOReturnSuccess || info.abi != SWEET_GPU_ABI) {
        IOServiceClose(conn);
        return kr != kIOReturnSuccess ? errno_for(kr) : -EPROTO;
    }
    IOKitConn *c = calloc(1, sizeof(*c));
    c->conn = conn;
    pthread_mutex_init(&c->lock, NULL);
    *out = c;
    return 0;
}

static void iokit_close(void *conn)
{
    IOKitConn *c = conn;
    IOServiceClose(c->conn);
    free(c->maps);
    free(c);
}

static int iokit_ioctl(void *conn, enum vgpu_selector sel, void *arg, size_t size)
{
    IOKitConn *c = conn;
    size_t out = size;
    return errno_for(IOConnectCallStructMethod(c->conn, (uint32_t)sel, arg, size, arg, &out));
}

static void *iokit_mmap(void *conn, uint64_t token, size_t size)
{
    IOKitConn *c = conn;
    mach_vm_address_t addr = 0;
    mach_vm_size_t len = size;
    kern_return_t kr = IOConnectMapMemory64(c->conn, (uint32_t)token, mach_task_self(), &addr, &len, kIOMapAnywhere);
    if (kr != kIOReturnSuccess) { errno = -errno_for(kr); return NULL; }
    pthread_mutex_lock(&c->lock);
    if (c->mapCount == c->mapCap) {
        c->mapCap = c->mapCap ? c->mapCap * 2 : 64;
        c->maps = realloc(c->maps, c->mapCap * sizeof(*c->maps));
    }
    c->maps[c->mapCount].addr = addr;
    c->maps[c->mapCount].type = (uint32_t)token;
    c->mapCount++;
    pthread_mutex_unlock(&c->lock);
    return (void *)(uintptr_t)addr;
}

static int iokit_munmap(void *conn, void *p, size_t size)
{
    IOKitConn *c = conn;
    (void)size;
    mach_vm_address_t addr = (mach_vm_address_t)(uintptr_t)p;
    pthread_mutex_lock(&c->lock);
    for (size_t i = 0; i < c->mapCount; i++) {
        if (c->maps[i].addr != addr) continue;
        uint32_t type = c->maps[i].type;
        c->maps[i] = c->maps[--c->mapCount];
        pthread_mutex_unlock(&c->lock);
        return errno_for(IOConnectUnmapMemory64(c->conn, type, mach_task_self(), addr));
    }
    pthread_mutex_unlock(&c->lock);
    return -EINVAL;
}

const struct vgpu_backend vgpu_backend_iokit = { "iokit", iokit_open, iokit_close, iokit_ioctl, iokit_mmap, iokit_munmap };
