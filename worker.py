#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Author: Binux<i@binux.me>
#         http://binux.me
# Created on 2014-08-09 14:43:13

import time
import datetime
import asyncio
import functools
import tornado.log
import tornado.ioloop
from tornado import gen
import json
import config
from db import DB
from libs.fetcher import Fetcher
from libs.parse_url import parse_url

from libs.funcs import pusher
from libs.funcs import cal
import traceback
from libs.log import Log

logger_Worker = Log('qiandao.Worker').getlogger()

class MainWorker(object):
    def __init__(self, db=DB()):
        self.running = False
        self.db = db
        self.fetcher = Fetcher()

    def __call__(self):
        # self.running = tornado.ioloop.IOLoop.current().spawn_callback(self.run)
        # if self.running:
        #     success, failed = self.running
        #     if success or failed:
        #         logger_Worker.info('%d task done. %d success, %d failed' % (success+failed, success, failed))
        if self.running:
            return
        self.running = gen.convert_yielded(self.run())
        def done(future):
            self.running = None
            success, failed = future.result()
            if success or failed:
                logger_Worker.info('%d task done. %d success, %d failed' % (success+failed, success, failed))
            return
        self.running.add_done_callback(done)
        

    async def ClearLog(self, taskid, sql_session=None):
        logDay = int((await self.db.site.get(1, fields=('logDay',), sql_session=sql_session))['logDay'])
        for log in await self.db.tasklog.list(taskid = taskid, fields=('id', 'ctime'), sql_session=sql_session):
            if (time.time() - log['ctime']) > (logDay * 24 * 60 * 60):
                await self.db.tasklog.delete(log['id'], sql_session=sql_session)

    async def push_batch(self):
        try:
            async with self.db.transaction() as sql_session:
                userlist = await self.db.user.list(fields=('id', 'email', 'status', 'push_batch'), sql_session=sql_session)
                pushtool = pusher(self.db, sql_session=sql_session)
                if userlist:
                    for user in userlist:
                        userid = user['id']
                        push_batch = json.loads(user['push_batch'])
                        if user['status'] == "Enable" and push_batch["sw"] and isinstance(push_batch['time'],(float,int)) and time.time() >= push_batch['time']:
                            logger_Worker.debug('Exist push_batch task, waiting...')
                            title = u"定期签到日志推送"
                            delta = push_batch.get("delta", 86400)
                            logtemp = "{}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(push_batch['time'])))
                            tmpdict = {}
                            tmp = ""
                            numlog = 0
                            task_list = await self.db.task.list(userid=userid, fields=('id', 'tplid', 'note', 'disabled', 'last_success', 'last_failed', 'pushsw'), sql_session=sql_session)
                            for task in task_list:
                                pushsw = json.loads(task['pushsw'])
                                if pushsw["pushen"] and (task["disabled"] == 0 or (task.get("last_success", 0) and task.get("last_success", 0) >= push_batch['time']-delta) or (task.get("last_failed", 0) and task.get("last_failed", 0) >= push_batch['time']-delta)):
                                    tmp0 = ""
                                    tasklog_list = await self.db.tasklog.list(taskid=task["id"], fields=('success', 'ctime', 'msg'), sql_session=sql_session)
                                    for log in tasklog_list:
                                        if (push_batch['time'] - delta) < log['ctime'] <= push_batch['time']:
                                            tmp0 += "\\r\\n时间: {}\\r\\n日志: {}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(log['ctime'])), log['msg'])
                                            numlog += 1
                                    tmplist = tmpdict.get(task['tplid'],[])
                                    if tmp0:
                                        tmplist.append("\\r\\n-----任务{0}-{1}-----{2}\\r\\n".format(len(tmplist)+1, task['note'], tmp0))
                                    else:
                                        tmplist.append("\\r\\n-----任务{0}-{1}-----\\r\\n记录期间未执行签到，请检查任务! \\r\\n".format(len(tmplist)+1, task['note']))
                                    tmpdict[task['tplid']] = tmplist
                                    
                            for tmpkey in tmpdict:
                                tmp = "\\r\\n\\r\\n=====签到: {0}=====".format((await self.db.tpl.get(tmpkey, fields=('sitename',), sql_session=sql_session))['sitename'])
                                tmp += ''.join(tmpdict[tmpkey])
                                logtemp += tmp
                            push_batch["time"] = push_batch['time'] + delta
                            await self.db.user.mod(userid, push_batch=json.dumps(push_batch), sql_session=sql_session)
                            if tmp and numlog:
                                user_email = user.get('email','Unkown')
                                logger_Worker.debug("Start push batch log for {}".format(user_email))
                                await pushtool.pusher(userid, {"pushen": bool(push_batch.get("sw",False))}, 4080, title, logtemp)
                                logger_Worker.info("Success push batch log for {}".format(user_email))
        except Exception as e:
            if config.traceback_print:
                traceback.print_exc()
            logger_Worker.error('Push batch task failed: {}'.format(str(e)))
      
    async def run(self):
        running = []
        success = 0
        failed = 0
        try:
            tasks = await self.scan()
            if tasks is not None and len(tasks) > 0:
                for task in tasks:
                    running.append(asyncio.ensure_future(self.do(task)))
                    if len(running) >= 50:
                        logger_Worker.debug('scaned %d task, waiting...', len(running))
                        result = await asyncio.gather(*running[:10])
                        for each in result:
                            if each:
                                success += 1
                            else:
                                failed += 1
                        running = running[10:]
                logger_Worker.debug('scaned %d task, waiting...', len(running))
                result = await asyncio.gather(*running)
                for each in result:
                    if each:
                        success += 1
                    else:
                        failed += 1
            if config.push_batch_sw:
                await self.push_batch()
        except Exception as e:
            logger_Worker.exception(e)
        return (success, failed)

    async def scan(self):
        return await self.db.task.scan()

    @staticmethod
    def failed_count_to_time(last_failed_count, retry_count=8, retry_interval=None, interval=None):
        next = None
        if last_failed_count < retry_count or retry_count == -1:
            if retry_interval:
                next = retry_interval
            else:
                if last_failed_count == 0:
                    next = 10 * 60
                elif last_failed_count == 1:
                    next = 110 * 60
                elif last_failed_count == 2:
                    next = 4 * 60 * 60
                elif last_failed_count == 3:
                    next = 6 * 60 * 60
                elif last_failed_count < retry_count or retry_count == -1:
                    next = 11 * 60 * 60
                else:
                    next = None
        elif retry_count == 0:
            next = None

        if interval is None:
            interval = 24 * 60 * 60
        if next:
            next = min(next, interval / 2)
        return next

    @staticmethod
    def fix_next_time(next, gmt_offset=-8*60):
        date = datetime.datetime.utcfromtimestamp(next)
        local_date = date - datetime.timedelta(minutes=gmt_offset)
        if local_date.hour < 2:
            next += 2 * 60 * 60
        if local_date.hour > 21:
            next -= 3 * 60 * 60
        return next

    @staticmethod
    def is_tommorrow(next, gmt_offset=-8*60):
        date = datetime.datetime.utcfromtimestamp(next)
        now = datetime.datetime.utcnow()
        local_date = date - datetime.timedelta(minutes=gmt_offset)
        local_now = now - datetime.timedelta(minutes=gmt_offset)
        local_tomorrow = local_now + datetime.timedelta(hours=24)

        if local_date.day == local_tomorrow.day and not now.hour > 22:
            return True
        elif local_date.hour > 22:
            return True
        else:
            return False

    async def do(self, task):
        async with self.db.transaction() as sql_session:
            task['note'] = (await self.db.task.get(task['id'], fields=('note',), sql_session=sql_session))['note']
            user = await self.db.user.get(task['userid'], fields=('id', 'email', 'email_verified', 'nickname', 'logtime'), sql_session=sql_session)
            tpl = await self.db.tpl.get(task['tplid'], fields=('id', 'userid', 'sitename', 'siteurl', 'tpl', 'interval', 'last_success'), sql_session=sql_session)
            ontime = await self.db.task.get(task['id'], fields=('ontime', 'ontimeflg', 'pushsw', 'newontime', 'next'), sql_session=sql_session)
            newontime = json.loads(ontime["newontime"])
            pushtool = pusher(self.db, sql_session=sql_session)
            caltool = cal()
            logtime = json.loads(user['logtime'])
            pushsw = json.loads(ontime['pushsw'])

            if 'ErrTolerateCnt' not in logtime:logtime['ErrTolerateCnt'] = 0 

            if task['disabled']:
                await self.db.tasklog.add(task['id'], False, msg='task disabled.', sql_session=sql_session)
                await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                return False

            if not user:
                await self.db.tasklog.add(task['id'], False, msg='no such user, disabled.', sql_session=sql_session)
                await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                return False

            if not tpl:
                await self.db.tasklog.add(task['id'], False, msg='tpl missing, task disabled.', sql_session=sql_session)
                await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                return False

            if tpl['userid'] and tpl['userid'] != user['id']:
                await self.db.tasklog.add(task['id'], False, msg='no permission error, task disabled.', sql_session=sql_session)
                await self.db.task.mod(task['id'], next=None, disabled=1, sql_session=sql_session)
                return False

            start = time.time()
            try:
                fetch_tpl = await self.db.user.decrypt(0 if not tpl['userid'] else task['userid'], tpl['tpl'], sql_session=sql_session)
                env = dict(
                        variables = await self.db.user.decrypt(task['userid'], task['init_env'], sql_session=sql_session),
                        session = [],
                        )

                url = parse_url(env['variables'].get('_proxy'))
                if not url:
                    new_env = await self.fetcher.do_fetch(fetch_tpl, env)
                else:
                    proxy = {
                        'scheme': url['scheme'],
                        'host': url['host'],
                        'port': url['port'],
                        'username': url['username'],
                        'password': url['password']
                    }
                    new_env = await self.fetcher.do_fetch(fetch_tpl, env, [proxy])

                variables = await self.db.user.encrypt(task['userid'], new_env['variables'], sql_session=sql_session)
                session = await self.db.user.encrypt(task['userid'],
                        new_env['session'].to_json() if hasattr(new_env['session'], 'to_json') else new_env['session'], sql_session=sql_session)

                # TODO next not mid night
                if (newontime['sw']):
                    if ('mode' not in newontime):
                        newontime['mode'] = 'ontime'
                    if (newontime['mode'] == 'ontime'):
                        newontime['date'] = (datetime.datetime.now()+datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                    next = caltool.calNextTs(newontime)['ts']
                else:
                    next = time.time() + max((tpl['interval'] if tpl['interval'] else 24 * 60 * 60), 1*60)
                    if tpl['interval'] is None:
                        next = self.fix_next_time(next)

                # success feedback
                await self.db.tasklog.add(task['id'], success=True, msg=new_env['variables'].get('__log__'), sql_session=sql_session)
                await self.db.task.mod(task['id'],
                        last_success=time.time(),
                        last_failed_count=0,
                        success_count=task['success_count']+1,
                        env=variables,
                        session=session,
                        mtime=time.time(),
                        next=next,
                        sql_session=sql_session)
                await self.db.tpl.incr_success(tpl['id'], sql_session=sql_session)

                t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                title = u"签到任务 {0}-{1} 成功".format(tpl['sitename'], task['note'])
                logtemp = new_env['variables'].get('__log__')
                logtemp = u"{0} \\r\\n日志：{1}".format(t, logtemp)
                await pushtool.pusher(user['id'], pushsw, 0x2, title, logtemp)

                logger_Worker.info('taskid:%d tplid:%d successed! %.5fs', task['id'], task['tplid'], time.time()-start)
                # delete log
                await self.ClearLog(task['id'],sql_session=sql_session)
                logger_Worker.info('taskid:%d tplid:%d clear log.', task['id'], task['tplid'])
            except Exception as e:
                # failed feedback
                if config.traceback_print:
                    traceback.print_exc()
                next_time_delta = self.failed_count_to_time(task['last_failed_count'], task['retry_count'], task['retry_interval'], tpl['interval'])
                            
                t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                title = u"签到任务 {0}-{1} 失败".format(tpl['sitename'], task['note'])
                content = u"{0} \\r\\n日志：{1}".format(t, str(e))
                disabled = False
                if next_time_delta:
                    next = time.time() + next_time_delta
                    content = content + u" \\r\\n下次运行时间：{0}".format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(next)))
                    if (logtime['ErrTolerateCnt'] <= task['last_failed_count']):
                        await pushtool.pusher(user['id'], pushsw, 0x1, title, content)
                else:
                    disabled = True
                    next = None
                    content = u" \\r\\n任务已禁用"
                    await pushtool.pusher(user['id'], pushsw, 0x1, title, content)

                await self.db.tasklog.add(task['id'], success=False, msg=str(e), sql_session=sql_session)
                await self.db.task.mod(task['id'],
                        last_failed=time.time(),
                        failed_count=task['failed_count']+1,
                        last_failed_count=task['last_failed_count']+1,
                        disabled = disabled,
                        mtime = time.time(),
                        next=next,
                        sql_session=sql_session)
                await self.db.tpl.incr_failed(tpl['id'], sql_session=sql_session)

                logger_Worker.error('taskid:%d tplid:%d failed! %.4fs \r\n%s', task['id'], task['tplid'], time.time()-start, str(e).replace('\\r\\n','\r\n'))
                return False
        return True

if __name__ == '__main__':
    tornado.log.enable_pretty_logging()
    worker = MainWorker()
    io_loop = tornado.ioloop.IOLoop.instance()
    tornado.ioloop.PeriodicCallback(worker, config.check_task_loop).start()
    worker()
    io_loop.start()
