"""复用所选数据库保存账号和外部写操作，不执行启动迁移。"""

import json
import time

from pydantic import SecretStr

from agent_bot.agent.errors import AgentError
from agent_bot.ai.database import AiDatabase, query
from agent_bot.ai.errors import AiError
from agent_bot.plugins.bilibili.contracts import Account


def _operation_result(row: dict | None) -> dict | None:
    """既有操作结果与媒体有效期无关，pending 不允许重新触发外部写入。"""
    if row is None:
        return None
    if row['status'] == 'completed':
        return json.loads(row['result'])
    raise AgentError('BILIBILI_OPERATION_PENDING', status_code=409)


class BilibiliStore(AiDatabase):
    """事务只覆盖本地状态，禁止在数据库锁中等待平台网络。"""

    async def account(self) -> Account | None:
        """读取内部凭据；缺少迁移表时明确报告，不创建空表。"""
        row = await self.run(lambda c: query(
            c, 'SELECT uid,name,cookies,updated_at FROM agent_bot_bilibili_account WHERE id=1',
            one=True))
        return Account(uid=row['uid'], name=row['name'], cookies=SecretStr(row['cookies'])) \
            if row else None

    async def validate(self) -> None:
        """启用插件时检查两张专用表，禁用插件不触碰新表。"""
        try:
            await self.account()
            await self.run(lambda c: query(
                c, 'SELECT operation_key,uid,tool,status,result,created_at '
                   'FROM agent_bot_bilibili_operation LIMIT 1'))
        except AiError as error:
            if error.code == 'AI_NOT_INITIALIZED':
                raise AgentError('BILIBILI_NOT_INITIALIZED', status_code=503) from None
            raise

    async def save_account(self, account: Account) -> None:
        """已验证的新账号在一个事务替换，失败保留旧凭据。"""
        def save(c):
            """锁定已有单例，首次登录才插入。"""
            old = query(c, 'SELECT id FROM agent_bot_bilibili_account WHERE id=1 FOR UPDATE',
                        one=True)
            values = (account.uid, account.name, account.cookies.get_secret_value(),
                      int(time.time()))
            if old:
                query(c, 'UPDATE agent_bot_bilibili_account SET uid=%s,name=%s,cookies=%s,'
                         'updated_at=%s WHERE id=1', values)
            else:
                query(c, 'INSERT INTO agent_bot_bilibili_account '
                         '(uid,name,cookies,updated_at,id) VALUES (%s,%s,%s,%s,1)', values)
        await self.run(save, write=True)

    async def clear_account(self) -> None:
        """只删除本地授权，不删除历史操作记录或声称远端设备已退出。"""
        await self.run(lambda c: query(c, 'DELETE FROM agent_bot_bilibili_account WHERE id=1'),
                       write=True)

    async def cached_result(self, key: str) -> dict | None:
        """先读取已绑定账号、会话与参数的记录，不能因原图片到期而丢失防重结果。"""
        row = await self.run(lambda c: query(
            c, 'SELECT status,result FROM agent_bot_bilibili_operation WHERE operation_key=%s',
            (key,), one=True,
        ))
        return _operation_result(row)

    async def begin(self, key: str, uid: str, tool: str) -> dict | None:
        """唯一键仲裁并发请求；未完成记录绝不能自动再次发送。"""
        def begin(c):
            """成功结果可重复读取，pending 同时覆盖崩溃后的未知结果。"""
            row = query(c, 'SELECT status,result FROM agent_bot_bilibili_operation '
                           'WHERE operation_key=%s FOR UPDATE', (key,), one=True)
            if row:
                return _operation_result(row)
            query(c, 'INSERT INTO agent_bot_bilibili_operation '
                     '(operation_key,uid,tool,status,result,created_at) '
                     'VALUES (%s,%s,%s,%s,%s,%s)',
                  (key, uid, tool, 'pending', None, int(time.time())))
            return None
        try:
            return await self.run(begin, write=True)
        except AiError as error:
            if error.code == 'AI_CONFIG_CONFLICT':
                raise AgentError('BILIBILI_OPERATION_PENDING', status_code=409) from None
            raise

    async def complete(self, key: str, result: dict) -> None:
        """只保存已投影的安全结果，远端成功后本地失败保持 pending。"""
        await self.run(lambda c: query(
            c, 'UPDATE agent_bot_bilibili_operation SET status=%s,result=%s '
               'WHERE operation_key=%s',
            ('completed', json.dumps(result, ensure_ascii=False), key)), write=True)
