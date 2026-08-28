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
