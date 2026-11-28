// msm_gpu.c's IOKit use on iOS, whose SDK has no IOKit headers: the Mali tree's iokit_compat.h (found with -I<tree>/vgpu)
// plus the two things msm_gpu.c needs that it does not declare. On macOS the real header is used.
#pragma once
#include "iokit_compat.h"
#ifndef kIOReturnNotReady
#define kIOReturnNotReady iokit_common_err(0x2d0)
#endif
#if !__has_include(<IOKit/IOKitLib.h>)
#ifdef __cplusplus
extern "C"
#endif
kern_return_t IOConnectCallScalarMethod(mach_port_t connection, uint32_t selector, const uint64_t *input,
                                        uint32_t inputCnt, uint64_t *output, uint32_t *outputCnt);
#endif
