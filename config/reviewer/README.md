# reviewer CI 配置

这份配置是 `scripts/review.py` 的**可跟踪副本**，给 CI（`.github/workflows/ci.yml`
的 `review` job）使用：

```
config/reviewer/.workbuddy/experts/reviewer.json
config/reviewer/.workbuddy/skills/code-review/SKILL.md
```

本地开发仍用仓库根的 `.workbuddy/`（整目录 ignore，属于个人配置）。
CI 没有那个目录，所以把 reviewer 的人格与技能复制一份到这里；两边内容
应保持一致，改了一边记得同步另一边。

用法：

```bash
python -X utf8 scripts/review.py \
  --config-dir config/reviewer \
  --base origin/master --head HEAD \
  --fail-on blocker --out review.md
```

`--fail-on blocker` 只在出现 `[blocker]` 时返回 1；major/minor 只进报告，
不挡合并。
