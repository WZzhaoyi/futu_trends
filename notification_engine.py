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
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from socket import gaierror
import time
import os
import json
from requests_html import HTMLSession
from futu import *

class NotificationEngine:
    def plog(self,content):
        print('{} {}'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time())), content))

    def __init__(self, config:configparser.ConfigParser):
        """
            Notification Engine Constructor
        """
        # Email configuration
        self.mail_port = config.get("CONFIG", "EMAIL_PORT")
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

    def send_futu_message(self, codes:list[str], messages:list[str]):
        """
        根据关键词存入futu group
        """
        if not self.futu_keyword:
            self.plog('没有futu关键词，跳过存入futu group')
            return

        quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
        
        for keyword in self.futu_keyword:
            code_list = []
            for code, msg in zip(codes, messages): 
                if keyword in msg:
                    code_list.append(code)
            if code_list:
                ret, data = quote_ctx.modify_user_security(keyword, ModifyUserSecurityOp.ADD, code_list)
                if ret == RET_OK:
                    self.plog(f'{",".join(code_list)} 存入{keyword}')
                else:
                    self.plog(f'存入{keyword}失败 {data}')
                time.sleep(1)
        
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
        message['Subject'] = Header(f"Futu Stock Trends - {datetime.today().strftime('%Y-%m-%d')} - {subject}", 'utf-8')

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

if __name__ == "__main__":
    BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, 'config.ini'), encoding='utf-8')
    notification = NotificationEngine(config)
    notification.send_futu_message(['HK.00001','HK.00002'],['HK.00001 顶背离','HK.00002 底背离'])
    notification.send_telegram_message('{} test'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time()))))
    notification.send_email('group','{} test'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time())))) 