"""
短信服务相关
"""
import random
import re
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from tencentcloud.common import credential
from tencentcloud.sms.v20210111 import sms_client, models

from .. import dependencies
from ..common import SuccessInfo, XMUORDERException
from ..config import GlobalSettings
from ..database import Mysql
from ..logger import Logger
from ..scheduler import Scheduler, Task

router = APIRouter()
logger: Logger


class SendSmsModel(BaseModel):
    """
    发送信息模板
    """
    cID_list: list[str]
    time1: Optional[str] = 3
    time2: Optional[str] = 10


class SmsVerificationCodeModel(BaseModel):
    """
    发送验证码模板
    """
    phone: str


class BindCanteenSmsModel(BaseModel):
    """
    绑定餐厅通知手机号模板
    """
    cID: str
    cName: str
    phone: str
    sms_code: str


class GetCanteenBindPhoneModel(BaseModel):
    """
    获取餐厅绑定通知手机号模板
    """
    cID: str


class RemoveCanteenBindPhoneModel(BaseModel):
    """
    移除餐厅绑定的某个手机号
    """
    cID: str
    phone: str


@router.on_event("startup")
async def __init():
    #   获取默认日志
    global logger
    logger = Logger('短信模块')
    #   添加任务
    Scheduler.add(Task.clear_phone_verification_task, job_name='清空验证码数据',
                  trigger='cron', hour="2", minute="0", second='0')


@router.post("/sendCanteenNotice")
async def send_canteen_notice(data: SendSmsModel, verify=Depends(dependencies.code_verify_aes_depend)):
    conn = Mysql.connect()
    try:
        cid_list = [f"'{x}'" for x in data.cID_list]
        for x in cid_list:
            if x.find(' ') > -1:
                raise XMUORDERException("cID列表异常")

        #   过滤出需要发送的电话号码
        #   1. cID符合    2. 距离上次发送订单提醒超过30min
        sql = f'''
        select c.cID, p.phone, c.lastSendMsgTime
        from canteen c
                 left join phone p on c.cID = p.cID
        where c.cID in {f"({','.join(cid_list)})"}
            and TIMESTAMPDIFF(minute, c.lastSendMsgTime, NOW()) > 30;
        '''

        res = Mysql.execute_fetchall(conn, sql=sql)
        phone_list = set([line[1] for line in res if line[1] is not None])
        if len(phone_list) == 0:
            raise XMUORDERException('匹配的phone列表为空')

        #   更新 lastSendMsgTime
        sql = f'''
        update canteen set lastSendMsgTime = NOW()
        where cID in {f"({','.join(cid_list)})"};
        '''
        Mysql.execute_only(conn, sql)

        #   发送短信
        res = send_message(list(phone_list), time1=data.time1, time2=data.time2)
        return SuccessInfo(msg='Sms request success',
                           data={'SendStatusSet': res.SendStatusSet}).to_dict()

    except Exception as e:
        logger.debug(f'发送商家通知短信失败-{e}')
        raise HTTPException(status_code=400, detail="订单通知短信发送失败")
    finally:
        conn.close()


@router.post("/phoneVerificationCode")
async def phone_verification_code(data: SmsVerificationCodeModel, verify=Depends(dependencies.code_verify_aes_depend)):
    """
    发送验证码
    """
    conn = Mysql.connect()
    try:
        # 再次简单核验电话号码，防止注入等问题
        if re.match(r'^\+86[1][34578][0-9]{9}$', data.phone) is None:
            raise XMUORDERException(f'phone:{data.phone}不是正确的手机号码')

        # 验证码
        code = str(random.randint(100000, 999999))

        sql = '''
        select phone, code, expiration, lastSendTime, sendTimes
        from phone_verification where phone=%(phone)s;
        '''
        res = Mysql.execute_fetchone(conn, sql, phone=data.phone)
        if res is None:
            sql = '''
            insert into phone_verification (phone, code, expiration, lastSendTime, sendTimes)
            VALUES (%(phone)s, %(code)s, DATE_ADD(now(), interval 5 minute), now(), 0)
            '''
        else:
            if res[4] >= 5:
                raise XMUORDERException('此号码已达到今日发送验证码次数上限')

            # 2min内发送过验证码则退出
            sec = (datetime.now() - res[3]).seconds
            if sec < 2 * 60:
                raise XMUORDERException('此号码短信发送过于频繁，请稍后再试')

            # 5min后验证码过期
            sql = '''
            UPDATE phone_verification
                set phone=%(phone)s, code=%(code)s, sendTimes=sendTimes+1,
                expiration=DATE_ADD(now(), interval 5 minute),
                lastSendTime=NOW()
            where
                phone=%(phone)s;
            '''

        Mysql.execute_only(conn, sql, phone=data.phone, code=code)

        # 发送验证码短信
        res = send_verification_code(data.phone, code)
        conn.commit()

        # return SuccessInfo(msg='Verification code request success',
        #                    data={'SendStatusSet': res}).to_dict()
        logger.debug(f'验证码发送成功-phone:{data.phone}')
        return SuccessInfo(msg='Verification code request success',
                           data={'SendStatusSet': res.SendStatusSet}).to_dict()

    except XMUORDERException as e:
        logger.debug(f'发送验证码短信失败-phone:{data.phone}\t{e}')
        raise HTTPException(status_code=400, detail=e.msg)
    except Exception as e:
        logger.debug(f'发送验证码短信失败-phone:{data.phone}\t{e}')
        raise HTTPException(status_code=400, detail="发送短信验证码失败")
    finally:
        conn.close()


@router.post("/removeCanteenBindPhone")
async def remove_canteen_bind_phone_list(data: RemoveCanteenBindPhoneModel,
                                         verify=Depends(dependencies.code_verify_aes_depend)):
    """
    移除餐厅绑定的某个手机号
    """
    conn = Mysql.connect()
    try:
        sql = '''
        delete from phone where cID=%(cID)s and phone=%(phone)s;
        '''
        Mysql.execute_only(conn, sql, cID=data.cID, phone=data.phone)
        logger.success(f'移除餐厅绑定的手机号成功\t phone-{data.phone} cID-{data.cID}')
        return SuccessInfo(msg='remove phone from canteen success')

    except Exception as e:
        logger.debug(f'移除餐厅绑定的手机号失败\t phone-{data.phone} cID-{data.cID}\t{e}')
        raise HTTPException(status_code=400, detail="remove phone from canteen failed")
    finally:
        conn.close()


@router.post("/getCanteenBindPhone")
async def get_canteen_bind_phone_list(data: GetCanteenBindPhoneModel,
                                      verify=Depends(dependencies.code_verify_aes_depend)):
    """
    获取餐厅绑定的手机号
    """
    conn = Mysql.connect()
    try:
        sql = '''
        select phone from phone where cID=%(cID)s;
        '''
        res = Mysql.execute_fetchall(conn, sql, cID=data.cID)
        return SuccessInfo(msg='get phone list success',
                           data={'phone': (x[0] for x in res)}).to_dict()

    except Exception as e:
        logger.debug(f'获取餐厅绑定的手机号失败-cID={data.cID}\t{e}')
        raise HTTPException(status_code=400, detail="get phones of canteen failed")
    finally:
        conn.close()


@router.post("/bindCanteen")
async def bind_canteen_sms(data: BindCanteenSmsModel, verify=Depends(dependencies.code_verify_aes_depend)):
    conn = Mysql.connect()
    try:
        # 再次简单核验电话号码，防止注入等问题
        if re.match(r'^\+86[1][34578][0-9]{9}$', data.phone) is None:
            raise XMUORDERException(['此号码不是正确的手机号码', data.phone])

        sql = 'select phone from phone where cID=%(cID)s;'
        res = Mysql.execute_fetchall(conn, sql, cID=data.cID)
        if len(res) >= 3:
            raise XMUORDERException(['餐厅可绑定号码数已达上限', data.cID])
        for x in res:
            if x[0] == data.phone:
                raise XMUORDERException(['此号码已绑定', data.phone])

        sql = '''
        select phone, code, expiration from phone_verification
        where phone=%(phone)s; 
        '''
        res: tuple[str, str, datetime] = Mysql.execute_fetchone(conn, sql, phone=data.phone)
        # 无号码记录
        if res is None:
            raise XMUORDERException(['此号码未发送验证码', data.phone])

        # 验证码过期
        if datetime.now() > res[2]:
            raise XMUORDERException(['验证码已过期', data.phone])

        # 验证码错误
        if res[1] != data.sms_code:
            raise XMUORDERException(['验证码错误', data.phone])

        sql1 = '''
        # 更新、或添加此号码所在餐厅
        insert into canteen (cID, name)
            VALUES (%(cID)s, %(name)s)
        ON DUPLICATE KEY UPDATE
            name=%(name)s;
        '''
        sql2 = '''# 插入phone表
        insert into phone (cID, phone)
        values (%(cID)s, %(phone)s)
        '''

        Mysql.execute_only(conn, sql1, cID=data.cID, name=data.cName)
        Mysql.execute_only(conn, sql2, cID=data.cID, phone=data.phone)
        conn.commit()

        logger.success(f'绑定餐厅短信通知成功-phone:{data.phone}')
        return SuccessInfo(msg='Bind sms notification success',
                           data={'phone': data.phone}).to_dict()

    except XMUORDERException as e:
        logger.debug(f'餐厅绑定手机号失败 {e.msg}')
        raise HTTPException(status_code=400, detail=e.msg[0])
    except Exception as e:
        logger.debug(f'餐厅绑定手机号失败 {e}')
        raise HTTPException(status_code=400, detail="餐厅绑定手机号失败")
    finally:
        conn.close()


def send_tencent_sms(appid: str, sign_name: str, template_id: str,
                     template_params: list[str], phone_list: list[str]):
    """
    腾讯云发送短信
    :param appid: 短信应用ID
    :param sign_name: 短信签名内容
    :param template_id: 模板 ID
    :param template_params: 模板参数
    :param phone_list: 接收号码列表
    :return: SendSmsResponse
    """
    settings = GlobalSettings.get()
    # 实例化一个认证对象，入参需要传入腾讯云账户密钥对secretId，secretKey。
    cred = credential.Credential(settings.secret_id, settings.secret_key)

    # 实例化要请求产品(以sms为例)的client对象 第二个参数为地域
    client = sms_client.SmsClient(cred, "ap-guangzhou")

    # 实例化一个请求对象，根据调用的接口和实际情况，可以进一步设置请求参数
    req = models.SendSmsRequest()

    # 短信应用ID: 短信SdkAppId在 [短信控制台] 添加应用后生成的实际SdkAppId，示例如1400006666
    req.SmsSdkAppId = appid
    # 短信签名内容: 使用 UTF-8 编码，必须填写已审核通过的签名，签名信息可登录 [短信控制台] 查看
    req.SignName = sign_name

    # 模板 ID: 必须填写已审核通过的模板 ID。模板ID可登录 [短信控制台] 查看
    req.TemplateId = template_id
    # 模板参数: 若无模板参数，则设置为空
    req.TemplateParamSet = template_params
    req.PhoneNumberSet = phone_list

    return client.SendSms(req)


def send_message(phone_list: List[str], time1: str, time2: str) -> models.SendSmsResponse:
    """
    批量发送商家接单提醒
    """
    return send_tencent_sms(
        appid='1400647289',
        sign_name='XMU智能点餐',
        template_id='1334610',
        template_params=[str(time1), str(time2)],
        phone_list=phone_list
    )


def send_verification_code(phone: str, code: str, timeout: int = 5):
    """
    发送短信验证码
    """
    return send_tencent_sms(
        appid='1400647289',
        sign_name='XMU智能点餐',
        template_id='1344135',
        template_params=[str(code), str(timeout)],
        phone_list=[str(phone)]
    )
