# CTADL benchmark scripts

分析仍由正常的 CTADL CLI 完成。此目录只准备输入、重复调用 CLI、检查结果并保存日志；
没有另一套分析引擎，也不生成图表。输入生成器、比较器和正确性检查脚本都在此目录。

FlowLog 使用原生共享 SQLite 字典布局（需要已合并的 FlowLog #503）。普通 CLI、测试与
benchmark 使用同一条路径，不复制 SQLite 关系到中间数据库。`manifest.json` 的
`flowlog.sqlite_io` 和每条测量的 `sqlite_io` 标记为 `native-shared-dictionary-v1`；
缺少对应 CLI 阶段标记的样本会被拒绝。
`finalize_outputs` 记录字段名、索引、空输出和 record 展示元数据的处理，不是整库转换。

## 环境与普通使用

在 CTADL 仓库根目录执行。Linux 上需要 Python 3.10+、Rust、Soufflé 2.5（含 SQLite）、
C++ 编译器、OCaml/dune/menhir、curl、GNU time 和 Git。
可在不提交的 `benchmark/local.env` 中配置工具 PATH、CARGO_HOME、RUSTUP_HOME、
RUSTUP_TOOLCHAIN、CARGO_BUILD_JOBS 和 FLOWLOG_SRC。

```bash
bash benchmark/setup.sh
source benchmark/env.sh
```

`setup.sh` 更新相邻 `../flowlog` 的干净 `main` checkout，使用 `--locked` 构建编译器，
安装本仓库及本地前端、networkx 导出插件到 `.venv`，构建 taint-front，
并执行真实编译器的原生 SQLite 读写检查。
检查通过后，编译器版本、SHA-256 和 SQLite I/O 模式写入 `compiler.json`，
环境准备日志写入 `runs/setup-*/`。再次执行 setup 才更新 FlowLog；测量脚本不更新代码。
编译器和 `FLOWLOG_RUNTIME_PATH` 指向同一份 FlowLog。
CTADL 的分析缓存包含本地 runtime 的源码和依赖指纹；runtime 更新也会重新编译分析。

然后可以独立使用 CTADL：

```bash
ctadl import taint-front tests/taint-front/ex3.tnt -o .local/example
ctadl --directory .local/example index --engine flowlog -j 32 --timings .local/logs/index.jsonl
ctadl --directory .local/example query --engine flowlog -j 32 --timings .local/logs/query.jsonl
```

`--timings FILE` 是 CTADL 的可选日志功能，两种引擎都支持。它记录阶段时间及捕获的子进程
stdout/stderr；不传此参数时不生成这些文件。文件路径相对于调用 CTADL 时的目录。

## 运行

完整对比（全部 29 个输入、两种引擎、16 workers，含报告）可用 `sweep.sh` 一次完成并可续跑，见 [SWEEP.md](SWEEP.md)。

环境准备好后，一条命令启动；`run.sh` 只加载 `env.sh` 并调用 `run.py`：

```bash
# 最小正确性与测量验证，默认每种引擎预热一次、正式测量一次
bash benchmark/run.sh --suite smoke

# 默认 syn10 + syn25；32 workers、每种引擎三次正式测量
bash benchmark/run.sh

# 指定输入、线程数和次数
bash benchmark/run.sh --workloads syn10 --workers 1,32 --reps 3

# 多 NUMA node 的机器：先用 numactl --hardware 确认 CPU/node 编号。
# 此机器的 node 1 物理核为 32–63；CPU 和内存都固定到同一 node。
numactl --physcpubind=32-63 --membind=1 bash benchmark/run.sh --workloads libz,diary --workers 1,4,8
```

`workloads.json` 是输入清单，包含 1 个 smoke、2 个合成程序、14 个 Ubuntu 二进制、
13 个 F-Droid APK。合成程序由固定 seed 生成；真实输入固定版本并校验提取后的二进制或
APK 的 SHA-256。默认运行会准备选中的输入；也可以只下载：

```bash
python3 benchmark/prepare.py --workloads libz,diary
python3 benchmark/prepare.py --suite realistic
```

真实程序需要额外前端。固定使用 Ghidra 11.2、JDK 21.0.13+9 和 JADX 插件 0.6.2：

```bash
bash benchmark/setup-frontends.sh
source benchmark/env.sh
python3 benchmark/run.py --workloads libz,diary --reps 1
python3 benchmark/run.py --suite realistic
```

也可以使用已有的 Ghidra/JDK，在 `local.env` 配置 GHIDRA_HOME 和 JAVA_HOME。
工具版本不同的结果不要当成同一实验。原始输入来源与校验和来自
[原始输入清单](https://github.com/flowlog-rs/ctadl-flowlog/blob/b11379f89779fc295eeeb4ef133dc285cd2678b5/flowlog-backend/results/realistic/inputs.txt)；固定版本若不再可下载，准备会失败，
不会静默换用更新版本。`--offline` 要求真实输入已缓存；首次 Rust 分析编译仍可能访问 crates.io。

## 每轮如何执行

每个输入导入一次。每种线程数下，两种引擎各在独立的 facts 副本上执行 index/query 预热，
然后比较数据库（以及真实程序的 SARIF）。再使用新的 facts 副本进行正式测量。
各次运行串行；偶数次交换引擎顺序。预热记录标记为 `mode=warmup`，正式记录为 `mode=measure`。
正式阶段若发现编译事件，样本标为 `invalid_metrics`，不能作为性能数据。
`manifest.json` 记录 CPU affinity 和 NUMA 内存策略；NUMA 绑定由外层 `numactl`
施加并由编译器、前端和两个引擎继承。不同轮次应保持相同绑定。

比较使用已有的 compare_db/compare_pcode/compare_jadx/compare_sarif 工具，保留其原始输出。
不同的合法 choice-domain witness 可能造成差异；只报告键相同的 `WITNESS` 也进入复核，
脚本不会把未知差异自动判定为正确。
smoke 有差异即停止；其他输入继续测量，整轮标为 `needs_review`，退出码 4。
若只选一个引擎，不会做跨引擎正确性比较。

`needs_review` 需要进一步查原因。仅有 choice-domain 的键相同不足以证明结果正确；
可用 `benchmark/check_choice_model.py` 让 Soufflé 按 FlowLog 的选择重新推导，
检查选择的可推导性及所有输出（包括组件输出）。索引检查需要正常 CLI 的 `--all-outputs`，
并先确认增加输出没有改变原始索引结果。检查结论只适用于对应的输入、程序和版本，
不能把某次发现的差异类型加入全局忽略名单。
`run_taintfront.py` 同样将 witness 或其他比较差异标为 `needs_review`、返回 4，
并保留对应数据库；`--prune` 只删除比较相同且测试通过的数据库。

默认在记录完成后删除成功运行的工作数据库、facts 副本和报告，保留日志及生成的 Datalog。
`--keep-work` 保留这些工作目录；失败现场及比较有差异的预热数据库始终保留。真实输入和编译缓存可复用。
大量真实输入或保留数据库需要更多磁盘空间，脚本不自动删除其他实验的数据。

## 日志布局与字段

每轮创建新的 `benchmark/runs/<UTC时间>-<pid>/`，不覆盖旧运行：

```text
manifest.json                 代码版本、源码哈希、编译器哈希、环境、机器信息、输入、最终状态
ctadl-source.tar.gz            本轮 CTADL 源码快照，包含未提交的实现
analysis-sources.tar.gz        编译缓存中的转换程序、schema、UDF、编译日志和可用的锁文件
events.jsonl                  命令开始/结束事件
measurements.jsonl            每个阶段的原始测量记录
measurements.csv              同一批原始记录的 CSV，不做聚合
workloads/<id>/
  input.json                  实际使用的输入路径和 SHA-256
  import/                     导入命令与原始日志
  checks/workers-32/flowlog/   数据库/SARIF 比较日志和结论
  <engine>/workers-32/
    warmup/{index,query}/
    rep-001/{index,query}/
      command.json            argv、cwd、UTC起止、退出码、超时、wall time、启动前负载
      stdout.log              正常 CTADL 命令的原始 stdout
      stderr.log              正常 CTADL 命令的原始 stderr
      resources.txt           GNU time -v 的原始输出
      timings.jsonl           CTADL 自身的阶段及子进程记录
      timings.jsonl.commands/ 捕获的子进程原始 stdout/stderr
      measurement.json        本阶段的结构化测量
      index.dl / query.dl     实际交给分析层的 Datalog
```

| 字段 | 含义 |
|---|---|
| `wall_seconds` | 整条 CTADL 命令的时间，包括驱动、模型生成、分析和报告；预热还可能含编译 |
| `analyze_seconds` | 分析调用；FlowLog 包括输入准备、原生引擎、输出元数据处理 |
| `engine_seconds` | 分析二进制运行，包含引擎自身的 I/O 和 SQLite 输出；不是纯 fixpoint 时间 |
| `process_peak_rss_mib` | GNU time 记录的 CTADL 进程及其子进程最大 RSS，单位 MiB；不是进程 RSS 总和，也不声称是单独的引擎 RSS |
| `mode` / `repetition` | `warmup` 的 repetition 为 0；`measure` 从 1 开始 |
| `status` | 成功、失败、超时、无法启动或无效测量；缺失数值保持 null/CSV 空单元格 |

整轮退出码：0 完成、1 执行/测量失败、2 配置错误、4 比较结果需要检查、130 用户中断。
外层命令超时会终止整个子进程组并保存日志。index 失败则不执行该样本的 query。
机器、源码或输入不同的轮次保持分开，前端可以直接消费这些记录。

## 验证脚本

```bash
python3 -m unittest tests.test_timings tests.test_flowlog_runtime_cache tests.test_facts_writer tests.test_sarif_path_order tests.test_choice_model
python3 -m unittest discover -s benchmark -p 'test_*.py'
```
