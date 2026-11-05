-- 为既有模型供应商增加自定义 User-Agent。
-- 升级起点：0.6.2 及更早、尚未执行 provider-user-agent-v1 的数据库。
-- 执行顺序：由启动迁移清单在供应商表初始化后执行。
-- 数据影响：既有供应商写入空默认值，继续沿用 HTTP 客户端默认 User-Agent。

ALTER TABLE agent_bot_ai_provider
  ADD COLUMN user_agent TEXT NOT NULL DEFAULT '';
