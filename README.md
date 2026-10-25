# WeChat Desktop Sender

从已有 Windows 微信桌面发送工具中抽取的 Python 包：任务栏唤起 → 实时截图、本地 OCR 查找唯一编号备注 → 定位聊天条目 → 核对打开后的备注 → 原文粘贴一次、Enter 一次。保留原实现的发送顺序、窗口识别、头像几何算法、5 秒批次预告、持续非激活提示条和持久化队列。

**验证范围：原流程仅在维护者的一台 Windows 电脑、微信 4.1.13.65 上实际发送成功。本仓库抽取版完成的是离线回归，尚未重新实机发送验收。** 不承诺多版本或任意电脑兼容。Windows 10/11 x64 是后续兼容性验证目标，不是已测支持矩阵。缩放、多屏、主题、任务栏布局和微信 UI 更新都可能影响定位；首次使用必须本地校准。

English: Extracted Windows WeChat GUI sending flow with local OCR, batch notices and durable no-blind-retry state. The original flow was observed working on one machine with WeChat 4.1.13.65. This packaged extraction has offline regression coverage only.

## 范围与行为

- 通过已有 Win32 窗口元数据选择唯一微信窗口、使用本地校准图标唤起任务栏窗口。结果条目依据完整编号和相邻头像形状定位，不以“群聊”或“最常使用”分类作为准入条件。
- 保留 PyAutoGUI **0.9.54** 与 `rapidocr-onnxruntime` **1.4.4** 的原调用。当前输入合同是三个 ASCII 数字的唯一备注；没有声称任意文字备注均可输入。
- 选中聊天后沿用微信自身的编辑区焦点行为，直接复制原文、`Ctrl+V`、`Enter` 一次。没有追加编辑区焦点探测、空草稿判断、额外点击或二次回车。原搜索框焦点核对保留。
- 每批最多 10 条；先 5 秒预告，随后提示条持续显示进度。预告窗口可激活；发送期间的原生提示条使用 `WS_EX_NOACTIVATE`，直到执行器结束才解除提示。
- 结果记录为 `SUBMITTED_UNVERIFIED`，只表示本地 Enter 调用返回，没有微信服务器确认。中断或可能已输入时标为 `UNKNOWN`，不会自动再发。
- 本包只接受调用方提供的纯文本消息。没有接收端、订单识别、客户数据、微信数据库读取、DLL 注入或授权绕过。

## 安装与默认演示

需要 **Python 3.12**。微信 GUI 操作仅面向 Windows。先在新虚拟环境安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m wechat_desktop_sender --config examples/config.example.json --messages examples/messages.example.json
.\.venv\Scripts\python -m unittest discover -s tests -v
```

普通 CLI 调用是 dry-run：只在内存中校验消息和幂等规则，不读取屏幕、不启动微信、不写入剪贴板、不发送。示例编号、消息 ID、正文和作用域均为虚构；配置 `calibrated: false`，不能直接用于实发。图像测试没有安装可选依赖时会显示跳过。

安装 GUI/OCR 依赖时使用：

```powershell
.\.venv\Scripts\python -m pip install -e ".[desktop]"
```

版本锁定按原隔离执行环境核对，见 `pyproject.toml`。本次核对的 Python 为 3.12.14；Pillow 13.0.0、OpenCV 5.0.0.93、NumPy 2.5.4、ONNX Runtime 1.31.0、pyperclip 1.11.0、PyYAML 6.0.3。不在发送时安装依赖或下载模型。

## 首次本地校准

详细步骤见 [CALIBRATION.md](docs/CALIBRATION.md)。仓库不附带任何真实校准截图或已可发送的配置。

1. 复制示例为本地配置，设置自己的唯一三位备注和本地幂等作用域；在微信中人工确认当前登录账号及备注对应的聊天。
2. 根据当前显示缩放和微信窗口，填写配置中的 DPI、搜索条、标题条与搜索框锚点。不要沿用另一台机器的坐标。
3. 从自己本地查看的主任务栏截图裁出当前微信图标，生成本地图标绑定。只支持完全可见的主屏底部任务栏；自动隐藏、合并预览或不唯一的匹配会停止。
4. 生成 CPU OCR 配置。只引用已经安装且哈希匹配的三个模型；不把它们加入仓库。

```powershell
.\.venv\Scripts\python tools/configure_ocr.py --output .local/ocr/cpu-only-config.yaml
```

本地配置完成后，显式实发入口为：

```powershell
.\.venv\Scripts\python -I -m wechat_desktop_sender --config .local/sender.local.json --messages .local/messages.local.json --live
```

路径相对于配置文件所在目录解析；从 `examples/` 移到 `.local/` 后应重新核对。执行时不要操作鼠标和键盘；停止按钮可结束后续动作。当前窗口、搜索结果或输入状态发生原实现不允许的变化时会暂停或停止。任务栏、提示条或界面布局不符合条件时，不应通过删除校验来强行继续。

## 最小 API

```python
from wechat_desktop_sender.api import dry_run
from wechat_desktop_sender.configuration import load

config = load("examples/config.example.json")
result = dry_run(config, [{"id": "fictional-api-message", "text": "演示原文\n保留换行"}])
print(result)  # DRY_RUN, desktop_calls=0
```

GUI 批处理入口是 `desktop_app.launch(configuration, messages)`，应先设置 `WECHAT_SENDER_CONFIG`、调用 `load(..., live=True)`，且在主线程启动。推荐使用 CLI 完成这些步骤。底层 `Engine` 保留原有 sender、notice、before_send、after_batch、validate_route 等注入接口，便于 mock 和集成；不要绕过批次提示直接调用内部单条 sender。

## 停止、暂停和异常恢复

保留原有自动暂停语义：单条 `BLOCKED` 或 `UNKNOWN` 会暂停后续发送；“恢复已暂停队列”重新核对本地配置后处理尚未提交的队列。可重排的旧任务仅限原白名单原因，并且必须有 `submission_possible=false` 的完成收据。`UNKNOWN` 不会回到待发队列，需人工在微信核对结果。停止是终止当前执行器；再次打开程序会恢复持久队列并把遗留 `SUBMITTING` 标成 `UNKNOWN`。

消息以本地作用域、目标及原文 SHA-256 去重；相同正文使用新 ID 也不会再次发送。请保持作用域稳定，不能通过删数据库、改作用域或删除收据来“修复”未知状态。保留原应用文件锁，阻止同一运行目录的第二个实例；不要用不同运行目录并发控制同一桌面。当前没有手工随时暂停后原地续跑的额外机制，也没有送达回执或撤回。

## 数据与日志

公开包只含代码、文档、虚构配置和合成测试。截图、OCR 明文及窗口诊断仅在进程内参与定位；诊断 PNG 不保存。状态输出不含正文、账号或窗口句柄。用于队列恢复的本地 SQLite **包含消息原文**，本地配置包含备注；它们不是脱敏日志，应保留在自己的私有目录。剪贴板沿用原实现，会保存最后复制的原文；没有额外恢复剪贴板操作。

配置中的 `ledger_namespace` 是本地幂等标签，不是微信账号 ID。原业务联系人目录和接收端已经移除；包只校验本地配置一致性、GUI 应用窗口及精确备注，**不验证微信登录身份或实际群 talker**。切换登录账号或修改备注绑定时应先停止并人工核对。

## 来源与限制

[EXTRACTION.md](docs/EXTRACTION.md) 列出“成功版 → 抽取版”的逐模块对应与改动，包含移除的业务身份绑定。关键算法和发送回调经源 AST 对比，避免把新实现当作成功版。抽取包仍需要独立实机验收；原始成功结果不等于抽取包已通过所有场景。

项目自有代码采用 [MIT](LICENSE)。依赖与模型保持各自许可，见 [第三方声明](THIRD_PARTY_NOTICES.md)。这是非官方工具，未获得微信官方兼容性或稳定性承诺。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
