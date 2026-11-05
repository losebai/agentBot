"""临时诊断：重查该请求的两条记录 + 模型渠道配置。"""

import json

import pymysql

RID = "qq-71af35ec32e542c5b5bfa9e7bb58fa82"

conn = pymysql.connect(
    host="192.168.1.5", port=3306, user="agentbot", password="agentbot",
    database="agentbot", charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
)
try:
    cur = conn.cursor()

    cur.execute(
        "SELECT id, session_Id, request_id, type, source, status, model, "
        "duration, create_time, CHAR_LENGTH(content) AS content_len "
        "FROM agent_bot_call_record WHERE request_id=%s ORDER BY id",
        (RID,),
    )
    rows = cur.fetchall()
    print("== call records for request:", RID)
    for r in rows:
        print(r)

    # 找模型/渠道相关配置表
    cur.execute("SHOW TABLES")
    tables = [list(t.values())[0] for t in cur.fetchall()]
    cand = [t for t in tables if any(k in t.lower() for k in ("model", "channel", "provider", "llm", "ai_config"))]
    print("\n== candidate config tables:", cand)

    for t in cand:
        cur.execute(f"SELECT * FROM {t} LIMIT 30")
        data = cur.fetchall()
        print(f"\n== {t} ({len(data)} rows shown)")
        for d in data:
            print(json.dumps(d, ensure_ascii=False, default=str)[:500])

    # 如果存在 chat/agent 配置表也看下 deepseek-flash 的配置来源
    for t in tables:
        if t.lower() in ("agent_bot_config", "agent_bot_setting", "agent_bot_app_config"):
            cur.execute(f"SELECT * FROM {t}")
            print(f"\n== {t}")
            for d in cur.fetchall():
                print(json.dumps(d, ensure_ascii=False, default=str)[:500])
finally:
    conn.close()
