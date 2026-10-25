# 成功流程与本包的对应关系

本包从维护者自有的成功发送工具逐模块抽取，没有把重写的发送后端当成已验收版本。原流程实机成功与本包离线测试是两种不同证据。本包没有在发布过程中再次访问微信桌面或发送。

| 原模块 | 包内模块 | 保留内容 | 必要差异 |
| --- | --- | --- | --- |
| `sender_worker.py` | 同名 | 独立子进程、取消文件、120 秒超时、阶段收据；打开聊天后原文复制、粘贴、Enter 一次 | 从已安装包以 `-I -B -m` 启动子进程；业务联系人目录改为本地配置 Route 提供器 |
| `search001_once.py` | `search_once.py` | 搜索框识别/焦点核对、实时 OCR、头像形状及精确编号、Qt 弹窗关联、点击后标题核对 | 编号、DPI、捕获区域和锚点从本地配置取得；去掉旧试验 CLI；诊断不落盘；一个含固定编号的错误码改为通用码 |
| `native_win32.py` | 同名 | 原 Win32 API 包装、窗口筛选、实时保护、鼠标派发、已有搜索焦点辅助 | 仅相对导入 |
| `taskbar_click_once.py` | 同名 | `TaskbarPort`、校准绑定、原模板匹配与一次点击 | 可配置校准路径、包内导入；移除旧 CLI、停止保存诊断 PNG |
| `native_focus_once.py` | 同名 | 原 `timing`、`local_engine`、`countdown` | 只保留发送路径依赖的函数；捕获代码加载器指向包内文件并更新完整性哈希，排除独立编辑区焦点试验入口 |
| `bbox_capture_v1.py` | `capture.py` | `Native.__init__`、`Native.copy_rectangle_once` 原方法 | 删除历史 PID/HWND、会话元数据查询、旧 main、全图 OCR 和历史探测逻辑 |
| `live_capture_once.py` | 同名 | 原 `Probe`、应用标题元数据、窗口绑定函数、`Stop` | 只保留被调用定义；中间 `result.json` 经同样 JSON 编解码在内存传递；持久发送收据仍 flush/fsync |
| `simple_core.py` | 同名 | `Route`、`Ledger`、`Engine`、错误码及恢复白名单 | 删除接收业务设置与联系人目录解析，保留队列事务和批次状态逻辑 |
| `batch_notice.py` | 同名 | 原倒计时、进度、停止、执行器 idle 确认后的释放 | 相对导入；预告文案删除硬编码目标编号 |
| `notice_geometry.py` | 同名 | 原避让窗口与多屏工作区纯几何逻辑 | 源文本不变，统一换行符 |
| `notice_native.py` | 同名 | 原非激活原生提示条 | 源文本不变，统一换行符 |
| `notice_runtime.py` | 同名 | 原实时窗口/屏幕几何观察 | 包内导入与临时诊断目录 |
| `dpi_bootstrap.py` | 同名 | 与 PyAutoGUI 匹配的原 system-aware 初始化 | 源文本不变，统一换行符 |
| `simple_app.py` 的 `AppLock` | `app_lock.py` | 原本地文件锁，阻止同一运行目录的第二实例 | 作为单独模块导入，不含原业务界面 |
| 原应用 UI 的 notice/progress/end 桥接 | `desktop_app.py` | 5 秒预告 → 每条进度 → 执行器退出后结束提示的相同调用顺序 | 用最小手工批次界面代替业务界面 |
| 原 CPU OCR 配置 | `ocr-settings.json` | 全部 OCR 参数、模型文件名 | 删除绝对模型路径；本地配置工具写入当前已安装资源路径 |

## 新增的解耦边界

`configuration.py`、`app_paths.py` 处理本地路径与配置；`api.py` 将调用方显式给出的消息转换成原 Engine 接收的事件结构；`cli.py` 默认 dry-run，显式 live 才启动最小界面；`diagnostics.py` 保存进程内中间报告并丢弃诊断 PNG。新增工具只生成本地 OCR 路径和图标绑定。

没有更换窗口匹配、头像/聊天条目判定、OCR 后端或粘贴发送算法。编辑区没有新增焦点探测、空草稿校验、额外点击或输入后的重试。搜索框的原生焦点检查属于原搜索流程，仍然保留。

## 保留与缺失的身份保障

原工具通过业务接收端的本地联系人目录核对账户及源/目标聊天标识。该依赖没有公开进入本包；本包的 Route 字段只保留队列兼容结构，其值为本地作用域标签和备注，不是微信 account/talker。替代边界读取本地配置并核对一致性，GUI 阶段继续核对唯一应用窗口与精确备注。

因此，本包不声称能程序化确认当前登录账号或群真实 ID。用户需本地人工确认账号、唯一备注及其对应聊天；这项业务身份绑定差异不能被源算法一致性或离线测试覆盖。

## 对比证据与测试

发布前在私有工作目录，将只读原源码快照和抽取文件做 Python 3.12 AST 比对。只忽略位置属性、把包内相对导入层级归一化；未忽略调用、常量、参数或控制流。

共 **176 个定义相同、13 个定义因上述解耦发生调整**。数量包含类、方法和嵌套函数；不是 176 个互不重叠模块。`submit.opened`、`unique_chat_entry`、`TaskbarPort`、`NativePort`、`local_engine`、`Ledger`、`Engine`、`BatchNotice`、`NativeBanner`、`AppLock` 和两个捕获方法均在相同列表中。

机器可读清单见 [extraction-map.json](extraction-map.json)。仓库回归测试对照其中的 AST 哈希，供后续修改时发现漂移；私有原工程、历史配置和快照不公开。该指纹检查不代替行为回归或实机验证。

已移除：业务接收/API token、订单/客户处理、真实绑定、日志/数据库、原截图/头像/模板、OCR 模型和运行时、旧工程 Git 历史、独立焦点试验入口。`native_click_geometry.py` 没有进入已验证发送调用链，未纳入抽取。
