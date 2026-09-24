# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 台站运行监测：按台站/通道维护采样配置、接收带时区偏移的心跳，计算每日连续缺测区间并区分短暂断链、审批维护窗口和真正离线，支持维护窗口版本化与历史报表重算。
- 灾情协同：管理灾情报告、公告、部门责任和跨部门办理状态。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取，以及数据库时间格式。

## 台站运行监测

监测中心需要把短暂断链、跨午夜维护窗口和真正的长时间离线区分开，避免缺测率错误触发巡检。

### 时间约定

- 所有观测时间、窗口时间在写入前必须携带时区偏移（如 `+08:00` 或 `Z`），服务端统一归一化为 UTC 存储，响应中的时间一律带显式 `+00:00` 偏移。
- 报表的日界由 `offset` 参数（如 `+08:00`）按本地自然日定义，返回 `day_start_utc`/`day_end_utc` 两个 UTC 半开区间端点；同一台站可以用不同偏移复算。
- 系统时间列（窗口生效时刻、审批时刻、`as_of` 锚点）使用微秒精度，保证同一秒内连续修订与审批的双时态顺序确定。

### 主要接口

- `POST /api/seismic/channels`：登记台站通道的采样间隔（须整除 86400）与短暂断链阈值。
- `POST /api/seismic/channels/{station}/{channel}/heartbeats`：批量上报带偏移的心跳；同一 UTC 时刻重复上报（含不同偏移写法）幂等判重，返回 `accepted` 与 `duplicates`。
- `POST /api/seismic/maintenance-windows`：创建维护窗口（台站级 `channel` 留空，或通道级），`compensation` 取 `excluded`（剔除）或 `imputed`（插补计为有效），支持 `idempotency_key` 防重复提交。
- `PATCH /api/seismic/maintenance-windows/{uid}`：修订窗口，只追加新版本，旧版本保留。
- `POST /api/seismic/maintenance-windows/{uid}/decision`：审批/驳回不可变；只有最新版本可审批。
- `POST /api/seismic/maintenance-windows/{uid}/cancel`：以新版本取消，窗口不再生效但历史版本仍可重算。
- `GET /api/seismic/stations/{station}/channels/{channel}/uptime?date=YYYY-MM-DD&offset=+08:00[&as_of=...]`：每日有效率报表。
- `.../uptime/versions` 与 `.../uptime/snapshot?as_of=...`：列出冻结版本、取回历史快照。

### 缺测归因与有效率

报表按采样间隔在本地日内铺设期望槽位，连续缺测槽位合并为区间，归因顺序确定：

1. 与 `as_of` 时刻已审批维护窗口的并集相交 → `maintenance`，逐段给出补偿来源（窗口 uid、版本、审批人）。重叠窗口先求并集，并集段内任一窗口为 `imputed` 则整段插补计为有效，否则从分母剔除。
2. 其余缺测段，时长不超过短暂断链阈值 → `brief_dropout`，插补计为有效，不触发巡检。
3. 其余 → `offline`，真正离线，拉低有效率且 `inspection_required=true`。

跨本地午夜的区间会向相邻日各延伸一天查找最近心跳，并在响应中以 `crosses_day_boundary` 标记、按日界裁剪，因此跨日维护和跨日断链都能确定归因。

`as_of` 选取该时刻之前生效且已审批的窗口版本：修订窗口后用新锚点得到新结果，用旧锚点重算仍还原当时的结论，报表快照只追加冻结，原始心跳与观测永不改写。


## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、台站观测和科学计算服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
