# 第三方组件与许可

本程序随包包含下列上游组件。各组件的原始许可和版权声明保留在其安装目录或 `runtime/Lib/site-packages/*.dist-info` 下；它们的许可不因本程序封装而改变。

完整开源包包含全部 10 个现用模型。上游模型卡许可及固定版本记录见 `licenses/model-provenance.json`，权重来源与校验见 `models.lock.json` 和随包 `models/manifest.json`。

`licenses/components.json` 列出 Windows 包的 98 个固定运行组件及原始许可位置。`licenses/wheel-licenses` 保存 Windows 轮子中声明的许可副本；未包含许可文本的轮子已从对应源码归档或上游许可文件补齐，来源与哈希保存在 `licenses/supplemental-provenance.json`。CPython 和 Qt/PySide 的许可另存于对应目录：Windows Qt 许可固定至 6.8.3 标签，macOS Qt 许可固定至 6.11.2 标签。

| 组件 | 用途 | 上游许可/位置 |
| --- | --- | --- |
| CPython 3.12.10 Windows embeddable | 本地运行环境 | PSF License；`runtime/LICENSE.txt` |
| PySide6 Essentials / Shiboken6 6.8.3（Windows）、6.11.2（macOS） | 桌面界面 | LGPLv3/GPLv3/商业多许可；开源许可文本见 `licenses/Qt-PySide-6.8.3` 与 `licenses/Qt-PySide-6.11.2`，保留对应轮子的声明 |
| PaddleOCR 3.7.0 / PaddleX 3.7.0 / PaddlePaddle 3.3.0 | OCR、版面及表格推理 | Apache-2.0；对应 dist-info 与 `licenses` |
| PP-OCRv6、PP-DocLayout、PP-LCNet、SLANet/SLANeXt、RT-DETR 模型 | 本地模型权重 | PaddlePaddle 官方模型归档；来源及哈希见 `models.lock.json`，随附上游许可见 `licenses` |
| OpenCV、NumPy、Pillow、python-docx | 图像处理、可编辑 Word 导出 | 各自 dist-info 内原始许可 |
| Microsoft Visual C++ 运行库 | Windows 原生依赖 | 从 Qt 官方轮子及 CPython 官方嵌入式包原样随附，保留原组件许可声明 |
| NSIS 3.12 | Windows 安装程序与卸载器 | zlib/libpng、bzip2、CPL-1.0 及 LZMA 特别例外；`licenses/NSIS-3.12-LICENSE.txt`，固定归档与哈希见 `packaging/windows/nsis.lock.json` |
| 其他固定依赖 | 上述组件的运行支持 | 完整名称和版本见 `requirements-win.lock`，原始许可位于各自安装元数据 |
| Noto Sans SC 可变字体 | 与 Penpot 一致的中文界面字体，应用内加载 | SIL Open Font License 1.1；`app/offline_word/assets/NotoSansSC-OFL.txt`（源码中位于 `src/offline_word/assets`）；来源与 SHA-256 见下述设计资源说明 |
| MiSans Regular / Medium | 0.3.9 中文界面字体，随应用加载 | 小米 MiSans 字体知识产权许可协议；完整原文见 `licenses/MiSans-License.pdf`；小米保留字体版权 |

MiSans 来自[小米官方字体下载页](https://hyperos.mi.com/font/zh/download/)的 [MiSans.zip](https://hyperos.mi.com/font-download/MiSans.zip)，按原样内嵌 `MiSans/ttf/MiSans-Regular.ttf` 与 `MiSans/ttf/MiSans-Medium.ttf`，不修改、不单独售卖、不安装至系统。应用设置注明“界面字体 · MiSans”。原始许可随源码和运行包保留。字体使用其原始许可，不适用应用的 Apache-2.0 许可。

- Regular SHA-256：`9c120f0a849bc0aa5048daae2a3c0f6eecd828b5b33fce682a9622833f5feea6`
- Medium SHA-256：`b03e98374e971594b0b7a9706d0704241f76e1b88556cdda79c5039ef8a638d1`

界面图标与插画由项目设计画布导出。Noto Sans SC 字体从 [Google Fonts](https://github.com/google/fonts/tree/main/ofl/notosanssc) 获取，字体二进制保持原样；其版权与 OFL 全文保留于 `src/offline_word/assets/NotoSansSC-OFL.txt`。日常运行不访问这些地址。

Qt 动态库及其插件保留为独立文件，不静态链接、不加密、不阻止用户替换与调试其开源版本。源码包包含应用源码和构建方法。Qt/PySide 对应源码可从 [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/) 以及 [Qt 6.8.3](https://download.qt.io/archive/qt/6.8/6.8.3/single/) 和 [Qt 6.11.2](https://download.qt.io/archive/qt/6.11/6.11.2/single/) 源码归档获取。

官方项目：[CPython](https://www.python.org/)、[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[PaddleX](https://github.com/PaddlePaddle/PaddleX)、[PaddlePaddle](https://github.com/PaddlePaddle/Paddle)。构建工具会访问这些公开来源或校验等价镜像；日常识别程序不访问上述链接。

## Microsoft Fluent UI System Icons

最大化与还原按钮使用 Microsoft Fluent UI System Icons 的 Maximize / Square Multiple 16 Regular 原始矢量路径，按界面主题着色。来源：https://github.com/microsoft/fluentui-system-icons 。MIT 原文随包位于 `licenses/Fluent-UI-System-Icons.txt`。
