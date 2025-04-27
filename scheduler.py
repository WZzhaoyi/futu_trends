#!/usr/bin/env python
# -*- coding: utf-8 -*-

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

import os
import logging
import configparser
from typing import Dict, List, Any, Optional, Callable, Union, Tuple
from datetime import datetime, time as dt_time, timedelta
import pandas as pd
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
import cmd
import pandas_market_calendars as mcal

from ft_config import build_parser
from tools import code_in_futu_group
from trends import check_trends
from notification_engine import NotificationEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('futu_trends.log')
    ]
)
logger = logging.getLogger('scheduler')

MARKET_EXCHANGE_MAP = {'HK': 'XHKG', 'US': 'XNYS', 'CN': 'XSHG'}
MARKET_TIMEZONE_MAP = {'HK': 'Asia/Hong_Kong', 'US': 'America/New_York', 'CN': 'Asia/Shanghai'}

def load_single_config(config_path: str) -> Optional[Dict[str, Any]]:
    """
    加载单个配置文件
    
    Args:
        config_path: 配置文件路径
        
    Returns:
        Optional[Dict[str, Any]]: 配置字典，如果加载失败则返回None
    """
    try:
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')
        
        # 提取关键配置
        config_data = {
            'file_path': config_path,
            'file_name': os.path.basename(config_path),
            'futu_group': config.get('CONFIG', 'FUTU_GROUP', fallback=''),
            'futu_push_type': config.get('CONFIG', 'FUTU_PUSH_TYPE', fallback='K_DAY'),
            'trend_type': config.get('CONFIG', 'TREND_TYPE', fallback='reverse'),
            'futu_keyword': config.get('CONFIG', 'FUTU_KEYWORD', fallback=''),
            'market': config.get('CONFIG', 'MARKET', fallback='HK'),  # 默认为港股
            'raw_config': config
        }
        
        logger.info(f"加载配置文件: {config_path}")
        return config_data
    except Exception as e:
        logger.error(f"加载配置文件失败: {config_path}, 错误: {str(e)}")
        return None

def load_config_files(config_dir: str) -> List[Dict[str, Any]]:
    """
    加载配置文件目录中的所有ini配置文件
    
    Args:
        config_dir: 配置文件目录
        
    Returns:
        List[Dict[str, Any]]: 配置列表
    """
    configs = []
    
    # 确保目录存在
    if not os.path.exists(config_dir):
        logger.warning(f"配置目录不存在: {config_dir}")
        return configs
    
    # 遍历目录中的所有ini文件
    for filename in os.listdir(config_dir):
        if filename.endswith('.ini'):
            config_path = os.path.join(config_dir, filename)
            config = load_single_config(config_path)
            if config:
                configs.append(config)
    
    return configs

def create_trend_task(config: Dict[str, Any]) -> Callable[[], Optional[pd.DataFrame]]:
    """
    创建趋势检测任务
    
    Args:
        config: 配置字典
        
    Returns:
        Callable: 任务函数
    """
    def task_func() -> Optional[pd.DataFrame]:
        try:
            logger.info(f"执行趋势检测任务: {config['file_name']}")
            
            # 获取配置
            futu_group = config['futu_group']
            futu_host = config['raw_config'].get('CONFIG', 'FUTU_HOST', fallback='127.0.0.1')
            futu_port = int(config['raw_config'].get('CONFIG', 'FUTU_PORT', fallback='11111'))
            futu_push_type = config['futu_push_type']
            
            # 获取自选股列表
            ls = code_in_futu_group(futu_group, futu_host, futu_port)
            if ls is None or ls.empty:
                logger.warning(f"自选股集合为空: {futu_group}")
                return None
            
            # 检测趋势
            trends_df = check_trends(ls, config['raw_config'])
            if trends_df is None or trends_df.empty:
                logger.info(f"未检测到趋势信号: {futu_group}")
                return None
            
            # 发送通知
            notification = NotificationEngine(config['raw_config'])
            notification.send_futu_message(trends_df.index.tolist(), trends_df['msg'].tolist())
            notification.send_telegram_message(
                '{} {} {}:\n{}'.format(
                    datetime.now().strftime('%Y-%m-%d'),
                    futu_group,
                    futu_push_type,
                    '\n'.join(trends_df['msg'])
                ),
                'https://www.futunn.com/'
            )
            notification.send_email(
                futu_group,
                '<p>{} {} {}:\n{}</p>'.format(
                    datetime.now().strftime('%Y-%m-%d'),
                    futu_group,
                    futu_push_type,
                    '<br>'.join(trends_df['msg'])
                )
            )
            
            logger.info(f"趋势检测任务完成: {futu_group}")

            return trends_df
        except Exception as e:
            logger.error(f"趋势检测任务失败: {str(e)}")
            return None
    
    return task_func

class SchedulerManager(cmd.Cmd):
    """调度器管理类"""
    
    intro = '欢迎使用Futu Trends调度器命令行界面。输入help或?查看命令列表。\n'
    prompt = '(scheduler) '
    
    def __init__(self, config_dir: str = None, timezone: str = 'Asia/Shanghai', config: str = './config_template.ini', log_level: str = 'INFO'):
        """
        初始化调度器管理器
        
        Args:
            config_dir: 配置文件目录，如果提供则优先使用此目录
            timezone: 时区，默认为'Asia/Shanghai'
            config: 单个配置文件路径，当config_dir为None时使用
        """
        super().__init__()
        self.config_dir = config_dir
        self.timezone = timezone
        self.logger = logging.getLogger('scheduler')
        self.logger.setLevel(log_level)
        
        # 任务存储
        self.tasks = {}
        self.task_configs = {}
        
        # 创建并初始化调度器
        self.scheduler = self._create_scheduler()
        self.scheduler.start()
        
        # 如果指定了config_dir，则优先从目录读取配置
        if config_dir:
            self.logger.info(f"加载配置目录: {config_dir}")
            if not self.load_dir_tasks(self.config_dir):
                self.logger.warning(f"从配置目录加载任务失败: {self.config_dir}")
        else:
            self.add_task(config)
    
    def _create_scheduler(self) -> BackgroundScheduler:
        """
        创建调度器
        
        Returns:
            BackgroundScheduler: 调度器
        """
        scheduler = BackgroundScheduler(
            jobstores={
                'default': MemoryJobStore()
            },
            executors={
                'default': ThreadPoolExecutor(2)
            },
            job_defaults={
                'coalesce': True,  # 合并延迟的任务
                'max_instances': 1,  # 限制最大实例数为1
                'misfire_grace_time': 300  # 允许5分钟的延迟执行时间
            },
            timezone=self.timezone
        )
        
        self.logger.info("调度器创建完成")
        return scheduler
    
    def _add_cron_job(self, task_id: str, task_func: Callable[[], Optional[pd.DataFrame]], cron_expression: str, replace_existing=True, **kwargs) -> None:
        """
        添加Cron定时任务
        
        Args:
            task_id: 任务ID
            task_func: 任务函数
            cron_expression: Cron表达式
            **kwargs: 其他参数
        """
        self.scheduler.add_job(
            task_func,
            CronTrigger.from_crontab(cron_expression),
            id=task_id,
            name=task_id,
            replace_existing=replace_existing,
            **kwargs
        )
        self.logger.debug(f"添加Cron任务: {task_id}, 表达式: {cron_expression}")
    
    def _add_interval_job(self, task_id: str, task_func: Callable[[], Optional[pd.DataFrame]], interval: Union[int, timedelta], start_date: Optional[datetime] = None, replace_existing=True, **kwargs) -> None:
        """
        添加间隔定时任务
        
        Args:
            task_id: 任务ID
            task_func: 任务函数
            interval: 间隔时间（秒或timedelta对象）
            start_date: 开始时间，默认为None，表示立即开始
            **kwargs: 其他参数
        """
        # 添加任务
        self.scheduler.add_job(
            task_func,
            IntervalTrigger(seconds=interval if isinstance(interval, int) else interval.total_seconds(), start_date=start_date),
            id=task_id,
            name=task_id,
            replace_existing=replace_existing,
            **kwargs
        )
        
        self.logger.debug(f"添加间隔任务: {task_id}, 间隔: {interval}, 开始时间: {start_date if start_date else '立即'}")
    
    def run_interactive(self) -> None:
        """运行交互式命令行界面"""
        self.cmdloop()
    
    def shutdown(self) -> None:
        """关闭调度器"""
        if self.scheduler:
            self.scheduler.shutdown()
    
    def add_task(self, config_source: Union[str, Dict[str, Any]]) -> Optional[str]:
        """
        添加任务
        
        Args:
            config_source: 配置文件路径或配置对象
            
        Returns:
            Optional[str]: 任务ID，如果添加失败则返回None
        """
        # 如果传入的是配置文件路径，则加载配置
        if isinstance(config_source, str):
            config = load_single_config(config_source)
            if not config:
                self.logger.error(f"添加任务失败: 无法加载配置文件 {config_source}")
                return None
        else:
            # 如果传入的是配置对象，直接使用
            config = config_source
        
        # 创建更简洁的任务ID
        market = config['market']
        kline_type = config['futu_push_type']
        futu_group = config['futu_group']
        file_name = config['file_path']
        
        # task_id = f"{market}_{kline_type}_{futu_group}_{file_name}"
        task_id = f"{file_name}"
        
        # 如果任务已存在，先移除
        if task_id in self.tasks:
            self.logger.info(f"任务已存在，将更新: {task_id}")
            self.remove_task(task_id)
        
        # 创建任务函数
        task_func = create_trend_task(config)
        
        # 注册任务
        self.tasks[task_id] = task_func
        self.task_configs[task_id] = config
        self.logger.info(f"注册任务: {task_id} {market} {kline_type} {futu_group}")
        
        # 获取市场收盘时间
        trading_time = get_market_trading_time(market, self.timezone, offset_minutes=30)
        if trading_time is None:
            self.logger.warning(f"无法获取市场收盘时间")
            return task_id
        else:
            _, close_datetime = trading_time
            close_hour, close_minute = close_datetime.hour, close_datetime.minute
            self.logger.debug(f"市场 {market} 收盘时间: {close_hour}:{close_minute}")
        
        # 获取市场对应的时区
        market_timezone = MARKET_TIMEZONE_MAP.get(market, self.timezone)
        
        # 获取当前时间在市场时区下的日期
        market_now = datetime.now(pytz.timezone(market_timezone))
        
        if kline_type == 'K_DAY':
            # 日线任务，每个交易日收盘后执行
            # 检查市场收盘时间是否跨越一天
            day_crossed_type = check_day_crossed(market_timezone, self.timezone, close_hour, close_minute)
            
            # 获取本地收盘时间
            market_tz = pytz.timezone(market_timezone)
            local_tz = pytz.timezone(self.timezone)
            market_now = datetime.now(market_tz)
            market_close_time = market_now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
            local_close_time = market_close_time.astimezone(local_tz)
            
            # 根据跨越类型设置cron表达式
            if day_crossed_type == 1:
                # market_tz时间早于local_tz
                cron_expression = f'{local_close_time.minute} {local_close_time.hour} * * 2-6'
            elif day_crossed_type == -1:
                # market_tz时间晚于local_tz
                cron_expression = f'{local_close_time.minute} {local_close_time.hour} * * 1-4,7'
            else:
                # 无跨越
                cron_expression = f'{local_close_time.minute} {local_close_time.hour} * * 1-5'
            
            self._add_cron_job(task_id, task_func, cron_expression)
        elif kline_type == 'K_WEEK':
            # 周线任务，每周收盘后执行
            self._add_cron_job(task_id, task_func, f'{close_minute} {close_hour} * * 6')
        elif kline_type == 'K_MON':
            # 月线任务，每月最后一日收盘后执行
            # 获取本月最后一日
            last_day = (market_now.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            self._add_cron_job(task_id, task_func, f'{close_minute} {close_hour} {last_day.day} * *')
        elif kline_type in ['K_1M', 'K_5M', 'K_15M', 'K_30M', 'K_60M']:
            # 分钟线任务，根据K线类型设置间隔
            minutes = int(kline_type.split('_')[1][:-1])
            
            # 获取市场的开盘和收盘时间
            trading_time = get_market_trading_time(market, self.timezone)
            if not trading_time:
                self.logger.warning(f"无法获取市场 {market} 的交易时间，无法添加任务: {task_id}")
                return task_id
            
            open_datetime, close_datetime = trading_time
            
            # 检查当前时间是否已经超过开盘时间
            current_time = datetime.now(pytz.timezone(self.timezone))
            
            # 设置任务启动时间
            start_date = None if current_time >= open_datetime else open_datetime
            
            # 添加任务
            self._add_interval_job(task_id, task_func, minutes * 60, start_date)
            
            # 创建任务结束函数
            def end_task():
                self.logger.info(f"市场 {market} 收盘，任务 {task_id} 结束")
                # 移除当前任务
                if task_id in self.scheduler.get_jobs():
                    self.scheduler.remove_job(task_id)
                
                # 获取下一个交易日的开盘和收盘时间
                next_day = close_datetime + timedelta(days=1)
                next_trading_time = get_market_trading_time(market, self.timezone, next_day)
                
                if next_trading_time:
                    next_open_datetime, next_close_datetime = next_trading_time
                    # 添加下一个交易日的任务
                    self.logger.info(f"市场 {market} 下一个交易日开盘时间: {next_open_datetime.strftime('%Y-%m-%d %H:%M:%S')}, 任务将在 {next_open_datetime} 启动")
                    self._add_interval_job(task_id, task_func, minutes * 60, next_open_datetime)
                    
                    # 添加下一个交易日的任务结束函数
                    self.scheduler.add_job(
                        end_task,
                        DateTrigger(run_date=next_close_datetime),
                        id=f"{task_id}_end",
                        replace_existing=True
                    )
                else:
                    self.logger.warning(f"无法获取市场 {market} 的下一个交易日交易时间，无法添加下一个交易日任务")
            
            # 添加任务结束函数
            self.scheduler.add_job(
                end_task,
                DateTrigger(run_date=close_datetime),
                id=f"{task_id}_end"
            )
        else:
            self.logger.warning(f"无法获取任务时间")
            return task_id
        
        return task_id
    
    def remove_task(self, task_id: str) -> bool:
        """
        移除任务
        
        Args:
            task_id: 任务ID
            
        Returns:
            bool: 是否成功移除
        """
        if not task_id:
            self.logger.error("移除任务失败: 任务ID不能为空")
            return False
        
        try:
            # 从调度器中移除任务
            self.scheduler.remove_job(task_id)
            
            # 从任务存储中移除
            if task_id in self.tasks:
                del self.tasks[task_id]
            if task_id in self.task_configs:
                del self.task_configs[task_id]
                
            self.logger.info(f"移除任务: {task_id}")
            return True
        except Exception as e:
            self.logger.error(f"移除任务失败: {task_id}, 错误: {str(e)}")
            return False
    
    def list_tasks(self) -> List[str]:
        """
        列出所有任务
        
        Returns:
            List[str]: 任务ID列表
        """
        return list(self.tasks.keys())
    
    def load_dir_tasks(self, config_dir: Optional[str] = None, reload: bool = False) -> bool:
        """
        加载目录中所有任务
        
        Args:
            config_dir: 配置目录，如果为None则使用初始化时的目录
            
        Returns:
            bool: 是否成功加载
        """
        config_dir = config_dir or self.config_dir
        
        # 获取当前所有任务ID
        current_tasks = self.list_tasks()
        
        # 加载新配置
        configs = load_config_files(config_dir)
        if not configs:
            self.logger.error(f"未找到配置文件: {config_dir}")
            return False
        
        # 移除所有现有任务
        if reload:
            for task_id in current_tasks:
                self.remove_task(task_id)
        
        # 添加新任务
        for config in configs:
            task_id = self.add_task(config)
            if not task_id:
                self.logger.error(f"加载任务失败: {config['file_path']}")
        
        self.logger.info(f"加载{config_dir} 共 {len(configs)} 个任务")
        return True
    
    # 命令行界面方法
    def do_list(self, arg):
        """列出所有任务"""
        task_list = self.list_tasks()
        if not task_list:
            print("当前没有任务")
            return
            
        print("当前任务列表:")
        for i, task_id in enumerate(task_list):
            config = self.task_configs.get(task_id)
            if config:
                print(f"{i+1}. {task_id} - {config['futu_group']} ({config['futu_push_type']}, 市场: {config['market']})")
            else:
                print(f"{i+1}. {task_id}")
    
    def do_add(self, arg):
        """添加任务: add <配置文件路径>"""
        if not arg:
            print("请指定配置文件路径")
            return
            
        task_id = self.add_task(arg)
        if task_id:
            print(f"已添加任务: {task_id}")
        else:
            print(f"添加任务失败: 无法加载配置文件 {arg}")
    
    def do_remove(self, arg):
        """移除任务: remove <任务ID>"""
        if not arg:
            print("请指定任务ID")
            return
            
        if self.remove_task(arg):
            print(f"已移除任务: {arg}")
        else:
            print(f"移除任务失败: {arg}")
    
    def do_load(self, arg):
        """加载目录: load [配置目录]"""
        config_dir = arg if arg else None
        
        if self.load_dir_tasks(config_dir, reload=False):
            print("已加载目录")
        else:
            print("加载目录中任务失败")
    
    def do_reload(self, arg):
        """清空后加载目录: reload [配置目录]"""
        config_dir = arg if arg else None
        
        if self.load_dir_tasks(config_dir, reload=True):
            print("已重新加载目录")
        else:
            print("加载目录中任务失败")
    
    def do_exit(self, arg):
        """退出程序"""
        print("正在关闭调度器...")
        self.shutdown()
        return True
    
    def do_quit(self, arg):
        """退出程序"""
        return self.do_exit(arg)
    
    def do_EOF(self, arg):
        """处理EOF信号（Ctrl+D/Ctrl+Z），退出程序"""
        return self.do_exit(arg)

def check_day_crossed(market_tz: str, local_tz: str, close_hour: int, close_minute: int) -> int:
        """
        检查市场收盘时差是否跨越一天
        
        Args:
            market_tz: 市场时区
            local_tz: 本地时区
            close_hour: 收盘小时
            close_minute: 收盘分钟
            
        Returns:
            int: -1表示market_tz时间晚于local_tz，0表示无跨越，1表示market_tz时间早于local_tz
        """
        # 获取市场时区和本地时区
        market_timezone = pytz.timezone(market_tz)
        local_timezone = pytz.timezone(local_tz)
        
        # 获取当前时间在两个时区下的表示
        market_now = datetime.now(market_timezone)
        local_now = datetime.now(local_timezone)
        
        # 计算时差（小时）
        time_diff = int((local_now.utcoffset() - market_now.utcoffset()).total_seconds() / 3600)
        
        # 创建一个市场收盘时间的datetime对象
        market_close_time = market_now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        
        # 转换为本地时间
        local_close_time = market_close_time.astimezone(local_timezone)
        
        # 检查收盘时间是否跨越了一天
        day_crossed = market_close_time.date() != local_close_time.date()
        
        if day_crossed:
            if time_diff > 0:
                # market_tz时间早于local_tz
                return 1
            else:
                # market_tz时间晚于local_tz
                return -1
        else:
            # 无跨越
            return 0

def get_market_trading_time(market: str, timezone: str = 'Asia/Shanghai', start_date: Optional[datetime] = None, offset_minutes: int = 0) -> Optional[Tuple[datetime, datetime]]:
    """
    获取指定市场的最近一个交易日的开盘和收盘时间
    
    Args:
        market: 市场代码，如'HK', 'US', 'CN'
        timezone: 时区，默认为'Asia/Shanghai'
        start_date: 开始日期，默认为None，表示从今天开始
        offset_minutes: 收盘时间的偏移量（分钟），默认为0分钟
        
    Returns:
        Optional[Tuple[datetime, datetime]]: 市场开盘和收盘时间，如果获取失败则返回None
    """
    if market not in MARKET_EXCHANGE_MAP:
        logger.warning(f"未知市场: {market}")
        return None
    
    try:
        # 获取交易所日历
        exchange = mcal.get_calendar(MARKET_EXCHANGE_MAP[market])
        
        # 获取开始日期
        if start_date is None:
            start_date = datetime.now().date()
        else:
            start_date = start_date.date()
        
        # 获取最近30个交易日的日历
        end_date = start_date + timedelta(days=30)
        schedule = exchange.schedule(start_date=start_date, end_date=end_date)
        
        if schedule.empty:
            logger.warning(f"未来30天内没有交易日: {market}")
            return None
        
        # 获取第一个交易日的开盘和收盘时间
        open_time = schedule.iloc[0]['market_open']
        close_time = schedule.iloc[0]['market_close']
        
        # 转换为指定时区
        tz = pytz.timezone(timezone)
        open_time = open_time.astimezone(tz)
        close_time = close_time.astimezone(tz)
        
        # 加上偏移量
        close_time = close_time + timedelta(minutes=offset_minutes)
        
        return open_time, close_time
    except Exception as e:
        logger.error(f"获取市场交易时间失败: {market}, 错误: {str(e)}")
        return None

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    
    # 调度器管理器
    scheduler_manager = SchedulerManager(
        config_dir=args.config_dir,
        timezone=args.timezone,
        config=args.config
    )
    
    scheduler_manager.run_interactive()