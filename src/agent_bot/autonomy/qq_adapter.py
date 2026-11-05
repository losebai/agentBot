"""已有QQ身份的可选出站适配器，独立本地循环不导入或调用本模块。"""

import hashlib


def enqueue_report(store, connection, profile, task, report_id: str, text: str) -> None:
    """仅将获批的旧渠道报告写入出站队列，平台发送由渠道插件负责。"""
    id, task_id = profile['id'], task['id']
    from agent_bot.jobs.contracts import JobOwner

    owner = JobOwner.model_validate(profile["document"]["owner"])
    values = {
        "id": report_id,
        "owner_key": owner.owner_key,
        "deduplication_key": hashlib.sha256(
            ("report:" + report_id).encode()
        ).hexdigest(),
        **owner.model_dump(),
        "model": f"autonomy:{id}:{task_id}",
        "system_prompt": "",
        "prompt": task["document"]["goal"],
        "status": "SUCCEEDED",
        "result": text,
        "error_code": None,
        "run_at": store.now(),
        "created_at": store.now(),
        "started_at": store.now(),
        "completed_at": store.now(),
        "delivered_at": None,
    }
    with connection.cursor() as cursor:
        if store.params.backend == "sqlite":
            cursor.execute(
                "INSERT INTO agent_bot_job_owner (owner_key) VALUES (%s) "
                "ON CONFLICT(owner_key) DO NOTHING",
                (owner.owner_key,),
            )
        else:
            cursor.execute(
                "INSERT INTO agent_bot_job_owner (owner_key) VALUES (%s) "
                "ON DUPLICATE KEY UPDATE owner_key=VALUES(owner_key)",
                (owner.owner_key,),
            )
        cursor.execute(
            "INSERT INTO agent_bot_job ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join("%s" for _ in values)
            + ")",
            tuple(values.values()),
        )
