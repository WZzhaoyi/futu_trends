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

class EmailEngine:
    def plog(self,content):
        print('{} {}'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time())), content))

    def __init__(self, config:configparser.ConfigParser):
        """
            Email Engine Constructor
        """
        self.mail_port = config.get("CONFIG", "EMAIL_PORT")
        self.mail_host = config.get("CONFIG", "EMAIL_SERVER")
        self.sender = config.get("CONFIG", "EMAIL_SENDER")
        self.mail_pass = config.get("CONFIG", "EMAIL_PASWD")

    def send_email(self, receivers:str|list[str], filter_name: str, message_html: str):

        message = MIMEText(message_html, 'html', 'utf-8')
        message["From"] = self.sender
        message['To'] = ','.join(receivers) if isinstance(receivers, list) else receivers
        message['Subject'] = Header(f"Futu Stock Trends - {datetime.today().strftime('%Y-%m-%d')} - {filter_name}", 'utf-8')

        try:
            smtpObj = smtplib.SMTP_SSL(self.mail_host, self.mail_port) #建立smtp连接，ssl 465端口
            smtpObj.login(self.sender, self.mail_pass)  #登陆
            smtpObj.sendmail(self.sender, receivers, message.as_string())  #发送
            smtpObj.quit()
            self.plog(f'Email Sent: {receivers}')
        except (gaierror, ConnectionRefusedError):
            self.plog('Failed to connect to the server. Bad connection settings?')
        except smtplib.SMTPServerDisconnected:
            self.plog('Failed to connect to the server. Wrong user/password?')
        except smtplib.SMTPException as e:
            self.plog('SMTP error occurred: ' + str(e))
