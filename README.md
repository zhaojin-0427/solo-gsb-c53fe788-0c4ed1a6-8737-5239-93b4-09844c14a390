# 站场进路联锁推演台

基于 **Python / FastAPI + SQLite + 原生 JavaScript** 的站场进路联锁推演 Web 应用。
调度员在站场图上办理进路，服务端以数据库事务逐条核对区段空闲、道岔位置与侧防
资源，给出信号显示与冲突依据；检测上报按全站连续序号驱动列车占用/出清与锁闭
释放；任何越序、缺号、改写都会被拒绝或使进路故障锁闭。全部裁决追加写入、
不可追溯改写，重启后自动回放事件恢复当前锁闭现场。

---

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

打开浏览器访问：

```
http://localhost:8000
```

数据保存在 Docker 卷 `interlock-data`（容器内 `/data/interlock.db`），
容器重启后进路锁闭、检测序号与事件记录完整保留。

停止：

```bash
docker compose down            # 保留数据
docker compose down -v         # 同时删除数据卷
```

## 本地直接运行（可选）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
INTERLOCK_DB=./data/interlock.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 站场导入

1. 打开页面，在 **「1 · 站场导入」** 点击 **「载入示例站场」**
   （读取内置的 `examples/demo_yard.json`），再点 **「导入」**；
   或选择自己的站场 JSON 文件后导入。
2. 导入时服务端执行严格校验，**进路不连通或引用不存在的区段/道岔/侧防/
   信号机/端点时返回 422 并拒绝导入**，页面显示具体原因。
3. 存在未终结（活动或故障锁闭）的进路时不能重新导入站场（409）。

### 站场 JSON 结构

示例：`examples/demo_yard.json`（双正线 + 东西两条单渡线，8 区段、4 组道岔、
2 项侧防资源、8 条进路）。

```jsonc
{
  "id": "demo-yard",
  "name": "示例站",
  "endpoints": [ {"id": "X", "x": 30, "y": 70, "signal": "X"} ],  // 站界端点
  "signals":   [ {"id": "X", "name": "下行进站 X"} ],
  "switches":  [ {"id": "3#", "x": 150, "y": 70} ],               // 道岔节点
  "resources": [ {"id": "P1", "name": "1号安全线（侧防）"} ],       // 侧防资源
  "sections": [
    {
      "id": "1DG",            // 区段 id
      "a": "X",  "b": "3#",   // 两端节点（端点或道岔）
      "a_leg": null,          // 道岔腿位：null=岔根，"N"=直股，"R"=弯股
      "b_leg": null,
      "detector": "D1",       // 该区段的检测器 id（可省略）
      "x": 85, "y": 52        // 图面标签位置（可省略）
    }
  ],
  "routes": [
    {
      "id": "X-XN-I",
      "from": "X", "to": "XN",
      "sections": ["1DG", "3DG", "5DG"],                        // 进路依次区段
      "switches": [ {"id": "3#", "position": "N"} ],            // 道岔位置
      "flanks": ["P1"],                                          // 侧防资源
      "signal": "X"                                              // 开放信号机
    }
  ]
}
```

**道岔拓扑约定**：每组道岔必须且仅有三个区段连接——岔根（`leg=null`）、
直股 N、弯股 R；进路只能“岔根↔N”或“岔根↔R”通过，N↔R 直跨判定为进路
不连通。进路声明的道岔位置必须与其区段实际走的腿一致，否则拒绝导入。

---

## 推演操作

1. **申请放行**：在 **「3 · 进路申请与信号」** 点「申请放行」。
   - 放行（GRANT）：返回绿灯光、开放实例号、本进路锁闭的全部区段/道岔/侧防。
   - 拒绝（DENY）：信号保持红灯，`conflicts` 逐条给出冲突依据——共用区段、
     道岔（含位置与道岔名互斥）、侧防资源被哪条进路锁闭。
   - **共用区段、道岔或侧防的进路互斥**；资源完全不相交的进路可平行办理
     （示例中 I、II 道正线可同时开放）。
2. **检测上报**：在 **「4 · 检测上报」** 手工上报，或对开放进路点
   「▶ 模拟走行一步 / ⏩ 一次走完」，按进路顺序自动生成占用、出清上报。
3. **事件与回放**： **「5 · 裁决事件」** 列表为追加写流水；点击任意事件
   弹出「裁决回放」，重建该事件发生时刻的锁闭现场，可上/下逐条翻看。

### 联锁规则（已实现）

| 主题 | 规则 |
| --- | --- |
| 放行核对 | 单一 `BEGIN IMMEDIATE` 事务内核对进路各区段空闲、道岔（名+位置）、侧防均未锁，同时禁止重复办理同一未终结进路 |
| 锁闭范围 | 道岔与侧防自开放起锁闭，**锁至末段出清**才解锁；区段按走行逐段释放 |
| 逐段释放 | 已占用的本段只有在 **后继段确认占用且本段已出清** 时才释放；末段出清后释放全部剩余锁并置进路 COMPLETED |
| 列车顺序 | 必须沿进路依次占用、依次出清；越序占用/重复占用/未占用即出清/越序出清 → 该进路 **故障锁闭（FAULT）**，原有锁全部保留 |
| 检测序号 | 全站从 1 连续编号；乱序上报先缓冲，缺号期间 **禁止新放行、禁止解锁、禁止故障复位**；缺号补齐后缓冲按序自动级联处理 |
| 幂等/冲突 | 同序号同内容幂等返回原裁决（检测器与区段两种写法等价归一）；同序号异内容返回 409 拒绝 |
| 故障复位 | 必须 **确认进路全段空闲** 后人工复位，有区段占用返回 409；复位记录追加写 |
| 不可改写 | `events` 与 `detections` 表由 SQLite 触发器禁止 UPDATE/DELETE |
| 重启恢复 | 启动时清空物化态并回放全部事件，恢复当前锁闭、占用与序号游标；任意事件点可回放裁决 |

事件类型：`STATION_IMPORT`、`ROUTE_REQUEST`(GRANT/DENY)、`DETECTION`、
`SECTION_RELEASE`、`ROUTE_COMPLETED`、`ROUTE_FAULT`、`ROUTE_RESET`。

---

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/station` | 导入站场 JSON（连通性/引用校验，失败 422） |
| GET | `/api/state` | 当前站场、序号游标、占用、锁闭、活动/故障进路 |
| POST | `/api/routes/request` | 办理进路 `{"route_id": "..."}` → GRANT/DENY + 信号 + 冲突依据 |
| POST | `/api/detections` | 检测上报 `{"seq":1,"state":"OCCUPY|CLEAR","section":"1DG"}` 或 `"detector":"D1"` |
| POST | `/api/routes/reset` | 故障复位 `{"route": "进路id 或 开放实例号"}` |
| GET | `/api/events?limit=&before=&kind=` | 追加写事件流（倒序分页） |
| GET | `/api/replay/{event_id}` | 重建该事件时刻现场（隔离的内存库，不影响在线态） |
| POST | `/api/admin/rebuild` | 手动触发事件流重建 |

---

## 测试

```bash
pip install pytest httpx
python3 -m pytest                      # 37 个功能/API 测试
python3 -m pytest -m perf -s           # 百万上报性能测试（约 20 秒）
```

性能测试实测（容器外，SQLite WAL）：累计 **1,000,000** 条检测上报后——

- 新检测上报反馈：**≈ 0.8 ms**
- 进路放行核对反馈：**≈ 1.0 ms**
- 全站状态读取：**≈ 0.7 ms**
- 重启回放 100 万事件：**≈ 1.8 s**

均远优于“1 秒内反馈”的要求。热点保障：序号/锁/区段表均有索引、
写串行化 + WAL、无开放进路期间的历史检测回放走零解析快速路径。

## 项目结构

```
app/
  main.py            FastAPI 路由（每请求一个事务）
  database.py        SQLite schema、WAL、追加写触发器、写锁
  station.py         站场模型与导入校验（连通性/引用/道岔腿位）
  interlocking.py    联锁核心：放行、检测、逐段释放、故障锁、复位
  replay.py          启动事件回放与任意事件点现场重建
  static/            原生 JS + SVG 站场图前端
examples/demo_yard.json
tests/               功能、导入校验、API 端到端、百万级性能测试
docker-compose.yml / Dockerfile
```
