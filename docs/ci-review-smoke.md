# CI reviewer 冒烟

这条 PR 只用于验证 `.github/workflows/ci.yml` 的 review job 能在真实
GitHub Actions 上跑起来：取基线、跑 `scripts/review.py`、上传报告。

验证完即关闭，不合并。
