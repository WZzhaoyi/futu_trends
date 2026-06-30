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
import logging
import re
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from socket import gaierror
import time
import os
import json
from urllib.parse import quote
from requests_html import HTMLSession
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from futu_group import sync_futu_group

from .webhook import WebhookNotifier, HookResult

logger = logging.getLogger(__name__)


def _column_letter_to_index(column_letter):
    column_index = 0
    for char in column_letter:
        column_index = column_index * 26 + (ord(char) - ord('A') + 1)
    return column_index - 1


def _column_index_to_letter(column_index):
    letters = []
    column_index += 1
    while column_index:
        column_index, remainder = divmod(column_index - 1, 26)
        letters.append(chr(ord('A') + remainder))
    return ''.join(reversed(letters))


def _calendar_sheet_position(cell_origin):
    match = re.match(r'^([A-Z]+)(\d+)$', cell_origin)
    if not match:
        raise ValueError("Invalid cell origin format, expected like 'B2'")

    start_col_letter, start_row = match.groups()
    start_row = int(start_row)
    today = date.today()
    sheet_name = today.strftime('%y-%m')

    start_col = _column_letter_to_index(start_col_letter)
    first_day_of_month = datetime(today.year, today.month, 1)
    first_weekday = first_day_of_month.weekday() # （0=周一，6=周日）
    day, weekday = today.day, today.weekday()
    week = (day + first_weekday) // 7
    col_offset = (weekday + 1) % 7
    target_col = _column_index_to_letter(start_col + col_offset)
    target_row = start_row + week + 1
    header_end_col = _column_index_to_letter(start_col + 6)

    return {
        "sheet_name": sheet_name,
        "start_col_letter": start_col_letter,
        "start_row": start_row,
        "header_end_col": header_end_col,
        "target_col": target_col,
        "target_row": target_row,
    }


def _valid_target_prices(recent_high, recent_low):
    try:
        recent_high = float(recent_high)
        recent_low = float(recent_low)
    except (TypeError, ValueError):
        return False
    return recent_high > 0 and recent_low > 0 and recent_high != recent_low


def _feishu_cell_value_to_text(values):
    if not values or not values[0]:
        return ""

    value = values[0][0]
    if value is None:
        return ""
    return str(value)


class NotificationEngine:

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

        # Feishu Sheet configuration
        self.feishu_app_id = config.get("CONFIG", "FEISHU_APP_ID", fallback="")
        self.feishu_app_secret = config.get("CONFIG", "FEISHU_APP_SECRET", fallback="")
        self.feishu_static_tenant_access_token = config.get("CONFIG", "FEISHU_TENANT_ACCESS_TOKEN", fallback="")
        self.feishu_spreadsheet_token = config.get("CONFIG", "FEISHU_SPREADSHEET_TOKEN", fallback="")
        self.feishu_sheet_cell_origin = config.get("CONFIG", "FEISHU_SHEET_CELL_ORIGIN", fallback=self.google_sheet_cell_origin)
        self._feishu_tenant_access_token = ""
        self._feishu_token_expires_at = 0

        # Webhook configuration
        self._webhook = WebhookNotifier(config)

    def send_futu_message(self, codes:list[str], messages:list[str], highs:list[float], lows:list[float]):
        """
        根据关键词存入futu group
        """
        if not self.futu_keyword:
            logger.warning('没有futu关键词，跳过存入futu group')
            return

        for keyword in self.futu_keyword:
            matched_codes = []
            matched_highs = []
            matched_lows = []
            for code, msg, recent_high, recent_low in zip(codes, messages, highs, lows):
                if keyword not in msg:
                    continue
                if not _valid_target_prices(recent_high, recent_low):
                    continue
                matched_codes.append(code)
                matched_highs.append(recent_high)
                matched_lows.append(recent_low)

            if matched_codes:
                sync_futu_group(
                    keyword,
                    matched_codes,
                    host=self.host,
                    port=self.port,
                    price_up_list=matched_highs,
                    price_down_list=matched_lows,
                    overwrite=False,
                )


    def send_email(self, subject: str, message_html: str):
        """
        发送邮件
        """
        # 检查邮件配置是否完整
        if not all([self.mail_port, self.mail_host, self.sender, self.mail_pass]):
            logger.warning('邮件配置不完整，跳过发送')
            return

        # 检查是否有订阅者
        if not self.receivers:
            logger.warning('没有邮件订阅者，跳过发送')
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
            logger.info('Email Sent: %s', self.receivers)
        except (gaierror, ConnectionRefusedError):
            logger.error('Failed to connect to the server. Bad connection settings?')
        except smtplib.SMTPServerDisconnected:
            logger.error('Failed to connect to the server. Wrong user/password?')
        except smtplib.SMTPException as e:
            logger.error('SMTP error occurred: %s', e)

    def send_telegram_message(self, text, link='www.google.com'):
        """
        给电报发送文字消息
        """
        # 检查Telegram配置是否完整
        if not all([self.TELEGRAM_BOT_TOKEN, self.TELEGRAM_CHAT_ID]):
            logger.warning('Telegram配置不完整，跳过发送')
            return

        headers = {
            'Content-Type': 'application/json',
        }
        data = {
            "chat_id": self.TELEGRAM_CHAT_ID,
            "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": "查看原文", "url": link}]]},
        }
        url = f'https://api.telegram.org/bot{self.TELEGRAM_BOT_TOKEN}/sendMessage'
        try:
            self.SESSION.post(url, headers=headers, json=data, proxies=self.PROXIES)
            logger.info('Telegram Sent: %s', self.TELEGRAM_CHAT_ID)
        except:
            logger.error('网络代理错误，请检查确认后关闭本程序重试')

    def send_telegram_photo(self, img_url):
        """
        给电报发送图片
        """
        # 检查Telegram配置是否完整
        if not all([self.TELEGRAM_BOT_TOKEN, self.TELEGRAM_CHAT_ID]):
            logger.warning('Telegram配置不完整，跳过发送')
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
            logger.warning('Telegram配置不完整，跳过发送')
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

    def _safe_feishu_request(self, method, url, *, headers=None, **kwargs):
        """执行飞书 OpenAPI 请求，带重试和业务错误检查"""
        headers = headers or {}
        for attempt in range(3):
            try:
                response = self.SESSION.request(
                    method,
                    url,
                    headers=headers,
                    proxies=self.PROXIES,
                    timeout=30,
                    **kwargs,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code", 0) != 0:
                    raise RuntimeError(f"{data.get('code')}: {data.get('msg')}")
                return data
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(1)
        return None

    def _get_feishu_tenant_access_token(self):
        if self.feishu_static_tenant_access_token:
            return self.feishu_static_tenant_access_token

        if not all([self.feishu_app_id, self.feishu_app_secret]):
            raise ValueError("Feishu app_id/app_secret 未配置")

        now = time.time()
        if self._feishu_tenant_access_token and now < self._feishu_token_expires_at:
            return self._feishu_tenant_access_token

        data = self._safe_feishu_request(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "app_id": self.feishu_app_id,
                "app_secret": self.feishu_app_secret,
            },
        )
        self._feishu_tenant_access_token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 7200))
        self._feishu_token_expires_at = now + max(expire - 300, 60)
        return self._feishu_tenant_access_token

    def _feishu_headers(self):
        return {
            "Authorization": f"Bearer {self._get_feishu_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _get_feishu_sheets(self):
        data = self._safe_feishu_request(
            "GET",
            f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{self.feishu_spreadsheet_token}/sheets/query",
            headers=self._feishu_headers(),
        )
        return data.get("data", {}).get("sheets", [])

    def _create_feishu_sheet(self, title):
        data = self._safe_feishu_request(
            "POST",
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.feishu_spreadsheet_token}/sheets_batch_update",
            headers=self._feishu_headers(),
            json={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                            }
                        }
                    }
                ]
            },
        )

        for reply in data.get("data", {}).get("replies", []):
            properties = reply.get("addSheet", {}).get("properties", {})
            if properties.get("title") == title and properties.get("sheetId"):
                return properties["sheetId"]
        raise RuntimeError(f"Feishu sheet 创建成功但未返回 sheetId: {title}")

    def _write_feishu_values(self, cell_range, values):
        self._safe_feishu_request(
            "PUT",
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.feishu_spreadsheet_token}/values",
            headers=self._feishu_headers(),
            json={
                "valueRange": {
                    "range": cell_range,
                    "values": values,
                }
            },
        )

    def _read_feishu_value(self, cell_range):
        encoded_range = quote(cell_range, safe="")
        data = self._safe_feishu_request(
            "GET",
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{self.feishu_spreadsheet_token}/values/{encoded_range}",
            headers=self._feishu_headers(),
            params={
                "valueRenderOption": "ToString",
                "dateTimeRenderOption": "FormattedString",
            },
        )
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        return _feishu_cell_value_to_text(values)

    def send_feishu_sheet_message(self, message):
        """更新飞书电子表格"""
        if not all([self.feishu_spreadsheet_token, self.feishu_sheet_cell_origin]):
            logger.warning('飞书电子表格配置不完整，跳过发送')
            return

        try:
            position = _calendar_sheet_position(self.feishu_sheet_cell_origin)
            sheet_name = position["sheet_name"]

            sheets = self._get_feishu_sheets()
            sheet_id = next((sheet.get("sheet_id") for sheet in sheets if sheet.get("title") == sheet_name), "")
            if not sheet_id:
                sheet_id = self._create_feishu_sheet(sheet_name)
                header_range = (
                    f'{sheet_id}!{position["start_col_letter"]}{position["start_row"]}:'
                    f'{position["header_end_col"]}{position["start_row"]}'
                )
                self._write_feishu_values(header_range, [['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']])

            target_cell = f'{sheet_id}!{position["target_col"]}{position["target_row"]}:{position["target_col"]}{position["target_row"]}'
            existing_content = self._read_feishu_value(target_cell)
            updated_content = f"{existing_content}\n\n{message}" if existing_content else message
            self._write_feishu_values(target_cell, [[updated_content]])
            logger.info('Feishu Sheet Updated: %s at %s (%s)', self.feishu_spreadsheet_token, target_cell, sheet_name)

        except Exception as e:
            logger.error('飞书电子表格操作失败: %s', e)

    def send_google_sheet_message(self, message):
        """更新Google Sheet"""
        if not all([self.google_sheet_id, os.path.exists(self.google_api_json), self.google_sheet_cell_origin]):
            logger.warning('Google Sheet配置不完整，跳过发送')
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

            position = _calendar_sheet_position(self.google_sheet_cell_origin)
            start_col_letter = position["start_col_letter"]
            start_row = position["start_row"]
            cell_sheet_name = position["sheet_name"]
            target_col = position["target_col"]
            target_row = position["target_row"]
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
                header_range = f'{cell_sheet_name}!{start_col_letter}{start_row}:{position["header_end_col"]}{start_row}'
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
            logger.info('Google Sheet Updated: %s at %s', self.google_sheet_id, target_cell)

        except Exception as e:
            logger.error('Google Sheet操作失败: %s', e)

    def send_webhook(self, content: str) -> HookResult:
        """通过 Webhook 发送"""
        return self._webhook.send(content)


if __name__ == "__main__":
    BASE_DIR = os.path.split(os.path.realpath(__file__))[0]
    config = configparser.ConfigParser()
    config.read(os.path.join(BASE_DIR, '..', 'config.ini'), encoding='utf-8')
    notification = NotificationEngine(config)
    notification.send_futu_message(['HK.00001','HK.00002'],['HK.00001 顶背离','HK.00002 底背离'],[100,200],[90,190])
    notification.send_telegram_message('{} test'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time()))))
    notification.send_email('group','{} test'.format(time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(time.time()))))
