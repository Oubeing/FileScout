# FileScout

**FileScout** —— 用 Python 从零实现的 Windows 文件名即时搜索引擎。功能完整：
边打边搜、通配符/布尔查询、搜索宏、大小/日期过滤、正则、内容搜索、排序、
过滤器、书签、历史记录、CLI 与 PyQt5 桌面 GUI。索引引擎与 CLI **零第三方依赖**，
桌面界面基于 PyQt5。查询语法与 CLI 参数遵循主流文件名搜索工具的惯用法，
从其他工具迁移过来无需学习成本。

```
filescout-py/
├── FileScout.py             # 启动脚本（= python -m filescout）
├── filescout/
│   ├── __main__.py          # 入口分发：GUI / CLI / 重建索引
│   ├── config.py            # JSON 配置（索引卷、排除、书签、过滤器、历史）
│   ├── indexer.py           # SQLite 索引 + 目录遍历 + 增量重扫（错误隔离）
│   ├── search.py            # 查询语言（解析→编译→执行）
│   ├── cli.py               # 命令行接口
│   ├── app.py               # PyQt5 桌面 GUI（主窗口/选项/书签）
│   └── util.py              # 格式化、大小/日期解析、Windows 外壳动作
├── selftest.py              # 搜索引擎测试（40+ 断言）
├── gui_selftest.py          # PyQt5 GUI 测试（offscreen 真实运行，53 项断言）
├── test_completeness.py     # 索引完整性回归（长路径/错误隔离/junction/去重，13 项）
├── test_real_tree.py        # 真实目录树索引 vs os.walk 一致性校验
├── test_volume_resume.py    # 逐卷扫描/断点续扫回归（9 项）
├── test_parallel_write.py   # 三卷并发写同一 DB 的压力回归
├── test_progress_count.py   # 进度计数回归（重走计数=已扫描对象数）
├── bench_parallel.py        # 真实三盘并行扫描计时工具
└── bench_before_after.py    # 新旧索引器扫描速度对比工具
```

## 运行

```bash
# 安装唯一的第三方依赖（桌面 GUI）
pip install PyQt5

# 桌面窗口（默认）
python FileScout.py

# 等价的模块方式
python -m filescout

# 命令行搜索并退出
python FileScout.py -search "*.py TODO" -exit
python FileScout.py -search "size:>10mb" -sort size -sort-descending -exit
python FileScout.py -filter Pic -csv pics.csv -txt pics.txt

# 重建索引
python FileScout.py -rebuild

# 测试
python selftest.py           # 引擎测试
QT_QPA_PLATFORM=offscreen python gui_selftest.py   # GUI 测试
python test_completeness.py  # 索引完整性回归（长路径/错误隔离/junction/去重）
python test_real_tree.py     # 真实目录树（site-packages）索引 vs os.walk 一致性
python test_volume_resume.py # 逐卷扫描/断点续扫回归
python test_parallel_write.py # 三卷并发写压力回归
python test_progress_count.py # 进度计数回归
python bench_before_after.py # 索引速度新旧对比（复制 node_modules 到临时目录实测）
```

**环境要求**：Python 3.8+；Windows / Linux / macOS 均可运行（文件操作针对 Windows 优化）。
桌面 GUI 需要 `PyQt5`（`pip install PyQt5`，支持 5.15+）；未安装时启动 GUI 会给出
明确提示，引擎与 CLI 不受影响。

配置与索引库默认存放在 `%APPDATA%\FileScout\`（`config.json` + `filescout.db`）。
设置环境变量 `FILESCOUT_HOME` 可改为便携模式；`FILESCOUT_ROOTS` 可临时指定索引
根目录（测试用）。旧版安装目录（`EverythingPy` / `everything.db`）会在首次启动时
自动迁移到新名称，已有索引不会丢失；旧环境变量名仍作为回退兼容。

## 功能特性

- **即时搜索**：边打边显示（QThread 流式返回，防抖 150ms），亚毫秒级响应
- **查询语言**：通配符 `*` `?`、布尔运算（AND/OR/NOT/分组）、正则、大小/日期/
  属性/深度/扩展名过滤、文件类型宏、`content:` 文件内容搜索
- **结果列表**：Name / Path / Size / Extension / Date Modified（另可选
  Date Created / Accessed / Attributes），点击表头排序，文件夹优先
- **文件操作**：双击打开、Open Path、Explore、复制名称/路径、删除到回收站、
  属性对话框
- **索引管理**：多卷 + 自定义文件夹 + 排除列表（通配符）、junction 跟随开关、
  定时重扫（默认 300s）、F5 重扫、Shift+F5 强制重建、断点续扫
- **桌面体验**：过滤器菜单、书签、搜索历史 MRU、列显隐、窗口几何/列宽持久化
- **CLI**：`-search -filter -sort -max-results -csv -txt -efu -exit -rebuild
  -version`，表格/CSV/TXT/EFU 导出

## 索引与性能设计

### 增量重扫的三级加速

1. **批量事务**：写操作按线程攒批，每 64 个变化目录（或 0.4s）合并为一次事务，
   替代过去「每目录一次 COMMIT」——三卷并行扫描时 COMMIT 开销与写锁竞争都降低
   一个数量级。
2. **差异写入**：重扫一个目录时只写真正变化的行（新名字 INSERT、变化行 UPDATE、
   消失名字 DELETE + 子树清理），未变化的目录零写入。
3. **目录 mtime 跳过**：定时重扫时，修改时间未变的目录整个跳过（不 scandir、
   不读旧行）。实测（node_modules 12,388 文件 / 907 目录）：

   | 场景 | 旧实现 | 新实现 |
   |---|---:|---:|
   | 首次全量扫描 | 1.17s | **0.52s（2.2x）** |
   | 定时重扫（全走） | 0.97s | 0.99s（持平） |
   | 定时重扫（mtime 跳过） | 0.97s | **0.004s（约 250x）** |
   | 改 10 个文件后重扫 | 0.97s | **0.004s** |

   **正确性边界**（已实测 NTFS 语义）：文件的新建/删除/改名/移动会**立即**更新
   所在目录的 mtime，因此**文件名索引永不受跳过影响**；仅「内容修改」
   （大小/日期/属性变化，不改动目录条目）在跳过窗口内不刷新对应行的元数据。
   为此每个索引根每 1 小时强制一次全走刷新元数据，Shift+F5 强制重建始终全走。
   该行为可在配置 `indexes.skip_unchanged_dirs` 关闭。

### 多卷并行与稳定性

- **并行扫卷**：C:/D:/E: 同时扫描（目录遍历是 IO 密集型，各卷独立线程 + 独立
  SQLite 连接），C: 不再把 D:/E: 挡住几分钟；小盘先扫完立即可搜索。
- **断点续扫**：每卷扫完记录时间戳，重启后 300s 内已完成的卷直接跳过。
- **Windows SQLite 并发防护**：首个连接才执行 `PRAGMA journal_mode=WAL`；
  连接创建时在锁内预热读（触发 `-shm` 恢复）；写操作对瞬时 READONLY 指数退避
  重试；卷线程启动前主实例预热写一次。
- **默认排除** `*\Windows.old`、`*\$Recycle.Bin`、`*\System Volume Information`、
  `*\WinSxS\{Manifests,Catalogs,Backup,FileMaps,Temp}`（实测后者占首轮 C: 盘
  遍历的大半时间，且是组件存储簿记，用户不会搜索；可在选项 Exclude 页增删）。

### 界面上的索引状态

- **首次构建 / 强制重建**：结果列表上方显示黄色提示条，实时列出各卷状态
  （`C:\ 扫描中 312,686 · D:\ 完成 · E:\ 等待中`）——这期间搜索结果为部分数据，
  属预期行为；某卷扫完后该卷内容立即可搜索。
- **定时重扫**：完全静默（不弹提示条、不闪进度），状态栏仅短暂显示
  `Updating index...`，完成后回到 `Ready`。索引早已完整，没有必要反复提示
  「扫描中」。

### 防漏文件设计

遍历用**显式栈**而非递归（深层 `node_modules` 等不会爆栈）；单个目录列不出、
单个文件元数据读不到时记入 `Index.errors` 并跳过，**不让整个扫描事务回滚**；
根目录规范化（尾线）并去重（卷根已覆盖的文件夹根不重复遍历）；`follow_junctions`
默认开启并按 realpath 去重防环；`os.scandir` 优先使用 `\\?\` 扩展路径（>260
字符的长路径在 site-packages / node_modules 中很常见）；无法列出的目录会区分
「已删除」（清理子树）与「无权限」（保留旧行 + 报告）。

## 查询语法速查

```
todo                    名称包含 "todo"（默认大小写不敏感）
*.py  m?in.py           通配符（* 任意多字符，? 单字符；含通配符时按全名匹配）
todo !list              NOT
todo | cat  (a | b) c   OR / 分组；空格 = AND
regex:^(cli|search)\.py$  正则（() | ! 在 regex: 值内按字面处理）
path:docs  wfp:C:\x\*   搜索完整路径 / 全路径匹配
ext:py;js  ext:<>exe    扩展名过滤
folder:  file:  dupe:   仅文件夹 / 仅文件 / 重名
size:>10mb  size:10mb..20mb  size:<1gb
dm:today  dm:past7days  dm:2024-01-01..2024-02-01  dm:>2024-01-01
attrib:D  attrib:RS  attrib:<>H    目录/只读+系统/非隐藏
depth:1  depth:>2  depth:1..3      相对于索引根的深度
content:TODO            搜索文件内容（较慢）
pic:cat  zip:           文件类型宏（可附加关键词）
sort:name  sort:size-descending  sort:dm-ascending
```

## 实现要点

- **搜索引擎**（`search.py`）：词法分析 → 递归下降解析（NOT > AND > OR）→ 两级编译。
  仅涉及名称/路径/大小/日期/扩展名的查询编译为**单条 SQL**（LIKE + ESCAPE、索引扫描），
  需要正则、整词、变音符、深度、内容搜索的查询退化为 **Python 谓词流式求值**——
  两条路径都支持边算边显示（每 200 条一批推送到界面）。
- **索引器**（`indexer.py`）：`files(parent, name, ...)` 一物一行，全路径 = `parent || name`；
  卷根本身作为可搜索条目存入。每个线程一个 SQLite 连接（WAL），索引线程不阻塞搜索；
  工作线程退出时显式释放连接；重扫时对比每目录新旧行集，仅重写变化目录，并级联
  清理已删除目录的子树；线程本地写缓冲 + 批量事务 + 差异写入 + mtime 跳过（见上）。
- **GUI**（`app.py`）：PyQt5 桌面布局（菜单栏 / 搜索栏 / 匹配开关 / 过滤器 /
  可排序结果树 / 状态栏）；图标用 `QPainter` 现场绘制（无外部资源）；索引与搜索
  各自运行在 `QThread` 中，通过信号槽向界面推送增量结果；窗口几何与列宽持久化
  （base64 `saveGeometry`）；书签 / 历史 / 过滤器 / 选项对话框均为原生 Qt 组件。
  索引提示条只在索引真正不完整时出现（首次构建 / 强制重建 / 有卷未扫完），
  定时重扫静默完成。

## 测试

- `selftest.py`：构造测试目录树，对 40+ 查询逐一断言（通配符/布尔/宏/排序/选项/
  内容搜索/增量重扫/重建），全部通过。
- `gui_selftest.py`：`QT_QPA_PLATFORM=offscreen` 下**真实运行** PyQt5 GUI，
  驱动完整代码路径（构建、索引线程、流式搜索、表头排序、开关持久化、书签、
  历史弹窗、选项对话框、列显隐、剪贴板、索引提示条显隐、关闭与几何持久化），
  45 项断言全部通过。
- `test_completeness.py`：13 项断言覆盖漏文件场景——超长路径（>260 字符）深层树、
  无尾线根目录、单目录不可读不中止扫描、junction 环、重叠根去重、删除检测、
  与 `os.walk` 逐文件比对，全部通过。
- `test_volume_resume.py`：9 项断言覆盖多盘场景——每卷独立成事务、完成后写入
  `root_done` 时间戳、重启后 `skip_recent` 跳过已完成卷、被中断的卷不标记完成
  且下次重扫、强制重扫不受跳过影响，全部通过。
- `test_parallel_write.py`：三线程并发写同一数据库（模拟并行卷扫描）三轮，
  验证无 `SQLITE_READONLY`、行数与卷数一致，全部通过。
- `test_real_tree.py`：对真实 `site-packages` 树（6000+ 对象）建索引并与
  `os.walk` 结果逐条比对：0 缺失、0 幻影。
- `test_progress_count.py`：回归「进度计数按已扫描对象数增长」——对已是最新的
  索引重走时差异写入几乎不写行，计数仍须等于全量对象数且持续发出进度信号
  （旧实现会把界面计数冻结在卷根 1 上）。
- CLI 已实测：表格/CSV/TXT/EFU 导出、过滤器、排序、正则、`-rebuild`、`-version`。

### 关于「搜不到文件」的排查建议

1. **首次索引未完成**：全盘索引需要数分钟，界面黄色提示条会说明结果不完整并
   列出各卷并行进度；等提示条消失后再搜。状态栏/`-rebuild` 输出会列出无法
   读取的目录（无权限目录需以管理员身份运行才能索引）。
2. **重启后不用重等**：已扫完的卷在 300s 内会跳过，直接从下一个卷继续
   （提示条可见各卷状态）。
3. **确认索引范围**：Tools → Options → Indexes 查看卷/文件夹根；新增文件夹后
   F5 重扫。
4. **默认排除目录**：`Windows.old`、`$Recycle.Bin`、`System Volume Information`、
   `WinSxS\{Manifests,Catalogs,Backup,FileMaps,Temp}` 默认不索引（遍历式索引
   的性能取舍，可在 Exclude 页删除对应条目恢复）。
5. **junction 指向的位置**：默认 `follow_junctions = true`（遍历式索引必须跟随，
   否则经 junction 才能到达的内容会漏）。
6. **元数据列偶尔不是最新**：定时重扫会跳过未修改的目录，文件内容变化（不新增/
   删除/改名）对应的大小/日期列最多延迟 1 小时刷新，或按 Shift+F5 立即全量重建。

## 许可证

MIT，见 [LICENSE](LICENSE)。
