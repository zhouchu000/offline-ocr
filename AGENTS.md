# 开发约定

先阅读 README.md、docs/DEVELOPMENT.md 和 CONTRIBUTING.md。

- 应用运行保持离线；模型须使用显式本地路径并校验。
- 保留原图与原始识别结果，人工校对优先；候选须确认后采用。
- 默认资源配置面向八代酷睿、无独显环境。
- 修改后运行相关测试；界面变化检查实际 Qt 渲染与键盘操作。
- 版本号与 releases.json 同步，使用 tools.sync_changelog 更新 CHANGELOG.md。
- 不提交真实资料、口令、私钥、个人路径、运行环境或缓存。模型随完整发布附件提供，保留来源、许可和校验清单。
