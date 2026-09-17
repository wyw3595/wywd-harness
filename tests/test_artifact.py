"""工具输出外化（s13）的回归测试：阈值、落盘、指针、绝不覆盖。

全离线、不碰磁盘以外的东西。用例里的阈值一律**显式压小**（比如 10 字符），
这样"造一个大输出"不需要真的拼几 MB 字符串——测的是判断逻辑，不是性能。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.harness.artifact import (
    ArtifactStore,
    summarize,
)


class TemporaryStoreCase(unittest.TestCase):
    """共用的临时目录收尾。

    宿主的"安全删除"会拦 unlink（见项目记忆），清理失败不该算测试失败。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        try:
            self.tmp.cleanup()
        except OSError:
            pass


class ThresholdTests(TemporaryStoreCase):
    """阈值口径：bash 按字符，其它工具按字节——两套口径各有理由。"""

    def test_small_output_is_returned_as_is(self) -> None:
        """不超阈值就什么都不做：原样返回，连目录都不建。"""

        store = ArtifactStore(self.root)
        output = "工具 bash 返回：(exit 0)\nhello"

        self.assertEqual(store.externalize("bash", output), output)
        self.assertFalse(store.directory.exists())

    def test_bash_threshold_counts_characters(self) -> None:
        """bash 按字符：刚好等于阈值不外化，多一个字符才外化。

        预览也一起调小（10+10=20，底线 40），否则 100 字符的小输出
        过不了"省得出来"那道底线，测的就成了底线而不是阈值。
        """

        store = ArtifactStore(self.root, bash_max_chars=100,
                              head_chars=10, tail_chars=10)

        self.assertFalse(store.should_externalize("bash", "x" * 100))
        self.assertTrue(store.should_externalize("bash", "x" * 101))

    def test_other_tools_threshold_counts_bytes(self) -> None:
        """非 bash 按**字节**：中文一个字符三字节，按字符算会低估体积。"""

        store = ArtifactStore(self.root, blob_threshold_kb=1,
                              head_chars=10, tail_chars=10)

        # 400 个汉字 = 1200 字节 > 1KB，虽然只有 400 个字符
        self.assertTrue(store.should_externalize("fs_read", "汉" * 400))
        # 400 个 ASCII = 400 字节 < 1KB
        self.assertFalse(store.should_externalize("fs_read", "x" * 400))

    def test_small_win_is_not_worth_a_page_fault(self) -> None:
        """省不到一半就不换页——否则指针可能比原文还长。

        这是教材缺的那条底线（它的 bash 阈值 30000 与预览 6KB+24KB 几乎相等）。
        换页要付两次代价：落盘的 IO，以及模型想读全文时的一次 fs_read。
        所以"刚过阈值"的输出宁可原样留着。
        """

        store = ArtifactStore(self.root, bash_max_chars=30,
                              head_chars=10, tail_chars=10)   # 底线 = 40

        self.assertFalse(store.should_externalize("bash", "x" * 39))
        self.assertTrue(store.should_externalize("bash", "x" * 100))


class StoreTests(TemporaryStoreCase):
    """落盘：内容逐字节一致、SHA 对得上、序号递增、绝不覆盖。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = ArtifactStore(self.root, bash_max_chars=10)

    def test_stored_bytes_match_the_original(self) -> None:
        """磁盘上必须是**逐字节**的原文——它是唯一正文所有者。"""

        output = "错误：" + "x" * 100
        artifact = self.store.store("bash", output)
        expected = output.encode("utf-8")

        self.assertEqual(artifact.path.read_bytes(), expected)
        self.assertEqual(artifact.size_bytes, len(expected))
        self.assertEqual(artifact.sha256,
                         hashlib.sha256(expected).hexdigest())

    def test_source_id_follows_the_file_name(self) -> None:
        """source_id 就是文件名主干：指针里那个 ID 能反查到文件。"""

        artifact = self.store.store("bash", "x" * 50)

        self.assertEqual(artifact.source_id, artifact.path.stem)
        self.assertTrue(artifact.source_id.startswith("tool_result_"))

    def test_counter_increments_per_artifact(self) -> None:
        first = self.store.store("bash", "a" * 50)
        second = self.store.store("bash", "b" * 50)

        self.assertEqual(first.source_id, "tool_result_001")
        self.assertEqual(second.source_id, "tool_result_002")
        self.assertEqual(first.path.read_text(), "a" * 50)
        self.assertEqual(second.path.read_text(), "b" * 50)

    def test_never_overwrites_existing_evidence(self) -> None:
        """计数器回零（进程重启）也不能踩旧证据——独占创建兜底。

        覆盖的后果不是"少一个文件"：指针里写着路径，覆盖等于让旧指针指向
        新内容——审计读回来是另一份东西，还查不出问题出在哪。
        """

        first = self.store.store("bash", "旧证据" * 20)

        # 模拟进程重启：新 store、计数器从零开始，但目录里已经有 001 了
        reborn = ArtifactStore(self.root, bash_max_chars=10)
        second = reborn.store("bash", "新证据" * 20)

        self.assertNotEqual(first.path, second.path)
        self.assertEqual(second.source_id, "tool_result_002")   # 跳过了 001
        self.assertEqual(first.path.read_text(), "旧证据" * 20)  # 旧的没被动


class PointerTests(TemporaryStoreCase):
    """指针：进上下文的那份必须有界，且带齐"回读"所需的一切。"""

    def setUp(self) -> None:
        super().setUp()
        self.store = ArtifactStore(self.root, bash_max_chars=10,
                                   head_chars=20, tail_chars=20)

    def test_pointer_carries_source_summary_digest_and_path(self) -> None:
        """四个来源字段各有用途：引用 / 判断值不值得读 / 校验 / 回读入口。"""

        output = "第一行：错误摘要\n" + "中间" * 100 + "\n最后一行：退出状态"
        artifact = self.store.store("bash", output)
        pointer = artifact.to_pointer(output, head_chars=20, tail_chars=20)

        self.assertIn(f"[Artifact: {artifact.source_id}]", pointer)
        self.assertIn("Summary: 第一行：错误摘要", pointer)
        self.assertIn(artifact.sha256, pointer)
        self.assertIn(str(artifact.path), pointer)
        self.assertIn("fs_read", pointer)        # 缺页中断的入口

    def test_pointer_keeps_head_and_tail(self) -> None:
        """头尾都留：关键信息常在末尾（编译错误最后几行、测试 summary）。"""

        output = "HEAD" + "x" * 200 + "TAIL"
        pointer = self.store.externalize("bash", output)

        self.assertIn("HEAD", pointer)
        self.assertIn("TAIL", pointer)
        self.assertIn("省略", pointer)

    def test_pointer_is_shorter_than_the_original(self) -> None:
        """外化必须真的换来空间——否则只是换个方式撑爆上下文。"""

        output = "x" * 100_000
        pointer = self.store.externalize("bash", output)

        self.assertLess(len(pointer), len(output))
        self.assertLess(len(pointer), 1_000)     # 头部字段 + 两个预览

    def test_summary_is_deterministic(self) -> None:
        """摘要不调模型：同输入必须同输出，否则测试与审计都不可复现。"""

        output = "首个非空行在这里\n后面还有内容"
        self.assertEqual(summarize(output), summarize(output))
        self.assertIn("首个非空行在这里", summarize(output))
        self.assertIn("共", summarize(output))

    def test_empty_output_still_summarizes(self) -> None:
        self.assertIn("空输出", summarize(""))
