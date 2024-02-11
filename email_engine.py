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
#  Written by Joey <wzzhaoyi@outlook.com>, 2023
#  Copyright (c)  Joey - All Rights Reserved


# the first step is always the same: import all necessary components:
import smtplib
import ssl
import time
import configparser
import os
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from socket import gaierror


class EmailEngine:
    def plog(self,content):
        print('{} {}'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time())), content))

    def __init__(self):
        """
            Email Engine Constructor
        """
        self.BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
        config = configparser.ConfigParser()
        config.read(os.path.join(self.BASE_DIR, 'config.ini'), encoding='utf-8')
        self.EMAIL_PORT = config.get("CONFIG", "EMAIL_PORT")
        self.EMAIL_SERVER = config.get("CONFIG", "EMAIL_SERVER")
        self.EMAIL_SENDER = config.get("CONFIG", "EMAIL_SENDER")
        self.EMAIL_PASWD = config.get("CONFIG", "EMAIL_PASWD")

        # Create a secure SSL context
        self.context = ssl.create_default_context()

    def send_email(self, receiver: str, filter_name: str, message_html: str):
        message = MIMEMultipart("alternative")
        message["Subject"] = f"Futu Stock Trends - {datetime.today().strftime('%Y-%m-%d')} - {filter_name}"
        message["From"] = self.EMAIL_SENDER
        message["To"] = receiver
        text = "Please kindly review today's chosen stock list! "
        html = message_html

        # Turn these into plain/html MIMEText objects
        part1 = MIMEText(text, "plain")
        part2 = MIMEText(html, "html")

        # Add HTML/plain-text parts to MIMEMultipart message
        # The email client will try to render the last part first
        message.attach(part1)
        message.attach(part2)

        try:
            # send your message with credentials specified above
            with smtplib.SMTP(self.EMAIL_SERVER, self.EMAIL_PORT) as server:
                server.starttls(context=self.context)  # Secure the connection
                server.login(self.EMAIL_SENDER, self.EMAIL_PASWD)
                server.sendmail(self.EMAIL_SENDER, receiver, message.as_string())

            self.plog(f'Email Sent: {receiver}')
        except (gaierror, ConnectionRefusedError):
            self.plog('Failed to connect to the server. Bad connection settings?')
        except smtplib.SMTPServerDisconnected:
            self.plog('Failed to connect to the server. Wrong user/password?')
        except smtplib.SMTPException as e:
            self.plog('SMTP error occurred: ' + str(e))
