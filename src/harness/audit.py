"""s23 Audit：每步留痕，不可篡改——SHA256 哈希链 + 链尾锚点。

机制（教材同款 + 它指出的坑）：
  - **哈希链**：每条 entry 的 hash = SHA256(本条 canonical JSON +
    前一条 hash)。改任何一条历史（内容/顺序），从那条起全部对不上
    ——verify() 当场报错。
  - **链只能防"改"，防不了"删尾"**：合法链的任何前缀仍是合法链，
    删掉末尾几条 verify 照样过。解法是**链外锚点** audit.head（条数 +
    链尾 hash）——验证时交叉比对，截断立现。真生产还要把锚点写到
    本机之外（远端/WORM），拿到机器的攻击者既能作恶又能改账本。
  - canonical JSON：键排序 + 紧凑分隔符 + UTF-8——同一份数据任何
    时候序列化结果字节级相同，否则 verify 会被"等价重排"误伤。

本项目的分账：权限决策的审计已在 permissions.AuditTrail（s04）；
本模块补的是**工具执行留痕**——谁在哪个会话、调了什么、返回了什么。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

GENESIS = "0" * 64


def _canonical(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def compute_hash(entry_data: dict, prev_hash: str) -> str:
    """本条 hash = SHA256(canonical(本条去 hash 字段) + prev_hash)。"""

    material = _canonical(entry_data) + prev_hash
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditChain:
    """append-only 的哈希链审计日志（JSONL）+ 链尾锚点文件。

    锚点与链同目录（教学边界：防的是"改账本"，防不了"连锚点一起
    改的本地攻击者"——生产要把锚点写远端，见模块头）。
    """

    def __init__(self, log_path: Path,
                 head_path: Optional[Path] = None) -> None:
        self._log_path = Path(log_path)
        self._head_path = Path(head_path) if head_path \
            else self._log_path.with_suffix(".head")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # 2026-11-08 reviewer 评测抓到的 blocker：sidecar 用
        # ThreadPoolExecutor 并行跑多个会话的 turn，两个 tool_end 可能
        # 同时 append——两边都读到同一个 (seq, prev)，写出重复 seq + 分叉
        # prev_hash，链永久断。用 RLock 把「读链尾→算 hash→写日志→写锚点」
        # 包成临界区；verify/entries 也拿锁，避免读到写一半的行。
        # （跨进程写者仍不防——见模块头的教学边界。）
        self._lock = threading.RLock()

    def _tail(self) -> tuple[int, str]:
        """(条数, 链尾 hash)——从盘上重放（append-only，真相在文件）。"""

        count, prev = 0, GENESIS
        if not self._log_path.exists():
            return count, prev
        with self._log_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                prev = entry["hash"]
                count += 1
        return count, prev

    def append(self, action: str, params: dict,
               result: str = "") -> dict:
        """追加一条（顺带更新锚点）。params 里别放超大数据——审计链
        是留痕不是存储，result 截 2000 字符。"""

        with self._lock:            # seq/prev 的读与写必须在同一临界区
            seq, prev = self._tail()
            entry = {
                "seq": seq + 1,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "action": action,
                "params": params,
                "result": str(result)[:2000],
                "prev_hash": prev,
            }
            entry["hash"] = compute_hash(
                {k: v for k, v in entry.items() if k != "hash"}, prev)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._write_head(seq + 1, entry["hash"])
            return entry

    def _write_head(self, count: int, tail_hash: str) -> None:
        """原子写锚点：先写同目录临时文件，再 os.replace。

        reviewer 评测抓到的 major：原来的 write_text 中途被杀会留下半截
        JSON，下一次 verify 在 json.loads 处崩掉——锚点坏掉不该让整个
        校验抛异常。临时文件同目录保证 replace 是同一文件系统上的原子操作。
        """

        payload = json.dumps({"count": count, "tail": tail_hash})
        handle, temp_name = tempfile.mkstemp(
            dir=str(self._head_path.parent),
            prefix=self._head_path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temp_name, self._head_path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def entries(self) -> list[dict]:
        with self._lock:            # 不读写一半的行
            if not self._log_path.exists():
                return []
            return [json.loads(line) for line in
                    self._log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]

    def verify(self) -> tuple[bool, int, str]:
        """整链校验：逐条重算 hash + 锚点交叉比对（防删尾）。

        返回 (是否通过, 校验条数, 人话结论)。任何一条对不上立刻停，
        并报出第一条坏在哪——审计要能指认现场。
        """

        with self._lock:
            prev = GENESIS
            for index, entry in enumerate(self.entries(), start=1):
                if entry.get("prev_hash") != prev:
                    return False, index - 1, \
                        f"第 {index} 条 prev_hash 断链（改写或重排过历史）"
                expected = compute_hash(
                    {k: v for k, v in entry.items() if k != "hash"}, prev)
                if entry.get("hash") != expected:
                    return False, index, f"第 {index} 条 hash 与内容不符（被改过）"
                prev = entry["hash"]
            # 链本身没坏 ≠ 完整：锚点比对防"删尾"。
            count, tail = self._tail()
            if self._head_path.exists():
                try:
                    head = json.loads(
                        self._head_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    # reviewer 评测抓到的 major：锚点半截/坏 JSON 时不能
                    # 让 verify 自己抛异常——审计坏了要报告，不是崩溃。
                    return False, count, "锚点文件损坏（不是合法 JSON）"
                if head.get("count") != count or head.get("tail") != tail:
                    return False, count, \
                        "锚点与链条不符——末尾记录被删过（截断）"
            return True, count, "链条完整且与锚点一致"
