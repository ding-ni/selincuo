"""Recycle with the Windows Shell; never permit a permanent-delete fallback."""
import os
import uuid


def recycle(path):
    if os.name != 'nt':
        raise RuntimeError('此操作需要Windows回收站')
    import ctypes as c
    from ctypes import wintypes as w
    guid = lambda value: (c.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)
    ole, shell = c.OleDLL('ole32'), c.WinDLL('shell32')
    ole.CoInitializeEx.argtypes = [c.c_void_p, w.DWORD]
    ole.CoInitializeEx.restype = c.c_long
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0:
        raise RuntimeError('无法初始化Windows文件操作服务')
    operation, item = c.c_void_p(), c.c_void_p()

    def invoke(pointer, index, types=(), args=()):
        table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
        result = c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(table[index])(pointer, *args)
        if result < 0:
            raise RuntimeError('Windows回收站操作未完成（0x%08X）' % (result & 0xffffffff))
        return result

    try:
        clsid = guid('3ad05575-8857-4850-9277-11b85bdb8e09')
        iid = guid('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')
        ole.CoCreateInstance.argtypes = [c.c_void_p, c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(c.c_void_p)]
        ole.CoCreateInstance.restype = c.c_long
        if ole.CoCreateInstance(c.byref(clsid), None, 1, c.byref(iid), c.byref(operation)) < 0:
            raise RuntimeError('Windows文件操作服务不可用，文件保持原位')
        shell.SHCreateItemFromParsingName.argtypes = [w.LPCWSTR, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
        shell.SHCreateItemFromParsingName.restype = c.c_long
        shell_iid = guid('43826d1e-e718-42ee-bc55-a1e261c37bfe')
        if shell.SHCreateItemFromParsingName(str(path), None, c.byref(shell_iid), c.byref(item)) < 0:
            raise RuntimeError('Windows无法读取所选成果目录')
        # FOFX_RECYCLEONDELETE and FOFX_EARLYFAILURE require recycling. A
        # disabled/full/unsupported bin must fail, never silently erase data.
        flags = 0x00080000 | 0x00100000 | 0x0040 | 0x0400 | 0x0010 | 0x0004
        invoke(operation, 5, (w.DWORD,), (flags,))
        invoke(operation, 18, (c.c_void_p, c.c_void_p), (item, None))
        invoke(operation, 21)
        aborted = w.BOOL()
        invoke(operation, 22, (c.POINTER(w.BOOL),), (c.byref(aborted),))
        if aborted.value:
            raise RuntimeError('Windows取消了回收操作，平台记录仍保留')
    finally:
        if item:
            invoke(item, 2)
        if operation:
            invoke(operation, 2)
        ole.CoUninitialize()
