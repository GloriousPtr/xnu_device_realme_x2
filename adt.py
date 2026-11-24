# The realme X2 (RMX1992, 8 GB) device tree values, for build/mapple/tools/mkadt.py.

# Upper DDR bank (live FDT memory <0x01 0x80000000 0x00 0xfbb00000>); all fixed
# reserved-memory carve-outs sit below 0x100000000 in the lower bank.
RAM = (0x180000000, 0xfbb00000)
# ABL's splash framebuffer (reserved-memory cont_splash_region@9c000000, size 0x1700000).
FB = dict(base=0x9c000000, size=0x1700000, width=1080, height=2340, stride=1080 * 4)
ECID = 0x5357454554000002  # 'SWEET' + 2, synthetic
PRODUCT_NAME = 'realme X2'
DISPLAY_GAMUT = 'P3'
ABOUT = dict(serial='MAPPLE0000000000', model='RMX1992', region='', regulatory='RMX1992')


def devices(a, ph, phandle, gic_ph, ecid, flags):
    """/arm-io devices: none; the kernel line's drivers find theirs by address."""
    return []
