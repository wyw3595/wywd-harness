"""scripts：冒烟脚本目录。一律不进 tests/——花钱、要网络、结果不确定。

唯一例外：shell.py（SidecarShell 壳类）是"可测的编排件"——它不含 UI
胶水（input/print/cl.Message），只有进程生命周期与 RPC 包装，配单测
tests/test_shell.py（注入假 client/假 process，不真 spawn）。
"""
