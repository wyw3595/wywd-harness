"""
============================================================
Harness 练习 01：一次最小运行
============================================================

📚 为什么需要它
  Harness 像一个运行总管。每个任务经过它时，都要拿到自己的编号，
  并返回统一格式的结果。以后接入模型和工具时，调用方不需要改变。

📚 本课核心
  - 用 @dataclass 定义“数据容器” RunResult。
  - 用 uuid4() 为一次执行生成不重复的 run_id。
  - 用函数和 return 把任务处理逻辑封装起来。

面试要点：dataclass 适合表示结构化数据；函数的返回类型让调用方
明确能获得什么结果。

完成标志：运行 tests/test_main.py 中的测试时通过 1 项测试。
运行方式：
  .\\.venv\\Scripts\\python.exe -X utf8 -m src.harness.main
  .\\.venv\\Scripts\\python.exe -X utf8 -m unittest discover -s tests -v
============================================================
"""

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from src.harness.models import FakeModel, Model


@dataclass
class RunResult:
    """一次 Harness 运行的最小结果。"""

    run_id: str
    task: str
    output: str
    # 练习 16 起 "failed" 也是合法结局：契约先承认失败，循环才有值可写。
    # s01 整合：再加 "truncated"——provider 截断输出，沉默地当完整答案
    # 是骗用户；显式承认截断，才是诚实的完成。
    status: Literal["completed", "max_steps", "failed", "truncated"] = "completed"
    messages: list[dict] = field(default_factory=list)
    # 练习 18：本次运行累计的 token 用量（跨轮求和），由 run_agent 填写
    # ——成本从"听说很贵"变成看得见的数字。
    usage: dict[str, int] = field(default_factory=dict)


def run(task: str, model: Model | None = None) -> RunResult:
    """执行一个任务，并返回统一结构的运行结果。"""

    if model is None:
        model = FakeModel()

    run_id = str(uuid4())
    # 练习 08 起协议收消息列表：把任务包装成一条 user 消息。
    return RunResult(
        run_id=run_id,
        task=task,
        output=model.generate([{"role": "user", "content": task}]).text,
    )


if __name__ == "__main__":
    result = run("第一次运行 harness")
    print(result)
