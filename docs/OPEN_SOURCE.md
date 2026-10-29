# GitHub 完整开源发布

应用原创代码与文档采用 Apache-2.0。第三方库、模型、字体和图标保留各自许可，见 `NOTICE`、`THIRD_PARTY_NOTICES.md` 与 `licenses/`。

## 上传目录

源码与安装版包含相同的全部功能。默认导出完整源码和 10 个本地模型：

```powershell
python -B -m tools.build_source_release --output build/public-source
```

工具导出已提交且属于公开范围的源码，另行读取并校验 `models/` 的全部文件。结果为 `OfflineOCR-0.5.6-source-with-models.zip`、解压目录、逐文件清单和 SHA-256。保留模型来源及原始许可；不包含旧 Git 历史或本地资料。

Git 仓库使用单独的代码目录；大体积模型随 Releases 附件发布。先生成仓库上传目录：

```powershell
python -B -m tools.build_source_release --code-only --output build/github-repository
```

上传 `OfflineOCR-0.5.6-source` 目录。原开发目录保留内部记录和本地资料，不应直接推送其历史。

在 GitHub 创建空仓库后，可从公开目录建立新历史：

```powershell
git init -b main
git add .
git commit -m "Release 0.5.6 source"
git remote add origin https://github.com/<账户>/<仓库>.git
git push -u origin main
```

将账户与仓库名替换为实际值。也可使用 GitHub Desktop 发布该目录。随后创建同版本 Release，上传：

- `OfflineOCR-0.5.6-source-with-models.zip`：全部源码、模型与许可。
- Windows 离线安装程序：应用、全部模型和运行环境。
- `SHA256SUMS` 及版本验证说明。

GitHub 普通仓库[限制超过 100 MiB 的单文件](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)，[Release 单个附件须小于 2 GiB](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。使用上述附件方式可完整分发模型，不削减功能，也不要求用户配置 Git LFS。发布工具不会自动上传。

## 更新签名

源码只包含更新验签公钥。维护者生成自己的签名密钥，并将公钥写入 `src/offline_word/update_keys.py`；私钥须保存在仓库之外。第三方构建不共享原发布密钥，更新包只用于同一维护者签名的安装版本。

在独立发布分支将 `UPDATE_PUBLIC_KEY_HEX` 设为空字符串后，可使用 `tools.update_signing` 生成 Windows 用户绑定的密钥，并写入公钥。工具拒绝覆盖不匹配的原公钥；生成的新私钥不可用于原发布者签名的安装版本。

## 发布检查

- 从公开目录独立运行测试，确认未依赖原开发目录。
- 保留第三方许可，不将字体或模型重新声明为 Apache-2.0。
- 使用公开样例检查真实识别、Word 输出、安装与升级。
- 填写验证范围；未经实测的平台与硬件不标记为已通过。

静态扫描无法证明所有内容均可公开。维护者应在推送前复核变更与新增资源。
