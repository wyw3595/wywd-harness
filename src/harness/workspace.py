"""会话工作区（Workspace）："上传一个目录，模型在里面工作"的落点。

背景（2026-09-13 动手）：此前沙箱根是 file_tools.ALLOWED_ROOT——模块
import 那一刻就焊死在项目根上，运行时没有任何口子换目录。本模块把
"沙箱根"从全局常量升格为会话级状态：

    上传/指定一个目录 -> 包成 Workspace -> 装配层按它现做一套工具
    （toolbox.build_registry_for）-> 模型在它里面读写，出界即拒

两个来源（按接入顺序）：
  1. from_existing_dir(path)：直接引用本机一个已有目录——最简单，
     先把"换沙箱"的机制跑通；
  2. from_zip(zip_path)：解压一个 zip 到 workspaces/<id>/（三道安检，
     见 _safe_extract_all）——网页/终端上传走这条。

配套的生命周期：
  - archive()：把工作区打包成 zip 带走（与 from_zip 对称，roundtrip
    能还原同一棵树）；
  - cleanup()：删除工作区目录，默认拒绝（confirm=True 才动手）。

设计要点：
  - frozen dataclass：两个字段（workspace_id、root）定了就不许改。
    换目录 = 新建一个 Workspace，而不是原地改——可变的沙箱根会
    在多个会话之间串台（A 会话改了 root，B 会话的工具跟着越狱）；
  - 会话隔离天然成立：每个 Workspace 的 root 互相独立，工具集又是
    按 root 现做的闭包，A 会话的工具根本解析不到 B 会话的路径；
  - cleanup 默认拒绝：删目录不可逆，不挂 confirm=True 只返回警告。
    教学取舍：宁可啰嗦，不可误删。
  - zip 是**不可信输入**（用户从外面拿来的压缩包），所以它的处理
    规则是"先安检、边解边查"：路径逐条验（防 zip slip）、条目数/
    单文件/总量三道限额（防解压炸弹）。详见 _safe_extract_all。
"""

from __future__ import annotations

import secrets
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# workspaces 的基地址：本文件位于 src/harness/，往上两级是项目根。
# 与 ALLOWED_ROOT 同一定位手法（用 __file__ 而非 cwd），保证无论从
# 哪里启动，会话工作区都落在同一个地方。
WORKSPACE_BASE = Path(__file__).resolve().parents[2] / "workspaces"

# 解压安检的三道限额。三者防的不是同一件事，缺一道就能被绕过：
#   ENTRIES —— 文件洪水（几十万个小文件拖垮文件系统/审计）；
#   FILE    —— 单个超大文件（一个 10GB 的 vm 镜像）；
#   TOTAL   —— 解压炸弹（几十 KB 的 zip 解出几十 GB，磁盘直接满）。
# 数值是教学取舍：够跑真实小项目，又不至于把开发机拖死。
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_FILE_BYTES = 20 * 1024 * 1024      # 单文件 20 MB
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024    # 解压总量 100 MB
ZIP_CHUNK_BYTES = 1 << 20                  # 逐块读写，1 MB 一块



def _new_workspace_id() -> str:
    """生成可读且基本不撞车的会话 ID：日期时间 + 四位随机后缀。

    用时间戳而不是 uuid：workspaces/ 目录是给人翻的，
    "20260913-1430-a1b2" 一眼能看出是哪次会话；后缀随机防止
    同一秒开的两个会话撞名（mkdir exist_ok=False 会直接炸）。
    """

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _safe_member_path(name: str, dest: Path) -> Path:
    """把 zip 里一个条目的名字解析成目标目录内的绝对路径；越界抛 ValueError。

    这是防 **zip slip** 的那一道。zip 条目名是完全由打包方控制的字符串，
    可以长这样（都是真实攻击面，不是理论）：
        ../../../../Windows/System32/evil.dll   相对穿越
        /etc/cron.d/evil                        绝对路径
        C:/Users/me/startup/evil.bat            带盘符
        ..\\..\\secret.txt                       反斜杠变体（Windows 打包工具）
    它们都靠"名字里带路径"把文件写到目标目录**之外**——解压本身没错，
    错在落点。所以判定和 _resolve_safe 同一套：先 resolve 再验亲缘，
    resolve 会把 ".." 和符号链接都算进去，骗不过去。

    反斜杠先统一成 "/"：老 Windows 打包工具会写反斜杠，
    在 Linux 上它只是普通文件名字符，在 Windows 上却是分隔符——
    不统一的话，同一个包在两个平台上落点不同（安全判定必须跨平台一致）。
    """

    normalized = name.replace("\\", "/")
    base = dest.resolve()
    target = (base / normalized).resolve()
    if not target.is_relative_to(base):
        raise ValueError(
            f"zip 条目越出目标目录（zip slip 嫌疑）：{name!r}——"
            f"压缩包只允许写进工作区内部，请重新打包（去掉 ../ 或绝对路径）"
        )
    return target


def _safe_extract_all(
    zip_path: Path,
    dest: Path,
    max_entries: int = MAX_ZIP_ENTRIES,
    max_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
    max_file_bytes: int = MAX_ZIP_FILE_BYTES,
) -> int:
    """把 zip 解压到 dest（只写文件，不还原符号链接/权限）；返回文件数。

    为什么不用 zf.extractall()：它一次只干两件事（写文件、建目录），
    既不验证路径（zip slip 是它历史上的著名 CVE 家族），也不限总量
    （解压炸弹）。所以这里**逐条自己解**，把三道闸放在写盘的路上：

      1. 条目数：开头一次性挡掉"文件洪水"；
      2. 逐条路径验亲缘（_safe_member_path）——任何一条越界，整包拒绝；
      3. 逐块累计实际写入字节数：单文件超限 or 总量超限立即中断。

    第 3 道刻意数**实际读出来的字节**，而不是信 zip 头里的 file_size——
    头是攻击方能随便写的声明值（声称 1KB、实际吐 10GB 的包就是这么
    做的）。数真实字节，声明值就失效了。

    只解文件、不还原符号链接：zip 里能带 unix symlink 条目，还原出来
    的链接可能指向工作区外，那等于自己给沙箱开后门。跳过它们，
    安全边界比"完整还原元数据"重要——这是刻意的取舍，不是没实现。

    禁区（.env / .git）**照常解压**，不做过滤。取舍理由：zip 是用户
    自己的内容，过滤会造成"归档再解压回来少了文件"的静默数据丢失；
    而禁区规则本来就在工具层（_resolve_safe / 权限策略）执行——
    文件躺在那儿，模型照样读不走，安全语义一点没松。
    """

    with zipfile.ZipFile(zip_path) as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(infos) > max_entries:
            raise ValueError(
                f"压缩包条目过多（{len(infos)} 个，上限 {max_entries}）——"
                f"请只打包需要模型处理的部分"
            )
        total = 0
        for info in infos:
            target = _safe_member_path(info.filename, dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zf.open(info) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(ZIP_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > max_file_bytes:
                        raise ValueError(
                            f"压缩包里的 {info.filename!r} 超过单文件上限 "
                            f"{max_file_bytes} 字节——大文件请单独处理"
                        )
                    if total > max_total_bytes:
                        raise ValueError(
                            f"解压总量超过上限 {max_total_bytes} 字节——"
                            f"请只打包需要模型处理的部分"
                        )
                    sink.write(chunk)
        return len(infos)


@dataclass(frozen=True)
class Workspace:
    """一个会话的沙箱根：模型在这个目录里工作，出界即 PermissionError。

    字段刻意只有两个：标识 + 路径。工具构建、权限策略、记忆都由
    装配层（toolbox）按 root 现做，本类不持有行为——它是纯数据。
    """

    workspace_id: str
    root: Path

    @classmethod
    def create(
        cls,
        workspace_id: str | None = None,
        base: Path = WORKSPACE_BASE,
    ) -> "Workspace":
        """在 workspaces/ 下开一块新空地，返回指向它的 Workspace。

        Args:
            workspace_id: 会话标识，默认按"时间戳 + 随机后缀"现生成。
            base: 会话目录的父目录（测试注入 tmp 用），默认项目根/workspaces。
        """

        ws_id = workspace_id if workspace_id is not None else _new_workspace_id()
        root = (base / ws_id).resolve()
        # mkdir(exist_ok=False)：同一 ID 开第二次是程序错误，直接炸。
        # 悄悄复用旧目录的话，上一个会话的文件会"阴魂不散"地出现在
        # 新会话里——那是数据串台，不是容错。
        root.mkdir(parents=True, exist_ok=False)
        return cls(workspace_id=ws_id, root=root)

    @classmethod
    def from_existing_dir(
        cls, path: str | Path, workspace_id: str | None = None
    ) -> "Workspace":
        """把一个已有目录包成 Workspace（本地路径入口：直接引用，不复制）。

        为什么不复制：复制要处理大小、进度、失败回滚，全是入口层的
        课题；先把"换沙箱"的机制跑通，zip 上传与复制是方案第 4 步的事。
        模型对这个目录的能力边界不变：仍然只能读文本、写文本、
        越界即拒——沙箱语义与项目根时代完全一致，只是换了个地方。
        """

        root = Path(path).resolve()
        if not root.is_dir():
            raise FileNotFoundError(
                f"不是可用的目录：{path}——上传的工作目录必须先存在，"
                f"网页入口则先走 zip 解压再包工作区"
            )
        return cls(workspace_id=workspace_id or root.name, root=root)

    @classmethod
    def from_zip(
        cls,
        zip_path: str | Path,
        workspace_id: str | None = None,
        base: Path = WORKSPACE_BASE,
        *,
        max_entries: int = MAX_ZIP_ENTRIES,
        max_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
        max_file_bytes: int = MAX_ZIP_FILE_BYTES,
    ) -> "Workspace":
        """解压一个 zip 成为新工作区（上传入口的主路）。

        Args:
            zip_path: 待解压的 zip 文件路径。
            workspace_id: 会话标识，默认按"时间戳 + 随机后缀"现生成。
            base: 会话目录的父目录（测试注入 tmp 用）。
            max_entries: 条目数上限（文件洪水）。
            max_total_bytes: 解压总量上限（解压炸弹）。
            max_file_bytes: 单文件大小上限。

        失败即回滚：解压中途任何一条安检没过（越界、超限、坏包），
        这里把刚开的目录整个删掉再抛。理由——失败留下的**半成品工作区**
        比失败本身更糟：用户以为上传成功了，模型却在一个缺文件的项目里
        干活。宁可当这次上传没发生过。

        剥层（GitHub zip 的体贴处理）：从 GitHub 下载的仓库 zip 解压后
        都套一层 `repo-main/`。顶层只有一个目录时，把 root 指向它——
        不剥的话模型每次都要多写一层前缀，而且 tree_dir 的根看起来
        像个空壳。多文件/多目录的包不剥（无法判定意图）。

        注意 zip 是**不可信输入**，三道安检在 _safe_extract_all 里，
        这里只负责"开目录 -> 解压 -> 失败回滚 -> 剥层"。
        """

        workspace = cls.create(workspace_id=workspace_id, base=base)
        try:
            _safe_extract_all(
                Path(zip_path), workspace.root,
                max_entries, max_total_bytes, max_file_bytes,
            )
        except Exception:
            # 目录是刚 create 的、内容全是这次解压写进去的——删它不碰
            # 任何别人的东西（所以 cleanup 可以直接 confirm=True）。
            workspace.cleanup(confirm=True)
            raise
        entries = list(workspace.root.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return cls(workspace_id=workspace.workspace_id,
                       root=entries[0].resolve())
        return workspace

    def archive(self, dest: str | Path | None = None) -> Path:
        """把工作区打包成一个 zip（会话产物带走用），返回 zip 路径。

        与 from_zip 对称：打出来的包再 from_zip 能还原同一棵树
        （包内路径一律相对工作根、用 / 分隔——跨平台可读）。
        默认落在工作区目录的旁边（`workspaces/<id>.zip`），可用 dest 指定。

        归档不做禁区过滤（.env / .git 照常进包）：这是用户在**带走
        自己的东西**，不是模型在读——而模型读不走它们的规则由工具层
        保证，两者不冲突。
        """

        zip_path = (Path(dest) if dest is not None
                    else self.root.parent / f"{self.workspace_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(self.root.rglob("*")):
                if path.is_file():
                    # as_posix()：包内统一用 / ——Windows 打的包在 Linux
                    # 上解也是同一棵树（zip 规范本来就规定 / 分隔）。
                    zf.write(path, path.relative_to(self.root).as_posix())
        return zip_path

    def cleanup(self, confirm: bool = False) -> str:
        """删除本工作区目录。默认拒绝——删除不可逆，必须显式 confirm=True。

        返回文案而不是静默成功：调用方（网页/终端）把它原样转告用户，
        "没删成"和"删了"都该有回音——这沿袭本仓库"错误要给下一步"
        的老规矩。
        """

        if not confirm:
            return (
                f"未删除 {self.root}——cleanup 默认拒绝。"
                f"确认要删请重试并传 confirm=True（操作不可逆，"
                f"建议先确认会话产物已备份）。"
            )
        shutil.rmtree(self.root)
        # 剥层的工作区（from_zip 把 root 指到了 <id>/<repo>/）删完内容后
        # 外层空壳还在，顺手清掉——只删"名字等于 workspace_id 且已空"
        # 的那一层，别的什么都不碰。
        shell = self.root.parent
        if (shell.name == self.workspace_id and shell.is_dir()
                and not any(shell.iterdir())):
            shell.rmdir()
        return f"已删除工作区 {self.workspace_id}（{self.root}）"
