# 架构说明

## 库存模型

四家门店采用独立库存：

```text
Product (款式)
  -> Sku (颜色 x 尺码、条码、售价)
     -> Inventory (sku_id + store + quantity + safety_stock)
```

`Inventory` 具有 `(sku_id, store)` 唯一约束和非负库存约束。销售与出库使用带库存条件的原子更新，因此多收银台同时销售同一 SKU 时不会扣成负数。

## 业务单据

- `SalesOrder / SalesOrderItem`：销售快照及防重复请求号
- `SalesRefund / SalesRefundItem`：每次退款的独立、不可混淆记录
- `StockTransferBatch / StockTransferItem`：多 SKU 跨店调拨与部分收货
- `InventoryCount / InventoryCountItem`：盘点快照和并发变更检测
- `StockLog`：所有库存数量变化
- `AuditEvent`：账号、商品、库存、销售和退款等关键操作

所有库存变化与对应流水在同一数据库事务中提交。销售、退款、入出库和调拨创建使用客户端请求编号防止双击或网络重试造成重复执行。

## 身份验证

浏览器只保存 `HttpOnly` 会话 Cookie；数据库保存令牌 SHA-256 摘要，不保存原始令牌。修改密码或停用账号会撤销该账号全部会话。写操作还需要与当前会话绑定的 CSRF 令牌。

## 部署边界

应用代码负责业务一致性、会话、权限和审计。服务器负责 HTTPS、VPN、防火墙、进程守护、数据库专用账号、监控和异地备份。正式部署设置：

```text
COOKIE_SECURE=true
BUSINESS_TIMEZONE=Asia/Shanghai
TRUST_PROXY_HEADERS=true   # 仅在受信任反向代理后启用
```
