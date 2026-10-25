# 第三方依赖与来源

本仓库的项目自有源码按根目录 MIT 许可证发布。没有把第三方包、模型、运行时、微信代码或二进制包含进来，也不对这些组件重新授权。安装时依赖仍按各自许可证提供。下列链接为上游项目，具体安装版本以包内 LICENSE/NOTICE 为准。

| 依赖 | 固定版本 | 许可证 / 上游 |
| --- | --- | --- |
| PyAutoGUI | 0.9.54 | BSD-3-Clause，[许可证](https://github.com/asweigart/pyautogui/blob/master/LICENSE.txt) |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0，[版本元数据](https://pypi.org/project/rapidocr-onnxruntime/1.4.4/)、[项目许可证](https://github.com/RapidAI/RapidOCR/blob/main/LICENSE) |
| Pillow | 13.0.0 | MIT-CMU，见包内附加许可；[上游](https://github.com/python-pillow/Pillow) |
| OpenCV Python | 5.0.0.93 | Apache-2.0；wheel 含其他组件许可，[上游](https://github.com/opencv/opencv-python) |
| NumPy | 2.5.4 | BSD-3-Clause、0BSD、MIT、Zlib、CC0-1.0，[上游](https://github.com/numpy/numpy) |
| ONNX Runtime | 1.31.0 | MIT 及第三方通知，[上游](https://github.com/microsoft/onnxruntime) |
| pyperclip | 1.11.0 | BSD-3-Clause，[上游](https://github.com/asweigart/pyperclip) |
| PyYAML | 6.0.3 | MIT，[上游](https://github.com/yaml/pyyaml) |

PyAutoGUI、RapidOCR 的递归依赖也由 pip 安装；包括 PyGetWindow/PyRect、PyMsgBox、PyScreeze、PyTweening、MouseInfo、six、shapely、pyclipper 等。分发整套运行环境时必须再次收集 wheel 内的 LICENSE/NOTICE，不能只复制本表。

`ocr-settings.json` 是原项目 CPU 参数的无路径抽取，配置字段来自 RapidOCR 1.4.4 默认配置。模型由该版本 wheel 提供：PP-OCRv4 检测、PP-OCRv4 识别、PPOCR mobile v2 分类。RapidOCR wheel 声明 Apache-2.0，模型源自 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE)。wheel 未为每个模型提供单独许可证，本仓库不再分发权重；使用或重新分发前应核对取得的原始模型来源和条款。配置工具只校验并引用已安装模型，不自动下载。

提取来源审计：窗口、搜索、发送、队列、提示条和 DPI 模块来自维护者自有项目；矩形捕获来自同一项目的独立实现 `bbox_capture_v1.py`。没有复制微信、接收业务、授权组件、商业 DLL 或来源不明代码。具体保留与移除见 [抽取对应表](docs/EXTRACTION.md)。
