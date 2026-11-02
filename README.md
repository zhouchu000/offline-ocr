# 离线 OCR

中文图片识别、校对与可编辑 Word 导出的桌面应用。运行期间无需联网，原始图片保持不变。

[官网](https://ocr.speedx.com.cn/) · [Windows 下载](https://github.com/zhouchu000/offline-ocr/releases/download/v0.5.6/OfflineOCR-0.5.6-Windows-x64.exe) · [完整源码与模型](https://github.com/zhouchu000/offline-ocr/releases/download/v0.5.6/OfflineOCR-0.5.6-source-with-models.zip)

当前版本：**0.5.6**。改善淡字、阴影低对比度及淡字模糊场景，完整开源包包含全部功能与 10 个本地模型。更新记录见 [CHANGELOG](CHANGELOG.md)。

## 功能

- 导入 JPG、PNG、BMP、TIFF，支持文件夹、多帧 TIFF 与任务列表增删。
- 识别正文、标题和表格；支持纸面确认、双任务并行及局部重新识别。
- 对照原图校对、复核候选、编辑表格，恢复被过滤文字。
- 导出 Word：原稿排版、连续正文、段落排版或纯文字；支持去页码、预览和后台导出。
- 手动保存口令加密项目，保留图片、识别结果及校对内容。

输入不支持 PDF格式

## 源码运行

完整源码附件 `OfflineOCR-0.5.6-source-with-models.zip` 包含全部应用源码、模型、许可和校验清单。Windows 离线安装程序另含 Python 与运行依赖，安装后无需联网。二者功能一致，无功能限制。

源码运行需要 Python 3.12：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,ocr]"
.venv\Scripts\python.exe -B -m offline_word --model-dir models
```

完整源码包已包含 `models/`，无需另行下载。直接克隆 Git 仓库时，可从同版本完整源码附件复制 `models/`；重建模型的方法见 [构建指南](docs/BUILD.md)。GitHub 自动生成的 Source code ZIP 不含发布附件。

应用运行时仅读取本地模型，校验失败时停止识别。普通模式自动增强符合条件的淡字；高精度模式提供多次识别和局部候选，由用户确认。严重失焦或已丢失笔画的文字仍需对照原图复核。

测试无需模型，安装 `.[dev]` 后执行：

```powershell
.venv\Scripts\python.exe -B -m pytest -q
```

[开发指南](docs/DEVELOPMENT.md) · [构建指南](docs/BUILD.md) · [GitHub 发布](docs/OPEN_SOURCE.md) · [参与开发](CONTRIBUTING.md)

## 数据与安全

识别与导出在本地完成，无云端接口或遥测。诊断默认关闭，仅记录耗时与内存。退出时清理独立临时会话。

保存的 `.ocrproj` 项目采用口令加密；临时图片、内存和导出的 Word 文档不在该加密范围内。口令遗失无法恢复。安全报告方式见 [SECURITY](SECURITY.md)。

## 许可

应用原创源码与文档采用 [Apache-2.0](LICENSE)。第三方库、模型、字体与图标保留各自许可，见 [NOTICE](NOTICE) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。
