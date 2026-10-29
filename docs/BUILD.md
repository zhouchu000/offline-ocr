# 构建指南

构建输入在联网的非保密开发机准备。成品应用运行期间不下载依赖、模型或字体。

## Windows 运行环境

使用 Python 3.12 x64，按 [开发指南](DEVELOPMENT.md) 创建虚拟环境。正式运行依赖及哈希固定于 `requirements-win.lock`；不要自动升级锁定版本。

```powershell
.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: --target runtime/windows-site -r requirements-win.lock
New-Item -ItemType Directory -Force runtime-downloads
Invoke-WebRequest https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip -OutFile runtime-downloads/python-3.12.10-embed-amd64.zip
```

嵌入式 Python 的来源与哈希固定于 `runtime-downloads/python-embed.lock.json`。构建器会核验归档，不应改写锁文件以匹配未知下载。

## 本地模型

完整源码附件已包含 `models/`，可以直接使用。仅从 Git 仓库克隆或需要重建时执行：

```powershell
.venv\Scripts\python.exe -B -m tools.prepare_models --catalog models.lock.json --archives model-archives --download --output models
```

输出目录须不存在。脚本验证模型归档哈希，生成逐文件清单；不覆盖已有模型。

## 运行载荷

```powershell
.venv\Scripts\python.exe -B -m tools.build_portable `
  --runtime-zip runtime-downloads/python-3.12.10-embed-amd64.zip `
  --runtime-lock runtime-downloads/python-embed.lock.json `
  --site-packages runtime/windows-site --models models --source src `
  --extra README.md --extra LICENSE --extra NOTICE `
  --extra THIRD_PARTY_NOTICES.md --extra licenses --extra docs `
  --extra requirements-win.lock --extra models.lock.json `
  --output build/OfflineWord-0.5.6-Windows-x64
```

载荷包含独立运行环境、本地模型、许可、启动入口及逐文件校验清单。生成 Word 无需安装 Office。

## 安装程序

使用 `packaging/windows/nsis.lock.json` 固定的 NSIS 3.12。其归档必须先按锁文件校验；解压至 `toolchains/nsis` 后构建：

```powershell
.venv\Scripts\python.exe -B -m tools.build_windows_installer `
  --payload build/OfflineWord-0.5.6-Windows-x64 `
  --output build/离线图片转Word-0.5.6-Windows-x64-离线安装程序.exe `
  --makensis toolchains/nsis/makensis.exe
```

外层 WPF 安装器需要 Windows .NET Framework 4.x 编译器。安装程序使用当前用户目录，默认深色，设置提供低性能模式。

离线更新由 `tools/build_offline_update.py` 生成，需要与安装版本匹配的维护者签名密钥。私钥不随源码提供，详见 [源码发布](OPEN_SOURCE.md)。没有匹配密钥时构建完整安装程序。

## 验证

构建完成后检查文件清单、原生启动、公开样例识别、取消与重试、Word 排版、安装与升级。记录实际平台、输入及结果。构建成功不能代替目标硬件、Windows 10、WPS 或断网实机验收。

源码包与安装程序属于不同交付物。完整源码附件包含全部模型与源码；完整安装程序另包含运行环境。仅仓库上传目录省略大体积模型，模型仍随同版本 Release 附件公开分发。
