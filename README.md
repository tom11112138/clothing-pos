# 四店服装零售 POS

FastAPI + Vue + PostgreSQL 的局域网零售系统。四家门店分别维护库存，总部管理员可查看汇总并进行跨店调拨。

## 已实现

- SPU 款式与颜色/尺码 SKU
- 每店独立库存、低库存筛选和 Code128 条码
- 原子扣库存、防超卖、多商品收银和销售请求防重
- 单 SKU 与多 SKU 调拨、在途库存、部分收货和拒收
- 盘点单及盘点期间库存冲突检测
- 部分/整单退款、独立退款单和退款请求防重
- 账号权限、安全 Cookie 会话、CSRF 防护和操作审计
- PostgreSQL 连接池及一键启动

## 一键启动

双击 `start.bat`。第一次启动会创建虚拟环境、安装固定版本依赖，并询问 PostgreSQL 和初始管理员配置。

- 本机：http://127.0.0.1:8000
- 局域网：`http://服务器内网IP:8000`
- API 文档：http://127.0.0.1:8000/docs

正式服务器应配置 HTTPS，并在 `.env` 中设置 `COOKIE_SECURE=true`。不同地点的门店应通过 VPN 访问，不要把 8000 端口直接开放到公网。

## 库存原则

`Inventory(sku_id, store)` 是唯一的业务库存来源。旧的 `Sku.quantity` 只为兼容早期数据保留，新库存变化必须经过入库、出库、销售、退款、调拨或盘点接口，以保证流水完整。

批量创建 SKU 时填写的初始库存进入 1 号店。更推荐初始库存填 0，创建后通过正式入库功能录入实际到货门店。

批量调拨每次收货都使用独立的 `client_request_id`。同一笔收货重试返回原结果，不再增加库存或重复记流水；下一批实际到货使用新编号。浏览器会在当前标签页保存待确认的收货，刷新页面或关闭收货弹窗后可以继续确认。不要在结果未确认时清除浏览器存储或关闭标签页。

升级收货防重功能后，需要重启后台并刷新所有收货终端的页面。启动时自动创建 `stocktransferreceipt` 表，保留现有调拨和库存数据。旧客户端缺少请求编号时会被拒绝，不会写入库存。

收银时必须等全部条码查询完成后才能结算。结算等待期间，扫码、改数量、删商品、清空、切店和登出均暂停；购物车有商品时，需先结算或清空再切店。清空、退出登录或更换门店后，旧条码查询结果不会重新加入购物车，也不会影响新账号的订单。

盘点现在整单一次提交：实盘数、库存差异、流水及完成状态共同成功或回滚。录入、完成和取消使用同一张单据锁及版本检查，过期页面不会覆盖其他管理员的修改。冲突后保留页面上的实盘数，重新加载前会要求确认；若盘点期间发生销售或其他库存变化，仍需取消旧单并重新盘点。

升级盘点功能时需要重启后台，并刷新所有终端页面。启动会为现有 `inventorycount` 表补上 `version` 字段，不删除历史数据。旧版客户端缺少版本或整单明细时会被拒绝。盘点完成接口改为同时提交 `expected_version` 和完整 `items`（`item_id`、`actual_qty`）；单行录入及取消接口同样需要 `expected_version`。

## 功能检查

在 `backend` 目录运行 `python -m unittest discover -p "test_*.py" -v`，使用独立测试数据库检查业务行为。在项目根目录运行 `node --test backend/test_transfer_receipts_frontend.cjs backend/test_checkout_frontend.cjs backend/test_inventory_counts_frontend.cjs`，模拟网络错误、刷新恢复、延迟扫码、切换账号及盘点冲突。这些检查不包含并发压力测试；SQLite 回归和 PostgreSQL 锁语句编译检查不能替代真实 PostgreSQL 多连接验证。

## 目录

```text
backend/
  main.py             应用入口和安全响应头
  models.py           商品、库存、订单、退款、账号和审计模型
  security.py         密码、Cookie 会话、CSRF 和登录限制
  database.py         PostgreSQL 连接与兼容升级
  routers/            业务接口
  static/             本地 Vue 页面及依赖
  test_workflows.py   核心业务回归测试
```

## 备份

在 PostgreSQL 工具可用时运行根目录的 `backup.bat`。正式服务器还应配置定时、加密、异地备份和定期恢复演练。
