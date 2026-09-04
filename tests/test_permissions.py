"""src.harness.permissions 决策层的离线单元测试(练习 s04 · A 批)。

覆盖教材锁定的治理契约(本批只测决策层):
  1. 硬拒绝是带 rule_id/reason 的结构化决定;
  2. 未匹配工具默认拒绝(default.deny, fail-closed);
  3. 有序规则 first-match-wins:危险命令不因后置审批规则而降级;
  4. WorkspaceScope:相对穿越 / 绝对外部路径被拒,界内放行,
     尚不存在的目标允许解析(strict=False 语义);
  5. resolve_permission:ALLOW/DENY 不调 approver,DENY 不可覆盖,
     ASK 只此一次询问,批准/拒绝落成不同 approval_status;
  6. path.write_ask:界内写 -> ASK(改状态必须人点头),理由带目标路径,
     越界写先被 outside 规则硬拒,没挂 scope 时 fail-closed;
  7. tool.allow_safe:免审批白名单显式放行,名单外依旧 default.deny。
"""

import unittest
from pathlib import Path

from src.harness.permissions import (
    PermissionAction,
    PermissionDecision,
    PermissionPolicy,
    PermissionResolution,
    ToolRequest,
    WorkspaceScope,
    build_default_policy,
    resolve_permission,
)


def req(name: str, arguments: object, rid: str = "t1") -> ToolRequest:
    """造一个请求,默认 tool_use_id 固定,便于断言语义聚焦。"""

    return ToolRequest(tool_use_id=rid, name=name, arguments=arguments)


class PolicyDecisionTests(unittest.TestCase):
    """deny 结构化 / 默认拒绝 / 顺序优先 / 纯决策。"""

    def setUp(self) -> None:
        self.policy = build_default_policy()  # scope=None:路径规则跳过

    def test_hard_deny_is_structured(self) -> None:
        """危险命令命中 hard_deny:结构化决定,rule_id/reason 都在。"""

        result = self.policy.decide(req("bash", {"command": "sudo rm -rf /"}))
        # 命中"危险集"这条,而不是默认拒绝或审批
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "bash.hard_deny")
        # reason 是人话,命令名要出现在里面,才说明这次请求有据可查
        self.assertIn("sudo", result.reason)

    def test_unknown_tool_default_deny(self) -> None:
        """没配规则的任何工具 -> default.deny(新增工具忘了配=拒绝)。"""

        result = self.policy.decide(req("curl", {"url": "https://x"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "default.deny")
        self.assertIn("curl", result.reason)

    def test_hard_deny_wins_over_ask(self) -> None:
        """顺序即安全:危险 bash 即便也匹配后面的审批规则,仍命中 deny。"""

        result = self.policy.decide(
            req("bash", {"command": "dd if=/dev/zero of=/dev/sda"})
        )
        self.assertEqual(result.rule_id, "bash.hard_deny")

    def test_non_dict_arguments_fail_closed(self) -> None:
        """参数不是 dict(连 command 都取不到)-> 不许进入审批,默认拒绝。"""

        result = self.policy.decide(req("bash", "just a string"))
        self.assertIs(result.action, PermissionAction.DENY)

    def test_ask_rule_for_plain_bash(self) -> None:
        """平凡 bash(非危险)-> ASK(需要人工同意才能执行)。"""

        result = self.policy.decide(req("bash", {"command": "ls -la"}))
        self.assertIs(result.action, PermissionAction.ASK)
        self.assertEqual(result.rule_id, "bash.requires_approval")

    def test_policy_decide_is_pure(self) -> None:
        """纯决策:decide 只产 PermissionDecision,不碰 UI/执行。"""

        decision = self.policy.decide(req("bash", {"command": "ls"}))
        self.assertIsInstance(decision, PermissionDecision)


class WorkspaceScopeTests(unittest.TestCase):
    """路径作用域:三种越界 + 界内放行 + 不存在的目标可解析。"""

    def setUp(self) -> None:
        self.scope = WorkspaceScope(Path("/workspace/harness"))

    def test_relative_escape_rejected(self) -> None:
        """../outside/secret.txt 穿越被拒。"""

        with self.assertRaises(PermissionError):
            self.scope.resolve("../outside/secret.txt")

    def test_absolute_outside_rejected(self) -> None:
        """绝对路径 /etc/hosts 在工作区外 -> 拒绝。"""

        with self.assertRaises(PermissionError):
            self.scope.resolve("/etc/hosts")

    def test_inside_allowed(self) -> None:
        """界内相对路径解析成工作区下的绝对路径。"""

        resolved = self.scope.resolve("src/harness/main.py")
        # as_posix() 统一正斜杠,避免 Windows 反斜杠把比对搅乱
        self.assertTrue(resolved.as_posix().endswith("src/harness/main.py"))

    def test_nonexistent_target_allowed(self) -> None:
        """目标尚不存在(待写入)也能解析——strict=False 的语义。"""

        # 文件不存在但能解析,不抛异常
        resolved = self.scope.resolve("logs/new.txt")
        self.assertTrue(resolved.as_posix().endswith("logs/new.txt"))

    def test_root_is_normalized(self) -> None:
        """工作区根自带 ".." 时也要归一化,不能自己先逃出去。"""

        scope = WorkspaceScope(Path("workspace/../workspace"))
        # 归一化后根是绝对路径,界内解析不抛异常
        scope.resolve("a.txt")


class ResolvePermissionTests(unittest.TestCase):
    """approver 只在 ASK 出场;DENY 不可覆盖。"""

    def _counting_approver(self):
        calls = {"n": 0}

        def approver(decision: PermissionDecision) -> bool:
            calls["n"] += 1
            return True

        return approver, calls

    def _decision(self, action: PermissionAction) -> PermissionDecision:
        return PermissionDecision(
            req("bash", {"command": "ls"}), action, "test.rule", "理由"
        )

    def test_allow_skips_approver(self) -> None:
        """ALLOW 直接放行,approver 零次调用。"""

        approver, calls = self._counting_approver()
        result = resolve_permission(self._decision(PermissionAction.ALLOW), approver)
        self.assertTrue(result.allowed)
        self.assertEqual(result.approval_status, "not_required")
        self.assertEqual(calls["n"], 0)  # 免审批:老板没被惊动

    def test_deny_not_overridable(self) -> None:
        """DENY 即使 approver 返回 True 也不放行——用户无权覆盖系统边界。"""

        approver, calls = self._counting_approver()
        result = resolve_permission(self._decision(PermissionAction.DENY), approver)
        self.assertFalse(result.allowed)
        self.assertEqual(calls["n"], 0)  # 硬拒绝:老板连出场机会都没有

    def test_ask_approved(self) -> None:
        """ASK + approver 同意 -> approved + allowed。"""

        result = resolve_permission(
            self._decision(PermissionAction.ASK), lambda decision: True
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.approval_status, "approved")

    def test_ask_rejected(self) -> None:
        """ASK + approver 拒绝 -> rejected + blocked,且带着原决策。"""

        decision = self._decision(PermissionAction.ASK)
        result = resolve_permission(decision, lambda d: False)
        self.assertFalse(result.allowed)
        self.assertEqual(result.approval_status, "rejected")
        # identity 断言:返回的 decision 就是传进去的那个对象,没被偷换
        self.assertIs(result.decision, decision)


class ReadAllowPolicyTests(unittest.TestCase):
    """教材式 path.read_allow:只读工具 + 界内 = 显式放行。"""

    def setUp(self) -> None:
        self.policy = build_default_policy(scope=WorkspaceScope(Path("/workspace")))

    def test_read_inside_is_allowed(self) -> None:
        """界内的只读工具请求命中 read_allow,而不是被 default.deny。"""

        result = self.policy.decide(req("list_dir", {"path": "src"}))
        self.assertIs(result.action, PermissionAction.ALLOW)
        self.assertEqual(result.rule_id, "path.read_allow")

    def test_read_escape_still_denied(self) -> None:
        """越界的只读请求走 outside 规则 DENY,轮不到 read_allow。"""

        result = self.policy.decide(req("read_file", {"path": "../../etc/passwd"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "path.outside_workspace")

    def test_no_scope_fails_closed(self) -> None:
        """没挂 scope 的 policy:read_allow 不启用 -> default.deny。"""

        result = build_default_policy().decide(req("list_dir", {"path": "src"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "default.deny")


class WriteAskPolicyTests(unittest.TestCase):
    """path.write_ask:界内写 -> ASK;越界写先被硬拒;没 scope 兜底拒绝。"""

    def setUp(self) -> None:
        self.policy = build_default_policy(scope=WorkspaceScope(Path("/workspace")))

    def test_write_inside_is_ask(self) -> None:
        """界内写文件命中 write_ask——改状态必须人点头,不是 ALLOW。"""

        result = self.policy.decide(req("write_file", {"path": "notes/todo.txt"}))
        self.assertIs(result.action, PermissionAction.ASK)
        self.assertEqual(result.rule_id, "path.write_ask")

    def test_write_ask_reason_carries_path(self) -> None:
        """ASK 理由带着目标路径——审批人要看证据再点头(explain 的本职)。"""

        result = self.policy.decide(req("write_file", {"path": "notes/todo.txt"}))
        self.assertIn("notes/todo.txt", result.reason)

    def test_write_outside_denied_before_ask(self) -> None:
        """越界写走 outside 硬拒,轮不到审批——顺序即安全语义。"""

        result = self.policy.decide(req("write_file", {"path": "../evil.txt"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "path.outside_workspace")

    def test_write_without_scope_fails_closed(self) -> None:
        """没挂 scope:写规则不启用 -> default.deny(而不是悄悄放行)。"""

        result = build_default_policy().decide(
            req("write_file", {"path": "notes/todo.txt"})
        )
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "default.deny")


class WorkspaceForbiddenTests(unittest.TestCase):
    """禁区段判定(s04 追加):与 file_tools._resolve_safe 同款——查整条动线。"""

    def setUp(self) -> None:
        self.scope = WorkspaceScope(Path("/workspace"), forbidden_parts={".env", ".git"})

    def test_deep_forbidden_part_detected(self) -> None:
        """路径中段撞禁区也算——.git/config 撞的是 .git 那段,不是终点名。"""

        self.assertTrue(self.scope.has_forbidden_part(".git/config"))

    def test_clean_inside_not_forbidden(self) -> None:
        """界内普通路径不撞禁区。"""

        self.assertFalse(self.scope.has_forbidden_part("src/main.py"))

    def test_escape_is_not_forbidden(self) -> None:
        """越界不算禁区:那是 outside 规则的管辖范围,这里只答"界内禁区吗"。"""

        self.assertFalse(self.scope.has_forbidden_part("../outside.txt"))

    def test_no_forbidden_configured(self) -> None:
        """没配禁区的 scope 永远 False——不凭空拦截。"""

        plain = WorkspaceScope(Path("/workspace"))
        self.assertFalse(plain.has_forbidden_part(".git/config"))


class ForbiddenZonePolicyTests(unittest.TestCase):
    """path.forbidden_zone:界内禁区在权限层就 DENY,轮不到 read/write 规则。"""

    def setUp(self) -> None:
        self.policy = build_default_policy(
            scope=WorkspaceScope(Path("/workspace"), forbidden_parts={".env", ".git"}),
        )

    def test_read_forbidden_is_denied_not_allowed(self) -> None:
        """读禁区:不再被 read_allow 放行——DENY 且拒绝理由带目标路径。"""

        result = self.policy.decide(req("read_file", {"path": ".env/secret"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "path.forbidden_zone")
        self.assertIn(".env/secret", result.reason)

    def test_write_forbidden_is_denied_not_ask(self) -> None:
        """写禁区:不升级成 ASK——改 .git 没得商量,直接 DENY。"""

        result = self.policy.decide(req("write_file", {"path": ".git/config"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "path.forbidden_zone")

    def test_forbidden_zone_beats_read_allow(self) -> None:
        """顺序:禁区规则排在 read_allow 之前,界内禁区漏不进免审批白名单。"""

        result = self.policy.decide(req("list_dir", {"path": ".git"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "path.forbidden_zone")

    def test_clean_path_unchanged(self) -> None:
        """没撞禁区的正常读,照旧 read_allow——禁区只拦禁区。"""

        result = self.policy.decide(req("read_file", {"path": "src/main.py"}))
        self.assertIs(result.action, PermissionAction.ALLOW)
        self.assertEqual(result.rule_id, "path.read_allow")


class SafeToolsPolicyTests(unittest.TestCase):
    """tool.allow_safe:显式白名单免审批;名单外依旧 default.deny。"""

    def test_safe_tool_is_allowed(self) -> None:
        policy = build_default_policy(safe_tools=["calc"])
        result = policy.decide(req("calc", {"expression": "1+1"}))
        self.assertIs(result.action, PermissionAction.ALLOW)
        self.assertEqual(result.rule_id, "tool.allow_safe")

    def test_unlisted_tool_still_denied(self) -> None:
        """白名单只放行名单内的工具;名单外的新工具照样默认拒绝。"""

        policy = build_default_policy(safe_tools=["calc"])
        result = policy.decide(req("curl", {"url": "https://x"}))
        self.assertIs(result.action, PermissionAction.DENY)
        self.assertEqual(result.rule_id, "default.deny")


if __name__ == "__main__":
    unittest.main()