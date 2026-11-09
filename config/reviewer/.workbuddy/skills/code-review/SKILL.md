---
title: code-review
summary: 评审本项目代码的检查清单（先结构后细节，附本仓库的硬约定）
read_when:
  - 代码评审
  - review
  - 检查代码
  - 重构
  - 看代码质量
agent_created: false
permissions:
  tools: [fs_read, fs_find, fs_glob]
  paths:
    read: ["**"]
    write: []
---

# 代码评审清单（本项目）

## 先看结构，再看细节

1. `fs_glob **/*.py` —— 看模块怎么划分的
2. 读每个模块的**模块级 docstring**：它讲清楚「**为什么**要有这个模块」了吗？
   只说「这个模块处理 X」是不够的
3. 类的公共方法是否都有 docstring？参数说明进不进 schema？（跑一遍测试就知道）

## 本项目的硬约定（违反就是 bug，不是风格问题）

| 约定 | 怎么查 |
|---|---|
| **依赖方向**：`src/harness/` 不能 import `scripts/` | `fs_find "from scripts" src/` 应该无结果 |
| **原子写**：所有落盘走 `file_tools.atomic_write_text` | 搜 `write_text(` / `open(` |
| **沙箱**：fs_* 的路径必须过 `_resolve_safe` | 看新的文件工具 |
| **失败要喊**：静默降级比报错难查 | 搜 `except.*:\s*pass` |
| **测试**：新增能力必须有对应测试 | 对比工具名 / 测试文件 |
| **常量而非字面量**：路径、上限、阈值 | 搜魔数 |

## 常见问题（这个项目真实出现过）

- 注释在解释「**是什么**」而不是「为什么」——前者代码自己会说
- 上限写死（应该走环境变量或模块常量，便于测试与调参）
- `except Exception: pass` —— 吞掉异常后，排查成本翻十倍
- 新增工具没加进 `SAFE_TOOLS` → 落到 `default.deny`，功能静默失效
- 改了行为没更新测试期望值 → 下次跑测试才发现

## 输出要求

评审结论要**可执行**：指出文件与行号，说清「什么会出错」，
不要只说「建议优化」。
