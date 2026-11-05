-- 用途：为统一调用归档建立可索引分类，消除每次计数时的 JSON 全表解析。
-- 顺序：完整旧业务库完成既有增量后，由启动清单执行；新库已包含相同定义。
-- 影响：只新增虚拟生成列和索引，建索引扫描旧记录，不改写正文、JSON、类型或密钥。
-- 历史决策与指令标记沿用旧查询规则，决策优先；NULL 代表普通对话。
-- 两条 DDL 不可直接重复执行；启动升级器按列和索引状态恢复 MySQL 中断。
ALTER TABLE agent_bot_call_record ADD COLUMN system_kind VARCHAR(16) COLLATE utf8mb4_bin
GENERATED ALWAYS AS (
  CASE WHEN (
    type = 'auto_speak'
    OR source = 'python:agent_bot_auto_speak_decision'
    OR SUBSTR(request_id, 1, 14) = 'auto-decision-'
    OR JSON_EXTRACT(data, '$.type') IN ('auto_speak_decision', '"auto_speak_decision"')
    OR JSON_EXTRACT(data, '$.system_kind') IN ('auto_speak', '"auto_speak"')
    OR (JSON_EXTRACT(request_context, '$.state') IS NOT NULL
        AND JSON_EXTRACT(data, '$.shouldSpeak') IS NOT NULL)
  ) THEN 'auto_speak' WHEN (
    COALESCE(JSON_EXTRACT(data, '$.system_kind') IN ('command', '"command"'), 0)
    OR JSON_EXTRACT(data, '$.original_data.system_kind') IN ('command', '"command"')
    OR SUBSTR(request_id, 1, 11) = 'qq-summary-'
    OR SUBSTR(request_id, 1, 8) = 'qq-draw-'
    OR SUBSTR(session_Id, -15) = ':quoted-summary'
    OR (SUBSTR(request_id, 1, 9) = 'qq-reply-' AND SUBSTR(content, 1, 1) = '/')
  ) THEN 'command' ELSE NULL END
) VIRTUAL COMMENT '系统分类：auto_speak、command；NULL 为普通对话，兼容历史标记';
-- 分类前缀用于精确计数，时间和主键用于分类后的稳定分页。
CREATE INDEX idx_call_system_time ON agent_bot_call_record(system_kind, create_time, id)
COMMENT '按系统分类计数和时间稳定分页，避免逐条解析归档 JSON';
