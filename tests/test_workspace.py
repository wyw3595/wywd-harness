"""工作区（Workspace）测试：上传目录 = 一个会话沙箱。

覆盖方案（2026-09-13 动手）第 1~3 步的关键承诺：
  1. 默认行为不变——root 不传（None 哨兵）时一切照旧（既有 366 条
     测试全过已经证明，这里不再重复）；
  2. 会话隔离——两个工作区各拿各的注册表，A 读不到 B 的文件，
     相对路径越出上传目录即 PermissionError；
  3. schema 干净——root / sandbox_root 不许出现在任何发给模型的
     schema 里（出现在签名里 = 模型能传参绕沙箱，安全漏洞）；
  4. 禁区照旧——上传目录里的 .env 一样读不走（机制只有一份，
     参数化后自动生效）。

全部离线：tmp 目录 + 直接调 handler，不碰网络、不碰真实项目树。
"""

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.toolbox import (
    DEFERRED_TOOLS,
    build_registry,
    build_registry_for,
)
from src.harness.file_tools import read_file, write_file
from src.harness.workspace import Workspace


class WorkspaceCreateTests(unittest.TestCase):
    """create / from_existing_dir / cleanup 的生命周期行为。"""

    def setUp(self) -> None:
        import shutil

        self._shutil = shutil
        self.base = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        # 测试自己的 base 是 tmp：整个删掉不算破坏用户数据。
        self._shutil.rmtree(self.base, ignore_errors=True)

    def test_create_makes_directory_and_isolates_by_id(self) -> None:
        ws = Workspace.create(workspace_id="s1", base=self.base)
        self.assertTrue(ws.root.is_dir())
        self.assertEqual(ws.workspace_id, "s1")
        self.assertEqual(ws.root, (self.base / "s1").resolve())

    def test_create_rejects_duplicate_id(self) -> None:
        """同一 ID 开两次直接炸——悄悄复用旧目录是数据串台，不是容错。"""

        Workspace.create(workspace_id="dup", base=self.base)
        with self.assertRaises(FileExistsError):
            Workspace.create(workspace_id="dup", base=self.base)

    def test_from_existing_dir_requires_real_directory(self) -> None:
        with self.assertRaises(FileNotFoundError):
            Workspace.from_existing_dir(self.base / "no-such-dir")

    def test_from_existing_dir_references_not_copies(self) -> None:
        """直接引用不复制：包出来的 root 与原目录是同一个路径。"""

        ws = Workspace.from_existing_dir(self.base)
        self.assertEqual(ws.root, self.base)
        self.assertEqual(ws.workspace_id, self.base.name)

    def test_cleanup_refuses_without_confirm(self) -> None:
        """cleanup 默认拒绝——删除不可逆，必须显式点头。"""

        ws = Workspace.create(workspace_id="c1", base=self.base)
        result = ws.cleanup()  # 不带 confirm
        self.assertIn("未删除", result)
        self.assertTrue(ws.root.exists())  # 目录还在

    def test_cleanup_removes_when_confirmed(self) -> None:
        ws = Workspace.create(workspace_id="c2", base=self.base)
        result = ws.cleanup(confirm=True)
        self.assertIn("已删除", result)
        self.assertFalse(ws.root.exists())


class WorkspaceIsolationTests(unittest.TestCase):
    """两个工作区：工具集按 root 现做，互相看不见对方的文件。"""

    def setUp(self) -> None:
        import shutil

        self._shutil = shutil
        self.dir_a = Path(tempfile.mkdtemp())
        self.dir_b = Path(tempfile.mkdtemp())
        # A 里放一个独有文件：会话 A 的模型该能读到它。
        (self.dir_a / "secret-a.txt").write_text("A 的私有内容", encoding="utf-8")
        (self.dir_b / "secret-b.txt").write_text("B 的私有内容", encoding="utf-8")
        self.ws_a = Workspace.from_existing_dir(self.dir_a)
        self.ws_b = Workspace.from_existing_dir(self.dir_b)
        self.registry_a = build_registry_for(self.ws_a)

    def tearDown(self) -> None:
        self._shutil.rmtree(self.dir_a, ignore_errors=True)
        self._shutil.rmtree(self.dir_b, ignore_errors=True)

    def test_a_reads_own_file(self) -> None:
        content = self.registry_a.execute("fs_read", "secret-a.txt")
        self.assertIn("A 的私有内容", content)

    def test_a_cannot_see_b_by_relative_escape(self) -> None:
        """A 的工具里用相对路径穿到 B 的目录——越界即 PermissionError。"""

        # B 的目录是 A 目录的兄弟（都在系统 tmp 下）：../<B目录名>/ 走越界路径。
        escape = f"../{self.dir_b.name}/secret-b.txt"
        with self.assertRaises(PermissionError):
            self.registry_a.execute("fs_read", escape)

    def test_a_cannot_absolute_path_into_b(self) -> None:
        """绝对路径也绕不过：沙箱根就是边界，管你相对还是绝对。"""

        with self.assertRaises(PermissionError):
            self.registry_a.execute("fs_read", str(self.dir_b / "secret-b.txt"))

    def test_write_lands_in_own_workspace(self) -> None:
        """A 会话里写文件，落盘落在 A 的目录——不会跑到 B 或项目根。"""

        self.registry_a.execute("fs_write", "note.txt", "A 会话写的")
        self.assertTrue((self.dir_a / "note.txt").exists())
        self.assertFalse((self.dir_b / "note.txt").exists())

    def test_forbidden_zone_inside_uploaded_dir(self) -> None:
        """上传目录里的 .env 同样是禁区——密钥读不走（机制只有一份）。"""

        (self.dir_a / ".env").write_text("API_KEY=sk-secret", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.registry_a.execute("fs_read", ".env")


class SchemaCleanlinessTests(unittest.TestCase):
    """沙箱根不许出现在任何模型可见的 schema 里——安全底线。

    细分两种"root"（改造前就存在的概念，别混）：
      - fs_find / fs_glob / tree_dir 的 root 是**搜索起点**（模型可见
        参数，原版就有）——它合法；
      - fs_list / fs_read / fs_write / fs_edit 的沙箱根参数，以及所有
        工具的 sandbox_root——它们出现在 schema 里 = 模型能传任意
        路径绕沙箱，这才是要禁的。
    """

    # 沙箱根管路径解释的文件工具：这些工具的 root 参数是内部注入。
    FILE_TOOLS = ("fs_list", "fs_read", "fs_write", "fs_edit")

    def _schemas(self, registry) -> list[tuple[str, dict]]:
        return [
            (schema["function"]["name"],
             schema["function"]["parameters"]["properties"])
            for schema in registry.model_schemas()
        ]

    def test_instant_tool_schemas_have_no_sandbox_root(self) -> None:
        for name, props in self._schemas(build_registry()):
            if name in self.FILE_TOOLS:
                self.assertNotIn("root", props,
                                 f"{name} 泄露了沙箱根参数 root")
            self.assertNotIn("sandbox_root", props,
                             f"{name} 泄露了 sandbox_root")

    def test_deferred_tool_schemas_have_no_sandbox_root(self) -> None:
        for tool in DEFERRED_TOOLS:
            props = tool.model_schema()["function"]["parameters"]["properties"]
            self.assertNotIn("sandbox_root", props,
                             f"{tool.name} 泄露了 sandbox_root")

    def test_custom_workspace_schemas_still_clean(self) -> None:
        """按上传目录现做的 schema 也要查——绑定别的 root 不改变签名。"""

        import tempfile

        tmp = Path(tempfile.mkdtemp())
        try:
            ws = Workspace.from_existing_dir(tmp)
            for name, props in self._schemas(build_registry_for(ws)):
                if name in self.FILE_TOOLS:
                    self.assertNotIn("root", props)
                self.assertNotIn("sandbox_root", props)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class DefaultBehaviorTests(unittest.TestCase):
    """默认工作区（项目根）的兼容包装：结构与改造前一致。"""

    def test_default_registry_has_same_tools(self) -> None:
        # names() 含延迟工具（注册在册、只是不进 model_schemas）——
        # 与改造前的注册名单逐一对齐。
        expected = {
            "get_weather", "now", "fs_list", "fs_read", "fs_write", "fs_edit",
            "calc", "fs_find", "fs_glob", "tree_dir", "memory_write",
            "ToolSearch", "DeferExecuteTool",
        }
        self.assertEqual(set(build_registry().names()), expected)

    def test_default_root_reads_project_file(self) -> None:
        """不传 root（哨兵）仍以项目根为沙箱：读 README 的开头能读到。"""

        content = read_file("README.md", limit=3)
        self.assertIn("wywd-harness", content)

    def test_default_root_write_is_parameterized(self) -> None:
        """write_file 的 root 参数能落到 tmp——直连函数层验证哨兵透传。"""

        import tempfile

        tmp = Path(tempfile.mkdtemp())
        try:
            write_file("probe.txt", "tmp 沙箱内容", root=tmp)
            self.assertEqual(
                (tmp / "probe.txt").read_text(encoding="utf-8"),
                "tmp 沙箱内容",
            )
            # 事件不存在于项目根——写进的是 tmp，不是默认沙箱。
            self.assertFalse((Path.cwd() / "probe.txt").exists())
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class ZipUploadTests(unittest.TestCase):
    """from_zip：解压安检（zip slip + 三道限额）+ 失败回滚 + 剥层。

    zip 是**不可信输入**（用户从外面拿来的包），所以这一组测的全是
    "坏包打进来会怎样"——每一道闸都要有一条"确实被拦下"的断言，
    少一条就漏一个攻击面。
    """

    def setUp(self) -> None:
        import shutil

        self._shutil = shutil
        self.base = Path(tempfile.mkdtemp())     # workspaces/ 的位置
        self.zips = Path(tempfile.mkdtemp())     # 待解压的包放这儿

    def tearDown(self) -> None:
        self._shutil.rmtree(self.base, ignore_errors=True)
        self._shutil.rmtree(self.zips, ignore_errors=True)

    def _make_zip(self, entries: dict, name: str = "upload.zip") -> Path:
        """按 {条目名: 内容} 造一个 zip——条目名故意允许任意字符串
        （攻击面就在这个名字上，测试不能替攻击者"讲卫生"）。"""

        path = self.zips / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, payload in entries.items():
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                zf.writestr(member, payload)
        return path

    def test_extracts_files_into_workspace(self) -> None:
        zip_path = self._make_zip({"a.txt": "hello", "sub/b.txt": "world"})
        ws = Workspace.from_zip(zip_path, workspace_id="u1", base=self.base)
        self.assertEqual((ws.root / "a.txt").read_text(encoding="utf-8"), "hello")
        self.assertEqual(
            (ws.root / "sub" / "b.txt").read_text(encoding="utf-8"), "world"
        )

    def test_rejects_zip_slip_relative(self) -> None:
        """../ 穿越：解压落点跑到工作区外——整包拒绝。"""

        zip_path = self._make_zip({"../evil.txt": "pwned"})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u2", base=self.base)

    def test_rejects_zip_slip_backslash(self) -> None:
        """反斜杠变体：老 Windows 打包工具会这么写，不统一分隔符就漏。"""

        zip_path = self._make_zip({"..\\evil.txt": "pwned"})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u3", base=self.base)

    def test_rejects_absolute_member(self) -> None:
        """绝对路径条目（/etc/... 或带盘符）同样不是"界内的相对路径"。"""

        zip_path = self._make_zip({"/tmp/evil.txt": "pwned"})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u4", base=self.base)

    def test_entry_count_limit(self) -> None:
        """文件洪水：条目数超限，开解之前就拒。"""

        zip_path = self._make_zip({f"f{i}.txt": "x" for i in range(5)})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u5", base=self.base,
                               max_entries=3)

    def test_total_bytes_limit(self) -> None:
        """解压炸弹：逐块数**实际字节**，声明的 file_size 骗不过去。"""

        zip_path = self._make_zip({"a.bin": b"x" * 1000, "b.bin": b"y" * 1000})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u6", base=self.base,
                               max_total_bytes=1500)

    def test_single_file_limit(self) -> None:
        zip_path = self._make_zip({"big.bin": b"x" * 2000})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u7", base=self.base,
                               max_file_bytes=1000)

    def test_failed_extract_leaves_nothing_behind(self) -> None:
        """失败即回滚：坏包不留下半成品工作区（用户以为成功最可怕）。"""

        zip_path = self._make_zip({"../evil.txt": "pwned"})
        with self.assertRaises(ValueError):
            Workspace.from_zip(zip_path, workspace_id="u8", base=self.base)
        self.assertFalse((self.base / "u8").exists())

    def test_single_top_dir_is_promoted(self) -> None:
        """GitHub 式打包：顶层只有一个目录时剥掉，root 指向它。"""

        zip_path = self._make_zip({"repo-main/src/a.py": "print(1)"})
        ws = Workspace.from_zip(zip_path, workspace_id="u9", base=self.base)
        self.assertEqual(ws.root, (self.base / "u9" / "repo-main").resolve())
        self.assertTrue((ws.root / "src" / "a.py").exists())

    def test_multiple_top_entries_not_promoted(self) -> None:
        """顶层不止一个条目时不剥——判定不了意图，别替用户猜。"""

        zip_path = self._make_zip({"a.txt": "1", "b.txt": "2"})
        ws = Workspace.from_zip(zip_path, workspace_id="u10", base=self.base)
        self.assertEqual(ws.root, (self.base / "u10").resolve())

    def test_promoted_workspace_cleanup_removes_shell(self) -> None:
        """剥层的工作区删干净：内容 + 外层空壳一起走。"""

        zip_path = self._make_zip({"repo-main/a.txt": "1"})
        ws = Workspace.from_zip(zip_path, workspace_id="u11", base=self.base)
        ws.cleanup(confirm=True)
        self.assertFalse((self.base / "u11").exists())

    def test_model_tools_work_inside_uploaded_zip(self) -> None:
        """上传之后真能用：按解压目录做注册表，模型读得到包里的文件。"""

        zip_path = self._make_zip({"repo-main/readme.md": "# 任务说明"})
        ws = Workspace.from_zip(zip_path, workspace_id="u12", base=self.base)
        registry = build_registry_for(ws)
        self.assertIn("任务说明", registry.execute("fs_read", "readme.md"))


class ArchiveTests(unittest.TestCase):
    """archive：打包带走；与 from_zip 构成 roundtrip。"""

    def setUp(self) -> None:
        import shutil

        self._shutil = shutil
        self.base = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self._shutil.rmtree(self.base, ignore_errors=True)

    def test_archive_roundtrip_restores_tree(self) -> None:
        """归档 -> 再解压：同一棵树（含子目录）原样还原。"""

        ws = Workspace.create(workspace_id="a1", base=self.base)
        (ws.root / "notes.txt").write_text("内容", encoding="utf-8")
        (ws.root / "sub").mkdir()
        (ws.root / "sub" / "x.py").write_text("print(1)", encoding="utf-8")

        archive = ws.archive()
        self.assertTrue(archive.is_file())

        restored = Workspace.from_zip(archive, workspace_id="a2", base=self.base)
        self.assertEqual(
            (restored.root / "notes.txt").read_text(encoding="utf-8"), "内容"
        )
        self.assertEqual(
            (restored.root / "sub" / "x.py").read_text(encoding="utf-8"),
            "print(1)",
        )

    def test_archive_keeps_forbidden_files(self) -> None:
        """归档**不过滤** .env：这是用户带走自己的东西，不是模型在读。

        过滤会造成"归档再解压回来少文件"的静默数据丢失——比"文件在
        但模型读不走"糟得多。禁区规则由工具层保证，与归档无关。
        """

        ws = Workspace.create(workspace_id="a3", base=self.base)
        (ws.root / ".env").write_text("API_KEY=secret", encoding="utf-8")
        archive = ws.archive()
        with zipfile.ZipFile(archive) as zf:
            self.assertIn(".env", zf.namelist())


if __name__ == "__main__":
    unittest.main()