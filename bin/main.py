from fastapi import FastAPI

# 添加项目路径进入环境变量，防止找不到模块
import sys
import os

sys.path.append(os.path.split(os.path.abspath(os.path.dirname(__file__)))[0])

from xmuorder_server import config
from xmuorder_server.database import Mysql
from xmuorder_server.routers import sms, xmu, statistics, printer, update
from xmuorder_server.logger import Logger
from xmuorder_server.scheduler import Scheduler
from xmuorder_server.weixin.weixin import WeiXin

app = FastAPI()


@app.on_event("startup")
async def __init():
    #   一定要按顺序执行下列初始化

    #   logger初始化
    Logger.init(os.path.abspath(os.path.join(__file__, '../log/日志.log')))

    #   配置文件读取
    config.GlobalSettings.init(_env_file='../.env')

    #   Mysql连接初始化
    Mysql.init()

    #   微信模块初始化
    WeiXin.init()

    #   刷新数据库 路由
    app.include_router(update.router, prefix="/update")
    #   短信相关 路由
    app.include_router(sms.router, prefix="/sms")
    #   xmu绑定 路由
    app.include_router(xmu.router, prefix="/xmu")
    #   统计模块 路由
    app.include_router(statistics.router, prefix="/statistics")
    #   云打印机模块 路由
    app.include_router(printer.router, prefix="/printer")

    #   scheduler初始化, router模块需要的任务在模块__init中添加
    Scheduler.init()


@app.get('/')
async def hello_world():
    return 'hello world'


if __name__ == "__main__":
    import uvicorn

    # noinspection PyTypeChecker
    uvicorn.run(app, host='127.0.0.1', port=5716)
