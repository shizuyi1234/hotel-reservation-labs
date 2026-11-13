# 栖程酒店预订系统

实验三控制台实现。需要 Python 3.10 或更新版本，使用标准库 SQLite，无需安装第三方依赖。

## 构建、运行与测试

在本目录运行以下命令。Windows 上如果使用 Python Launcher，可将 `python3` 换成 `py -3`。

```sh
python3 -m compileall -q hotel
python3 -m unittest discover -s tests -v
python3 demo.py
python3 -m hotel --db demo.db --seed-demo
python3 -m hotel --db demo.db
```

有 Make 时，可用 `make build`、`make test`、`make demo` 和 `make run`。演示初始化使用当前北京时间配置 14 天库存，建议使用一个新的数据库文件。固定时间的 `demo.py` 使用临时数据库，退出后自动删除。创建自定义管理员可运行 `python3 -m hotel --db hotel.db --init-admin admin`。

## 控制台操作

输入 `help` 查看当前角色的命令，命令后加 `--help` 查看参数。演示用户是 `customer`、`frontdesk`、`admin`，演示密码为 `demo-hotel-2026`。交互登录时密码通过隐藏输入读取。金额均用整数分表示，日期采用北京时间。

1. 顾客输入 `login customer`。用 `search 南京 入住日 退房日 2 1` 搜索可订房型，再用 `quote 房型号 入住日 退房日 2 1` 得到逐晚报价。用 `book 房型号 入住日 退房日 2 1 报价token` 提交。
2. 工作人员输入 `login frontdesk` 后，可用 `orders --status pending` 查看待确认订单。用 `confirm 订单号 版本号` 确认。按允许的时间和状态用 `check-in 订单号 版本号`、`check-out 订单号 版本号` 办理整单入住和退房。
3. 顾客可用 `order 订单号` 查看版本和逐晚快照。改订用 `quote ... --order 订单号` 先取得新报价和差额，再用 `modify 订单号 版本号 房型号 入住日 退房日 人数 间数 报价token` 提交。取消使用 `cancel 订单号 版本号`。
4. 管理员用 `hotel-save`、`grant` 管理酒店与授权。工作人员用 `room-save`、`inventory-save` 维护房型和日历库存。管理员用 `closure-add` 设置停业区间，员工通过 `cases` 和 `case-resolve` 跟进。
5. 工作人员和管理员可以用 `statistics 开始日 结束日 --include-rows` 核对汇总明细，用 `audit` 组合筛选日志。统计及停业区间的结束日不包含在范围内。
6. 写操作可用 `--request-id` 指定重试标识。报价变化时应核对返回的新报价并使用新标识。用 `tick` 主动处理到期和未到店订单。

## 目录和运行约定

| 模块 | 职责 |
| --- | --- |
| `hotel/domain.py` | 入住区间、报价、状态迁移和业务错误 |
| `hotel/accounts.py` | 账号、会话、角色和酒店范围 |
| `hotel/catalog.py`、`hotel/search.py` | 酒店与房型、日历库存、营业限制和报价 |
| `hotel/orders.py` | 下单、改退、履约、幂等回执和到期处理 |
| `hotel/queries.py` | 订单查询、经营统计及审计日志 |
| `hotel/storage.py`、`hotel/schema.sql` | SQLite 事务、约束和存储 |
| `hotel/cli.py` | 控制台和批处理入口 |

预订写入在 SQLite 写事务中重新验证库存和报价，库存以逐晚明细计算。控制台用请求触发的到期处理和手动 `tick` 替代常驻调度器。搜索在确认截止时自动排除到期占用，即使状态写入尚未触发。价格快照按分保存，查询统计从事件和间夜明细计算。单次住宿限制为 90 晚。提前退房按北京时间 14:00 区分当晚是否计入已用，入住当日退房仍计当晚。控制台是本地单机程序，会话保存在当前进程。

## 网页版

在本目录启动服务。使用全新数据库时先初始化演示数据。

```sh
python3 -m hotel --db web-demo.db --seed-demo
python3 -m hotel.web --db web-demo.db
```

浏览器打开 <http://127.0.0.1:8000>。顾客 `customer`、员工 `frontdesk`、管理员 `admin` 的演示密码均为 `demo-hotel-2026`。演示库存从初始化当天开始覆盖 14 天。顾客可以注册、查房、确认报价和改退，员工可维护酒店并办理订单，管理员可授权、登记停业和查看审计记录。各角色都使用同一 SQLite 数据库。

网页和接口由同一 Python 进程提供。会话在服务器进程内保留，重启后需要重新登录，业务记录持久化在 SQLite 文件中。开发服务默认只监听本机。实际联网部署应使用自己的数据库和账号，并在 HTTPS 反向代理后运行，启用 `--secure-cookie`。
