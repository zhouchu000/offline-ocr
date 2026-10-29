# 开发指南

## 环境

Python 3.12；Windows 10/11 x64 为主要目标。Qt 界面、图像处理和导出依赖定义于 `pyproject.toml`，OCR 依赖放在可选的 `ocr` 组。

在联网的非保密开发机准备环境：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

测试无需模型与 OCR 依赖：

```powershell
.venv\Scripts\python.exe -B -m pytest -q
```

自动界面检查使用离屏渲染；Windows 交互检查使用原生窗口：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.venv\Scripts\python.exe -B -m pytest -q
$env:QT_QPA_PLATFORM = 'windows'
.venv\Scripts\python.exe -B -m pytest -q tests/test_releases.py
```

完整源码附件包含 `models/`。直接克隆仓库时，从同版本完整附件复制该目录。安装 OCR 依赖后启动：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,ocr]"
.venv\Scripts\python.exe -B -m offline_word --model-dir models
```

原开发交接包包含内置运行环境，可继续使用 `dev.cmd`。完整公开源码包包含模型，运行依赖使用上面的虚拟环境命令准备；直接离线使用请选择含全部运行依赖的 Windows 安装程序。

## 结构

| 路径 | 用途 |
| --- | --- |
| `src/offline_word` | 桌面应用、识别调度、校对与导出 |
| `tests` | 自动回归检查 |
| `tools` | 模型准备、构建和验收工具 |
| `packaging/windows` | Windows 安装与更新程序 |
| `licenses` | 第三方许可与来源记录 |

版本仅修改 `src/offline_word/version.py`。新增版本须在 `releases.json` 中追加日期和日志，保留历史条目。`CHANGELOG.md` 由同一份数据生成。

## 验证范围

单元测试与模拟界面检查不代表真实模型准确率。修改识别或导出后，应使用公开样例验证真实 OCR 与 Word 排版。八代酷睿、8GB 内存、Windows 10 和 WPS 应分别记录实机结果。

仓库工作流仅安装开发依赖、运行测试和代码检查；不下载模型、不处理真实资料、不自动发布。
