"""原生目录选择框：让"打开目录"变成真的弹窗点选，不用手输路径。

## 为什么必须是后端弹框

浏览器**拿不到**选中文件/目录的绝对路径——这是规范层面的隐私设计：
`File` 对象上只有 name/size/type，选目录时多的 `webkitRelativePath` 也只是
相对路径。前端拿不到绝对路径，就没法让 sidecar"就地引用"那个目录，只能
把内容复制一份传上来。可这个服务本来就跑在用户本机（127.0.0.1），
**后端有能力**弹一个真正的系统选择框，把绝对路径原样带回来——
"就地引用、不复制"这个语义才成立。

## 为什么在 Windows 上不用 tkinter 的 askdirectory（踩过的坑）

Tk 的 `askdirectory` 在 Windows 上走的是**老掉牙的 `SHBrowseForFolder`**：
一棵纯文件夹树，**不显示文件**——用户会以为"没打开文件管理器"，而且它挂在
被 `withdraw()` 的隐藏父窗口下时，任务管理器里会看到进程状态变成
"未响应"（实测）。用户的原话是"选择了后没有显示哪个文件"，说的就是这个。

所以 Windows 走 COM 的 `IFileOpenDialog` + `FOS_PICKFOLDERS`：这是 Vista 起
资源管理器自己用的那个框——左边导航栏、中间**显示当前目录里的内容**、
下面"选择文件夹"按钮。跟"打开文件管理器选"是同一个东西。

（非 Windows 仍保留 tkinter 那条路，那边 Tk 就是原生对话框。）

## 为什么用子进程，而不是在本进程里起 GUI/COM

1. HTTP 请求跑在 ThreadingHTTPServer 的工作线程里。在工作线程里建 Tk 是
   Windows 上的已知坑；COM 的 `CoInitializeEx(APARTMENTTHREADED)` 也要求
   调用线程自己有个像样的消息循环。
2. 子进程有自己的主线程，用完即退；**对话框崩了也带不倒服务**。
3. 弹不出来（无图形环境 / COM 不可用）只是子进程非零退出，父进程照样活着，
   可以干净地降级回"手输路径"。

## 契约

    pick_directory()  ->  str       用户选中的绝对路径
                          None      用户取消
    DialogUnavailable               这套机制在这台机器上根本用不了
                                    （调用方据此降级，别当成错误弹给用户）

子进程用一行 `RESULT:<路径>` 回话，而不是只靠 stdout 裸输出——GUI/COM 库
自己会往 stdout/stderr 吐东西，裸读会把噪声当路径。
"""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

# 子进程回话前缀（父进程只认这一行）
_RESULT_PREFIX = "RESULT:"
# 子进程退出码：2 = 这台机器上没有可用的对话框机制
_EXIT_UNAVAILABLE = 2
# 等用户挑多久算超时。故意给得很宽松——挑目录时人是会翻半天的，
# 这里的超时只是防"对话框卡死在后台永远不返回"，不是催用户。
_DIALOG_TIMEOUT_SECONDS = 900
_TITLE = "让 Agent 在哪个目录里工作？（直接引用，不复制）"


class DialogUnavailable(RuntimeError):
    """这台机器上弹不出原生选择框（调用方应降级为手输路径）。"""


def _trace(message: str) -> None:
    """分段打点，写到 `WYWD_DIALOG_TRACE` 指定的文件（没设就什么都不做）。

    为什么需要它：服务端弹框这条路上，"卡住"是不会有输出的——子进程在
    模态 Show() 里不出来，父进程在 subprocess.run 里等，谁都不吭声。
    出问题时唯一能问到"它走到哪一步了"的办法，就是让子进程自己沿途记一笔。

    刻意用**文件 + 立即 flush**（不是 stderr）：父进程是 capture_output，
    管道里的东西要等子进程退出才读得到；而我们要查的正是"它没退出"。
    """

    path = os.environ.get("WYWD_DIALOG_TRACE")
    if not path:
        return
    try:
        import time
        with open(path, "a", encoding="utf-8") as sink:
            sink.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {message}\n")
    except OSError:
        pass


def _whereami() -> str:
    """"我在哪个窗口站/桌面"——看不到屏幕时，这是判断"弹得出来吗"的第一手。"""

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        def name_of(handle) -> str:
            buf = ctypes.create_unicode_buffer(512)
            need = ctypes.c_ulong()
            ok = user32.GetUserObjectInformationW(
                handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))
            return buf.value if ok else f"<err {ctypes.get_last_error()}>"

        desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        return (f"winsta={name_of(user32.GetProcessWindowStation())} "
                f"desktop={name_of(desktop)} "
                f"screen={user32.GetSystemMetrics(0)}")
    except Exception as exc:                 # 诊断本身绝不能把主流程带崩
        return f"<探不到: {exc}>"


def _interpreter() -> str:
    """优先 pythonw.exe：它没有控制台，不会在弹框前先闪一个黑窗。"""

    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


def available() -> tuple[bool, str]:
    """探一下这套机制能不能用（不弹框）。返回 (可用?, 原因)。"""

    if os.name == "nt":
        try:
            import ctypes
            ctypes.WinDLL("ole32")
            ctypes.WinDLL("shell32")
        except (ImportError, OSError) as exc:
            return False, f"Windows 的 COM/Shell 库加载不了：{exc}"
        return True, "Windows：资源管理器式文件夹选择框（IFileOpenDialog）"

    try:
        import tkinter  # noqa: F401
    except ImportError as exc:                      # 精简版解释器没带 tkinter
        return False, f"这台机器的 Python 没带 tkinter：{exc}"
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "没有图形显示环境（DISPLAY 未设置）——服务像跑在服务器上"
    return True, "用 Tk 的目录选择框"


def pick_directory(initial: str = "",
                   selftest: bool = False,
                   flash_ms: int = 0) -> str | None:
    """弹一个系统目录选择框，返回选中的绝对路径；用户取消返回 None。

    selftest=True：子进程**不建窗**，直接把 initial 解析后回传——只验父进程
        这一侧（起进程 + 协议 + 路径解析）。
    flash_ms>0：子进程真弹框，但起个后台线程在 N 毫秒后把它关掉。这样
        "起进程 → COM → 真弹出窗口 → 关闭 → 回话"整条链都能无人值守验到。
        真弹框那一下仍要人工点一次确认观感（框长什么样、能不能选）。

    raises DialogUnavailable —— 机制不可用（调用方降级）
    raises TimeoutError     —— 对话框卡住了（不该发生，但别让服务干等）
    """

    can, reason = available()
    if not can:
        raise DialogUnavailable(reason)

    args = [_interpreter(), str(Path(__file__).resolve()), "--child"]
    if initial:
        args.append(str(initial))
    if selftest:
        args.append("--selftest")
    if flash_ms:
        args.append(f"--flash-ms={flash_ms}")

    _trace(f"父进程将拉起子进程：{_interpreter()}（起始目录 {initial or '默认'}）")

    try:
        done = subprocess.run(
            args, capture_output=True, timeout=_DIALOG_TIMEOUT_SECONDS,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        _trace("子进程超时未返回")
        raise TimeoutError("目录选择框一直没有返回，已放弃") from exc
    _trace(f"子进程返回 code={done.returncode}")

    if done.returncode == _EXIT_UNAVAILABLE:
        raise DialogUnavailable((done.stderr or "").strip() or reason)
    if done.returncode != 0:
        raise DialogUnavailable(
            f"目录选择框异常退出（{done.returncode}）："
            f"{(done.stderr or '').strip()[:200]}")

    # 子进程正常收场也把它的话带出来：服务端看不到屏幕，"框弹了没"这类问题
    # 全靠子进程自己报（比如 flash 模式的窗口清单）。
    if (done.stderr or "").strip():
        print(done.stderr.strip(), file=sys.stderr)

    for line in (done.stdout or "").splitlines():
        if line.startswith(_RESULT_PREFIX):
            raw = line[len(_RESULT_PREFIX):].strip()
            return str(Path(raw).resolve()) if raw else None   # 空 = 用户取消
    # 连回话行都没有：机制没按预期工作，让调用方降级而不是给用户看个空
    raise DialogUnavailable("目录选择框没有回话（子进程没输出结果行）")


# ═══════════════════════════════════════════════════════════════
# Windows 后端：IFileOpenDialog（资源管理器式文件夹选择框）
# ═══════════════════════════════════════════════════════════════

# 这几个 IID/CLSID 是系统固定值，抄自 Windows SDK 的 shobjidl.h
_CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
_IID_IFILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
_IID_ISHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
# FOS_PICKFOLDERS：选文件夹而不是选文件（少了它弹的就是选文件的框）
# FOS_FORCEFILESYSTEM：只让选真实文件系统路径（不然可能回一个虚拟命名空间）
# FOS_PATHMUSTEXIST：路径必须存在
_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_PATHMUSTEXIST = 0x00000800
# SIGDN_FILESYSPATH：要"文件系统路径"形式的显示名（拿到的就是 D:\xxx）
_SIGDN_FILESYSPATH = 0x80058000
# HRESULT_FROM_WIN32(ERROR_CANCELLED=1223)：用户点了取消/关掉了框
_HRESULT_CANCELLED = 0x800704C7

# COM 接口的方法是"vtable 里的第 N 个函数指针"。这几个下标来自 shobjidl.h 的
# 声明顺序（IUnknown 占 0~2），写错一个就会调到别的函数上、拿到乱七八糟的结果。
#   IModalWindow:   3 Show
#   IFileDialog:    9 SetOptions / 10 GetOptions / 11 SetDefaultFolder /
#                   12 SetFolder / 17 SetTitle / 20 GetResult
#   IShellItem:     5 GetDisplayName
_VT_SHOW = 3
_VT_SET_OPTIONS = 9
_VT_GET_OPTIONS = 10
_VT_SET_DEFAULT_FOLDER = 11
_VT_SET_FOLDER = 12
_VT_SET_TITLE = 17
_VT_GET_RESULT = 20
_VT_RELEASE = 2
_VT_GET_DISPLAY_NAME = 5

_S_OK = 0
_S_FALSE = 1
# 故意用裸 c_long 而不是 ctypes.HRESULT：后者带一个隐式行为——函数返回失败码时
# 自动抛 OSError。可"用户取消"（HRESULT 0x800704C7）是**正常分支**，不该走异常，
# 我们要自己看返回值（踩过：Show() 一取消就抛 OSError，被外层当成"弹不出来"）。
_HRESULT = ctypes.c_long


def _make_guids():
    """把上面那几个字符串 GUID 变成 ctypes 结构（非 Windows 上返回空）。"""

    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def parse(text: str) -> GUID:
        parts = text.strip("{}").split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return GUID(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
                    (ctypes.c_ubyte * 8)(*tail))

    return GUID, parse


def _pick_windows(initial: str) -> str:
    """"资源管理器式"文件夹选择框。返回路径；用户取消返回 ""。

    raises OSError —— COM 不可用（调用方会翻成 DialogUnavailable）
    """

    import ctypes
    from ctypes import byref, c_void_p, wintypes

    GUID, parse = _make_guids()
    clsid = parse(_CLSID_FILE_OPEN_DIALOG)
    iid_dialog = parse(_IID_IFILE_OPEN_DIALOG)
    iid_item = parse(_IID_ISHELL_ITEM)

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    def call(ptr, index, restype, argtypes=(), *args):
        """按 vtable 下标调一个 COM 方法。argtypes 给默认值：无参方法
        （Release）最容易被漏传，漏了是 TypeError 而不是静默错，但何必。"""

        vtable = ctypes.cast(
            ptr, ctypes.POINTER(ctypes.POINTER(c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        return proto(vtable[index])(ptr, *args)

    def check(hr, what):
        if hr < 0:
            raise OSError(f"{what} 失败（HRESULT 0x{hr & 0xFFFFFFFF:08X}）")
        return hr

    # 显式声明返回类型：这几个都回 HRESULT，默认的 c_int 也能用，但写清楚
    # 免得后来人以为返回值是别的含义。
    ole32.CoCreateInstance.restype = _HRESULT
    ole32.CoInitializeEx.restype = _HRESULT
    shell32.SHCreateItemFromParsingName.restype = _HRESULT

    # COM 要先在**本线程**初始化成单线程套间——文件对话框是 STA 组件。
    _trace("before CoInitializeEx")
    hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    _trace(f"after CoInitializeEx hr=0x{hr & 0xFFFFFFFF:08X}")
    # S_FALSE = 本线程已经初始化过了，正常；RPC_E_CHANGED_MODE(0x80010106)
    # 说明本线程已是别的套间模型——那也是能用对话框的，不该当失败。
    if hr not in (_S_OK, _S_FALSE) and (hr & 0xFFFFFFFF) != 0x80010106:
        raise OSError(f"CoInitializeEx 失败（HRESULT 0x{hr & 0xFFFFFFFF:08X}）")

    dialog = c_void_p()
    item = c_void_p()
    name_buf = ctypes.c_wchar_p()
    try:
        check(ole32.CoCreateInstance(
            byref(clsid), None, _CLSCTX_INPROC_SERVER,
            byref(iid_dialog), byref(dialog)), "创建文件对话框")
        _trace(f"after CoCreateInstance dialog={dialog.value}")

        options = wintypes.DWORD()
        check(call(dialog, _VT_GET_OPTIONS, _HRESULT,
                   [ctypes.POINTER(wintypes.DWORD)], byref(options)),
              "读对话框选项")
        check(call(dialog, _VT_SET_OPTIONS, _HRESULT, [wintypes.DWORD],
                   options.value | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM
                   | _FOS_PATHMUSTEXIST), "设置对话框选项")
        call(dialog, _VT_SET_TITLE, _HRESULT, [wintypes.LPCWSTR], _TITLE)
        _trace("after SetOptions/SetTitle")

        # 起始目录：已经在一个目录里干活，就从那儿开始，省得每次从头翻
        if initial and Path(initial).is_dir():
            # 一定要 normpath 成反斜杠：SHCreateItemFromParsingName **不吃正斜杠**
            # （踩过：传 "D:/x" 它失败，于是框开在"文档"，用户以为起始目录没实现）。
            # 前面几层用的是 pathlib，Python 侧无所谓，到了 shell API 就得是
            # Windows 的写法。
            native = os.path.normpath(str(initial))
            hr = shell32.SHCreateItemFromParsingName(
                native, None, byref(iid_item), byref(item))
            if hr == _S_OK:
                # 起始目录在 FOS_PICKFOLDERS 下的行为有点绕（SetFolder 会被
                # "上次用过的地方"盖掉），两种都设、哪种生效看系统版本。
                # 顺序也试过：先 SetDefaultFolder 再 SetFolder，因为实测
                # 反过来的话 SetFolder 会被 SetDefaultFolder 重置。
                hr_default = call(dialog, _VT_SET_DEFAULT_FOLDER, _HRESULT,
                                  [c_void_p], item)
                hr_folder = call(dialog, _VT_SET_FOLDER, _HRESULT,
                                 [c_void_p], item)
                call(item, _VT_RELEASE, ctypes.c_ulong, [])
                item = c_void_p()
                if os.environ.get("WYWD_DIALOG_DEBUG"):
                    print(f"[dialog] 起始目录 {native}："
                          f"SetDefaultFolder=0x{hr_default & 0xFFFFFFFF:08X} "
                          f"SetFolder=0x{hr_folder & 0xFFFFFFFF:08X}",
                          file=sys.stderr, flush=True)
            else:
                # 起始目录只影响方便程度，不该让整次选择失败——但也不能沉默，
                # 否则"框开在别处"会被当成 bug 反复查。
                print(f"[dialog] 起始目录 {native} 用不上"
                      f"（HRESULT 0x{hr & 0xFFFFFFFF:08X}），框会开在默认位置",
                      file=sys.stderr, flush=True)

        # 模态阻塞：这里一直等到用户选完/取消
        _trace("before Show（到这一步才开始等人）")
        hr = call(dialog, _VT_SHOW, _HRESULT, [wintypes.HWND], None)
        _trace(f"after Show hr=0x{hr & 0xFFFFFFFF:08X}")
        if (hr & 0xFFFFFFFF) == _HRESULT_CANCELLED:
            return ""                          # 取消是正常分支，不是错误
        check(hr, "显示目录选择框")

        check(call(dialog, _VT_GET_RESULT, _HRESULT,
                   [ctypes.POINTER(c_void_p)], byref(item)), "取选择结果")
        check(call(item, _VT_GET_DISPLAY_NAME, _HRESULT,
                   [ctypes.c_int, ctypes.POINTER(ctypes.c_wchar_p)],
                   _SIGDN_FILESYSPATH, byref(name_buf)), "取路径字符串")
        _trace(f"got path={name_buf.value!r}")
        return name_buf.value or ""
    finally:
        if name_buf:
            ole32.CoTaskMemFree(name_buf)      # COM 分配的内存要 COM 来放
        if item:
            call(item, _VT_RELEASE, ctypes.c_ulong, [])
        if dialog:
            call(dialog, _VT_RELEASE, ctypes.c_ulong, [])
        ole32.CoUninitialize()


# ═══════════════════════════════════════════════════════════════
# 其它平台后端：Tk（在 mac/linux 上 Tk 用的就是系统原生框）
# ═══════════════════════════════════════════════════════════════

def _pick_tk(initial: str) -> str:
    """Tk 的目录选择框。返回路径；用户取消返回 ""。

    Windows 上**不要**走这条：Tk 在那儿用的是老的 SHBrowseForFolder，
    只有文件夹树、不显示文件（见模块开头）。
    """

    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()                    # 藏掉 Tk 自己那个空主窗
        root.attributes("-topmost", True)
        root.update()
        return filedialog.askdirectory(
            parent=root, title=_TITLE, mustexist=True,
            initialdir=initial or str(Path.home()),
        ) or ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 子进程入口 —— 这一段只该由 pick_directory() 拉起的子进程执行
# ═══════════════════════════════════════════════════════════════

# WM_CLOSE：让对话框走"用户取消"的正常路径
_WM_CLOSE = 0x0010


def _start_flash_watcher(timeout_ms: int, auto_ok: bool = False) -> None:
    """测试用：后台线程盯着对话框，**真显示出来**了再动它。

    ## 为什么要等"真的可见"才动手

    `Show()` 是异步建窗的：窗口对象先出现（有标题、有尺寸），之后才置上
    WS_VISIBLE。上来就动会拿到一个"还没显示出来"的窗口，于是读到的
    `visible=False` 是在测**我自己手快**，不是测对话框。踩过这个坑。

    ## 两种动它的方式

    - 默认：`WM_CLOSE` → 走"用户取消"分支。
    - `auto_ok=True`：找到对话框的"确定/选择文件夹"按钮按下去 → 走"用户
      真的选了一个"分支，`GetResult` / `GetDisplayName` 那一段才被验到。
      那一段是最后剩下的人工环节，能自动按掉就自动按掉。

    ## 为什么要把窗口清单打出来

    服务端看不到屏幕。"框弹了但用户说没看见"这类问题，唯一能拿到的现场
    证据就是窗口清单（类名/标题/可见性/位置）——光看"找没找到"分不清
    "没弹出来"和"弹在看不见的地方"。

    超时仍不可见就 os._exit(3) 硬退：主线程卡在模态 Show() 里没法正常收场，
    宁可让父进程看到"异常退出"，也别把框永远留在用户桌面上。
    """

    import ctypes
    import threading
    import time
    from ctypes import wintypes

    def describe(user32, hwnd, indent: str) -> str:
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return (f"{indent}class={cls.value!r} title={title.value!r} "
                f"id={user32.GetDlgCtrlID(hwnd)} "
                f"visible={bool(user32.IsWindowVisible(hwnd))} "
                f"rect=({rect.left},{rect.top})-({rect.right},{rect.bottom}) "
                f"hwnd={hwnd}")

    def inventory(user32) -> list[str]:
        """列出**本进程**的所有顶层窗口——对话框到底造出了什么，一目了然。"""

        pid = os.getpid()
        rows: list[str] = []

        def collect(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid:
                return True
            rows.append(describe(user32, hwnd, "      "))
            return True

        user32.EnumWindows(
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                               wintypes.LPARAM)(collect), 0)
        return rows

    def children(user32, parent) -> list[tuple[int, str]]:
        """列子窗口，返回 (hwnd, 描述)。要按的"确定"就在这里面。"""

        found: list[tuple[int, str]] = []

        def collect(hwnd, _lparam):
            found.append((hwnd, describe(user32, hwnd, "        ")))
            return True

        user32.EnumChildWindows(
            parent, ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                       wintypes.LPARAM)(collect), 0)
        return found

    def press_ok(user32, dialog) -> str:
        """按下对话框的"确定/选择文件夹"按钮。返回一行说明。"""

        kids = children(user32, dialog)
        print("[flash] 对话框子窗口清单：", file=sys.stderr, flush=True)
        for _hwnd, text in kids:
            print(text, file=sys.stderr, flush=True)

        # 先试标准 IDOK（=1）：多数对话框的"确定"就是控件 id 1
        ok = user32.GetDlgItem(dialog, 1)
        how = "GetDlgItem(IDOK)"
        if not ok:
            # 再按类名 + 文字找：中文"选择文件夹"，英文 "Select Folder"
            for hwnd, text in kids:
                if "class='Button'" not in text:
                    continue
                if any(word in text for word in
                       ("选择", "确定", "Select", "OK", "&O")):
                    ok, how = hwnd, "类名+文字找到的按钮"
                    break
        if not ok:
            return "没找到'确定'按钮（子窗口清单见上）"
        user32.PostMessageW(ok, 0x00F5, 0, 0)      # BM_CLICK
        return f"已按下 {how}（hwnd={ok}）"

    def watch() -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]

        deadline = time.time() + timeout_ms / 1000
        hwnd = None
        while time.time() < deadline:
            hwnd = user32.FindWindowW(None, _TITLE)
            if hwnd and user32.IsWindowVisible(hwnd):
                break                 # 真的显示出来了
            # 找到了但还不可见 = 还在建窗，接着等（别动半成品）
            time.sleep(0.05)

        print("[flash] 本进程顶层窗口清单：", file=sys.stderr, flush=True)
        for row in inventory(user32):
            print(row, file=sys.stderr, flush=True)

        if not hwnd:
            print("flash 失败：这段时间里没等到对话框窗口", file=sys.stderr)
            os._exit(3)

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        foreground = user32.GetForegroundWindow()
        print(
            "[flash] 对话框窗口："
            f"visible={bool(user32.IsWindowVisible(hwnd))} "
            f"rect=({rect.left},{rect.top})-({rect.right},{rect.bottom}) "
            f"foreground={'是自己' if foreground == hwnd else '是别的窗口'}",
            file=sys.stderr, flush=True)

        if auto_ok:
            # 等对话框把内容填好再按：按钮可能还没 enable，按了不生效
            time.sleep(0.8)
            print("[flash] " + press_ok(user32, hwnd),
                  file=sys.stderr, flush=True)
        else:
            user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)

    threading.Thread(target=watch, daemon=True).start()


def _child(argv: list[str]) -> int:
    """在**全新解释器的主线程**里弹框，把结果打成一行回话。"""

    # 位置参数 = 那一个起始目录；`--xxx` 全是开关。
    # 别写成 `argv[0] if not argv[0].startswith("--")`：父进程永远把 `--child`
    # 放在最前面，那样写会让起始目录**永远为空**（踩过：框一直开在"文档"，
    # 查了半天以为 SetFolder 不生效，其实是参数根本没传进来）。
    positional = [arg for arg in argv if not arg.startswith("--")]
    initial = positional[0] if positional else ""
    selftest = "--selftest" in argv
    auto_ok = "--auto-ok" in argv
    flash_ms = 0
    for arg in argv:
        if arg.startswith("--flash-ms="):
            flash_ms = int(arg.split("=", 1)[1])

    _trace(f"child 启动 argv={argv} initial={initial!r} {_whereami()}")

    if selftest:
        # 给冒烟用：**不建窗、不弹框**，只把起进程 + 回话协议 + 路径解析这一侧
        # 真实走一遍。真弹框那一半由人工点一次验（在有窗口的桌面会话里）。
        # 存在的理由：代理/沙箱 shell 会在"孙进程开窗"时把进程组干掉，
        # 导致弹框那一半在这样的环境里根本测不了。（实测如此。）
        print(_RESULT_PREFIX + str(Path(initial or Path.cwd()).resolve()))
        return 0

    if flash_ms:
        _start_flash_watcher(flash_ms, auto_ok=auto_ok)

    try:
        chosen = _pick_windows(initial) if os.name == "nt" else _pick_tk(initial)
    except ImportError as exc:
        print(f"缺少依赖：{exc}", file=sys.stderr)
        return _EXIT_UNAVAILABLE
    except OSError as exc:
        print(f"弹框失败：{exc}", file=sys.stderr)
        return _EXIT_UNAVAILABLE
    except Exception as exc:                # TclError 等：一并当"弹不出来"处理
        print(f"弹框失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return _EXIT_UNAVAILABLE

    print(_RESULT_PREFIX + chosen)
    return 0


if __name__ == "__main__":
    raise SystemExit(_child(sys.argv[1:]))
