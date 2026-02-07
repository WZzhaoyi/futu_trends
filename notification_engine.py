#  Futu Trends
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  Written by Joey <wzzhaoyi@outlook.com>, 2025
#  Copyright (c)  Joey - All Rights Reserved

import configparser
from datetime import datetime,date
import re
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from socket import gaierror
import time
import os
import json
from requests_html import HTMLSession
from futu import OpenQuoteContext, RET_OK, SetPriceReminderOp, PriceReminderType, PriceReminderFreq, ModifyUserSecurityOp
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import httplib2
from google_auth_httplib2 import AuthorizedHttp


class NotificationEngine:
    def plog(self,content):
        print('{} {}'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time())), content))

    def __init__(self, config:configparser.ConfigParser):
        """
            Notification Engine Constructor
        """
        # Email configuration
        self.mail_port = config.getint("CONFIG", "EMAIL_PORT")
        self.mail_host = config.get("CONFIG", "EMAIL_SERVER")
        self.sender = config.get("CONFIG", "EMAIL_SENDER")
        self.mail_pass = config.get("CONFIG", "EMAIL_PASWD")
        # 从配置中读取邮件订阅者列表，如果配置为空则设为空列表
        email_subscription = config.get("CONFIG", "EMAIL_SUBSCRIBTION", fallback="")
        self.receivers = [email.strip() for email in email_subscription.split(',') if email.strip()]

        # Telegram configuration
        self.TELEGRAM_BOT_TOKEN = config.get("CONFIG", "TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = config.get("CONFIG", "TELEGRAM_CHAT_ID")
        self.SESSION = HTMLSession()
        self.SESSION.adapters.DEFAULT_RETRIES = 5  # 增加重连次数
        self.SESSION.keep_alive = False  # 关闭多余连接
        proxy = config.get("CONFIG", "PROXY")
        self.PROXIES = {"http": proxy, "https": proxy}

        # Futu API configuration
        self.host = config.get("CONFIG", "FUTU_HOST")
        self.port = int(config.get("CONFIG", "FUTU_PORT"))
        futu_keyword = config.get("CONFIG", "FUTU_KEYWORD", fallback="")
        self.futu_keyword = [keyword.strip() for keyword in futu_keyword.split(',') if keyword.strip()]

        # Google Sheet configuration
        self.google_sheet_id = config.get("CONFIG", "GOOGLE_SHEET_ID", fallback="")
        self.google_api_json = config.get("CONFIG", "GOOGLE_API_JSON", fallback="")
        self.google_sheet_cell_origin = config.get("CONFIG", "GOOGLE_SHEET_CELL_ORIGIN", fallback="B2")

    def send_futu_message(self, codes:list[str], messages:list[str], highs:list[float], lows:list[float]):
        """
        根据关键词存入futu group
        """
        if not self.futu_keyword:
            self.plog('没有futu关键词，跳过存入futu group')
            return

        quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
        
        for keyword in self.futu_keyword:
            for code, msg, recent_high, recent_low in zip(codes, messages, highs, lows): 
                if keyword in msg:
                    ret_del, data_del = quote_ctx.set_price_reminder(code=code, op=SetPriceReminderOp.DEL_ALL)
                    ret_up, data_up = quote_ctx.set_price_reminder(code=code, op=SetPriceReminderOp.ADD, reminder_type=PriceReminderType.PRICE_UP,reminder_freq=PriceReminderFreq.ONCE,value=float(recent_high))
                    ret_down, data_down = quote_ctx.set_price_reminder(code=code, op=SetPriceReminderOp.ADD, reminder_type=PriceReminderType.PRICE_DOWN,reminder_freq=PriceReminderFreq.ONCE,value=float(recent_low))
                    if ret_del == RET_OK and ret_up == RET_OK and ret_down == RET_OK:
                        self.plog(f'{code} 价格提醒 [{recent_low},{recent_high}]')
                    else:
                        self.plog(f'{code} 价格提醒失败 {data_del} {data_up} {data_down}')
                    
                    ret, data = quote_ctx.modify_user_security(keyword, ModifyUserSecurityOp.ADD, [code])
                    if ret == RET_OK:
                        self.plog(f'{code} 存入{keyword}')
                    else:
                        self.plog(f'存入{keyword}失败 {data}')
                    time.sleep(3)
        
        quote_ctx.close()


    def send_email(self, subject: str, message_html: str):
        """
        发送邮件
        """
        # 检查邮件配置是否完整
        if not all([self.mail_port, self.mail_host, self.sender, self.mail_pass]):
            self.plog('邮件配置不完整，跳过发送')
            return
            
        # 检查是否有订阅者
        if not self.receivers:
            self.plog('没有邮件订阅者，跳过发送')
            return

        # 将消息转换为HTML格式
        message_html = message_html.replace('\n', '<br>')
        
        # 添加基本的HTML样式
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
                br {{ margin-bottom: 5px; }}
            </style>
        </head>
        <body>
            {message_html}
        </body>
        </html>
        """

        message = MIMEText(html_content, 'html', 'utf-8')
        message["From"] = self.sender
        message['To'] = ','.join(self.receivers)
        message['Subject'] = f"Trends - {datetime.today().strftime('%Y-%m-%d')} - {subject}"

        try:
            smtpObj = smtplib.SMTP_SSL(self.mail_host, self.mail_port) #建立smtp连接，ssl 465端口
            smtpObj.login(self.sender, self.mail_pass)  #登陆
            smtpObj.sendmail(self.sender, self.receivers, message.as_string())  #发送
            smtpObj.quit()
            self.plog(f'Email Sent: {self.receivers}')
        except (gaierror, ConnectionRefusedError):
            self.plog('Failed to connect to the server. Bad connection settings?')
        except smtplib.SMTPServerDisconnected:
            self.plog('Failed to connect to the server. Wrong user/password?')
        except smtplib.SMTPException as e:
            self.plog('SMTP error occurred: ' + str(e))

    def send_telegram_message(self, text, link='www.google.com'):
        """
        给电报发送文字消息
        """
        # 检查Telegram配置是否完整
        if not all([self.TELEGRAM_BOT_TOKEN, self.TELEGRAM_CHAT_ID]):
            self.plog('Telegram配置不完整，跳过发送')
            return

        headers = {
            'Content-Type': 'application/json',
        }
        data = f'{{"chat_id":"{self.TELEGRAM_CHAT_ID}", "text":"{text}", "reply_markup": {{"inline_keyboard":' \
               f' [[{{"text":"🔗查看原文", "url":"{link}"}}]]}}}} '
        url = f'https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}/sendMessage'
        try:
            self.SESSION.post(url, headers=headers, data=data.encode('utf-8'), proxies=self.PROXIES)
            self.plog(f'Telegram Sent: {self.TELEGRAM_CHAT_ID}')
        except:
            self.plog(f'网络代理错误，请检查确认后关闭本程序重试')
    
    def send_telegram_photo(self, img_url):
        """
        给电报发送图片
        """
        # 检查Telegram配置是否完整
        if not all([self.TELEGRAM_BOT_TOKEN, self.TELEGRAM_CHAT_ID]):
            self.plog('Telegram配置不完整，跳过发送')
            return

        url = f'https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}/sendPhoto'
        data = dict(chat_id=f"{self.TELEGRAM_CHAT_ID}&", photo=img_url)

        self.SESSION.post(url, data=data, proxies=self.PROXIES)
        self.plog(f'Telegram Sent: {self.TELEGRAM_CHAT_ID}')

    def send_telegram_photos(self, pic_urls):
        """
        给电报发送多张图片
        """
        # 检查Telegram配置是否完整
        if not all([self.TELEGRAM_BOT_TOKEN, self.TELEGRAM_CHAT_ID]):
            self.plog('Telegram配置不完整，跳过发送')
            return

        url = f'https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}/sendMediaGroup'
        params = {
            'chat_id': self.TELEGRAM_CHAT_ID,
            'media': [],
        }
        for pic in pic_urls:
            params['media'].append({'type': 'photo', 'media': pic})
        params['media'] = json.dumps(params['media'])
        result = self.SESSION.post(url, data=params, proxies=self.PROXIES)
        if result.status_code != 200: # 如果分组发送失败 则单独发送图片
            for pic in pic_urls:
                self.send_telegram_photo(pic)
    
    def _safe_execute(self, execute_func, *args, **kwargs):
        """安全的执行函数，带重试机制"""
        for attempt in range(3):
            try:
                return execute_func(*args, **kwargs).execute()
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(1)
        return None

    def send_google_sheet_message(self, message):
        """更新Google Sheet"""
        if not all([self.google_sheet_id, os.path.exists(self.google_api_json), self.google_sheet_cell_origin]):
            self.plog('Google Sheet配置不完整，跳过发送')
            return

        try:
            # 获取Google Sheet服务
            SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
            creds = Credentials.from_service_account_file(self.google_api_json, scopes=SCOPES)
            
            # 配置HTTP客户端
            if self.PROXIES.get('http'):
                proxy_url = self.PROXIES['http'].split('://', 1)[1]
                proxy_host, proxy_port = proxy_url.rsplit(':', 1)
                http_client = httplib2.Http(
                    proxy_info=httplib2.ProxyInfo(
                        proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
                        proxy_host=proxy_host,
                        proxy_port=int(proxy_port),
                    ),
                    timeout=30,
                    disable_ssl_certificate_validation=False
                )
            else:
                http_client = httplib2.Http(timeout=30)
            
            http = AuthorizedHttp(creds, http_client)
            service = build('sheets', 'v4', http=http)

            # 解析网格原点和日期
            match = re.match(r'([A-Z]+)(\d+)', self.google_sheet_cell_origin)
            if not match:
                raise ValueError("Invalid grid_origin format, expected like 'B2'")
            
            start_col_letter, start_row = match.groups()
            start_row = int(start_row)
            today = date.today()
            cell_sheet_name = today.strftime('%y-%m')
            
            # 计算目标单元格位置
            start_col = ord(start_col_letter[0]) - ord('A')
            first_day_of_month = datetime(today.year, today.month, 1)
            first_weekday = first_day_of_month.weekday() # （0=周一，6=周日）
            day, weekday = today.day, today.weekday()
            week = (day + first_weekday) // 7
            col_offset = (weekday + 1) % 7
            target_col = chr(ord('A') + start_col + col_offset)
            target_row = start_row + week + 1
            target_cell = f'{cell_sheet_name}!{target_col}{target_row}'

            # 确保工作表存在
            sheet_metadata = self._safe_execute(service.spreadsheets().get, spreadsheetId=self.google_sheet_id)
            if not any(sheet['properties']['title'] == cell_sheet_name for sheet in sheet_metadata.get('sheets', [])):
                # 创建新工作表并初始化日历模板
                self._safe_execute(
                    service.spreadsheets().batchUpdate,
                    spreadsheetId=self.google_sheet_id,
                    body={'requests': [{'addSheet': {'properties': {'title': cell_sheet_name}}}]}
                )
                
                # 设置表头
                header_range = f'{cell_sheet_name}!{start_col_letter}{start_row}:{chr(ord(start_col_letter)+6)}{start_row}'
                self._safe_execute(
                    service.spreadsheets().values().update,
                    spreadsheetId=self.google_sheet_id,
                    range=header_range,
                    valueInputOption='RAW',
                    body={'values': [['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']]}
                )

            # 读取现有内容
            existing_content = ""
            existing_result = self._safe_execute(
                service.spreadsheets().values().get,
                spreadsheetId=self.google_sheet_id,
                range=target_cell
            )
            existing_content += existing_result.get('values', [[]])[0][0] if existing_result.get('values') else ""
            
            # 更新单元格
            updated_content = f"{existing_content}\n\n{message}" if existing_content else message
            self._safe_execute(
                service.spreadsheets().values().update,
                spreadsheetId=self.google_sheet_id,
                range=target_cell,
                valueInputOption='RAW',
                body={'values': [[updated_content]]}
            )
            self.plog(f'Google Sheet Updated: {self.google_sheet_id} at {target_cell}')
            
        except Exception as e:
            self.plog(f'Google Sheet操作失败: {str(e)}')

if __name__ == "__main__":
    BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, 'config.ini'), encoding='utf-8')
    notification = NotificationEngine(config)
    notification.send_futu_message(['HK.00001','HK.00002'],['HK.00001 顶背离','HK.00002 底背离'],[100,200],[90,190])
    notification.send_telegram_message('{} test'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time()))))
    notification.send_email('group','{} test'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time())))) 