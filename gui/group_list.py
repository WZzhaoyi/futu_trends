import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import tkinter as tk
from tkinter import ttk, messagebox
from futu import *
import multiprocessing
import sys
import pandas as pd
from configparser import ConfigParser
from tools import code_in_futu_group
from ft_config import get_config
from signal_window import SignalWindow
import logging
import os
from datetime import datetime

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ThemeManager:
    """统一的主题管理类，与 lightweight_charts 样式对齐"""
    
    # 与 signal_window.py 中的颜色对齐
    # RGB(25, 25, 25) = #191919 (深色背景)
    # RGB(255, 255, 255) = #ffffff (浅色背景)
    
    DARK_THEME = {
        'bg': '#191919',           # rgb(25, 25, 25) - 与 lightweight_charts 对齐
        'fg': '#ffffff',           # rgb(255, 255, 255) - 白色文字
        'select_bg': '#2962FF',    # 选中背景色（蓝色）
        'heading_bg': '#333333',   # 表头背景色
        'entry_bg': '#333333',     # 输入框背景色
        'entry_fg': '#ffffff',     # 输入框文字颜色
    }
    
    LIGHT_THEME = {
        'bg': '#ffffff',           # rgb(255, 255, 255) - 白色背景
        'fg': '#000000',           # rgb(0, 0, 0) - 黑色文字
        'select_bg': '#2962FF',    # 选中背景色（蓝色）
        'heading_bg': '#e0e0e0',   # 表头背景色（浅灰）
        'entry_bg': '#f5f5f5',     # 输入框背景色（浅灰）
        'entry_fg': '#000000',     # 输入框文字颜色（黑色）
    }
    
    @staticmethod
    def get_theme_colors(dark_mode: bool):
        """获取主题颜色"""
        return ThemeManager.DARK_THEME if dark_mode else ThemeManager.LIGHT_THEME
    
    @staticmethod
    def apply_theme(root, dark_mode: bool = True):
        """
        应用主题样式到 Tkinter 窗口
        
        Args:
            root: Tkinter 根窗口
            dark_mode: 是否为深色模式，默认 True
        """
        style = ttk.Style()
        style.theme_use('clam')  # 使用 clam 引擎方便自定义颜色
        
        colors = ThemeManager.get_theme_colors(dark_mode)
        
        root.configure(bg=colors['bg'])
        
        # 配置 Treeview (列表) 样式
        style.configure("Treeview",
                        background=colors['bg'],
                        foreground=colors['fg'],
                        fieldbackground=colors['bg'],
                        borderwidth=0,
                        rowheight=30,
                        font=('Arial', 10))
        
        style.map('Treeview', background=[('selected', colors['select_bg'])])
        
        # 配置表头样式
        style.configure("Treeview.Heading",
                        background=colors['heading_bg'],
                        foreground=colors['fg'],
                        relief="flat",
                        font=('Arial', 10, 'bold'))
        
        # 配置滚动条
        style.configure("Vertical.TScrollbar",
                        background=colors['heading_bg'],
                        troughcolor=colors['bg'],
                        bordercolor=colors['bg'],
                        arrowcolor=colors['fg'])
        
        return colors

class WatchListApp:
    def __init__(self, root, config:ConfigParser):
        self.root = root
        self.config = config
        
        self.host = config.get("CONFIG", "FUTU_HOST")
        self.port = config.getint("CONFIG", "FUTU_PORT")
        self.group = config.get("CONFIG", "FUTU_GROUP", fallback='')
        # 从配置读取主题模式，与 signal_window.py 保持一致
        self.dark_mode = config.getboolean("CONFIG", "DARK_MODE", fallback=True)
        
        self.root.title(f"Watchlist {self.group}" if self.group else "Watchlist")
        self.root.geometry("350x600")
        
        # 应用主题（支持 dark/light 模式）
        self.theme_colors = ThemeManager.apply_theme(self.root, self.dark_mode)

        # 1. 顶部搜索框区域
        top_frame = tk.Frame(root, bg=self.theme_colors['bg'])
        top_frame.pack(fill="x", padx=10, pady=10)
        
        self.search_var = tk.StringVar()
        
        search_entry = tk.Entry(top_frame, textvariable=self.search_var, 
                                bg=self.theme_colors['entry_bg'], 
                                fg=self.theme_colors['entry_fg'], 
                                insertbackground=self.theme_colors['entry_fg'],
                                relief="flat", font=('Arial', 12))
        search_entry.pack(fill="x", ipady=5)
        # 占位符效果
        search_entry.insert(0, "Search / Filter...")
        search_entry.bind("<FocusIn>", lambda args: search_entry.delete('0', 'end'))

        # 2. 列表区域
        list_frame = tk.Frame(root, bg=self.theme_colors['bg'])
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # 定义列
        columns = ("code", "name")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        
        self.tree.heading("code", text="代码", anchor="w")
        self.tree.heading("name", text="名称", anchor="w")
        
        self.tree.column("code", width=100, anchor="w")
        self.tree.column("name", width=180, anchor="w")
        
        # 滚动条
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 3. 绑定事件
        self.tree.bind("<Double-1>", self.on_double_click) # 双击打开
        self.tree.bind("<Return>", self.on_double_click)   # 回车打开

        # 4. 加载数据
        self.code_pd = pd.DataFrame(columns=pd.Index(['code','name'])) # 存储完整数据用于过滤
        self.load_futu_data()
        self.search_var.trace("w", self.filter_list) # 监听输入变化

    def load_futu_data(self):
        """连接富途并获取分组数据"""
        # 初始化上下文
        # 获取股票列表
        self.code_pd = pd.DataFrame(columns=pd.Index(['code','name']))
        if self.group and self.host and self.port:
            ls = code_in_futu_group(self.group,self.host,self.port)
            if isinstance(ls, pd.DataFrame):
                self.code_pd = pd.concat([self.code_pd, ls[['code','name']]])
        code_list = self.config.get("CONFIG", "FUTU_CODE_LIST", fallback='').split(',')
        code_list = [code for code in code_list if code.strip()]
        if len(code_list) > 0:
            ls = pd.DataFrame({'code': code_list, 'name': code_list})
            self.code_pd = pd.concat([self.code_pd, ls])
        # 填充 UI
        self.refresh_list(self.code_pd)

    def refresh_list(self, df:pd.DataFrame):
        """刷新 Treeview"""
        # 清空
        for item in self.tree.get_children():
            self.tree.delete(item)
        # 插入
        for idx in range(len(df)):
            row = df.iloc[idx]
            self.tree.insert("", "end", values=(row['code'], row['name']))

    def filter_list(self, *args):
        """根据搜索框过滤"""
        keyword = self.search_var.get().lower()
        if not keyword:
            self.refresh_list(self.code_pd)
            return
        
        filtered = self.code_pd[self.code_pd['code'].str.contains(keyword) | self.code_pd['name'].str.contains(keyword)]
        self.refresh_list(filtered)

    def on_double_click(self, event):
        """处理点击事件，启动新进程打开看板"""
        selected_item = self.tree.selection()
        if not selected_item:
            return
        
        # 获取选中行的值
        item_values = self.tree.item(selected_item[0])['values']
        code = item_values[0] # 代码
        name = item_values[1] # 名称 (可选传给看板)
        
        logger.info(f"正在启动看板: {code} - {name} ...")
        
        # 核心：使用 multiprocessing 启动图表
        # 这样列表窗口不会卡死，且可以同时点开多个
        p = multiprocessing.Process(target=SignalWindow, args=(self.config, code))
        p.start()
        # 注意：这里我们不 join()，让子进程独立运行

if __name__ == '__main__':
    config = get_config()
    # Windows 下使用 multiprocessing 必须放在 if __name__ == '__main__' 下
    multiprocessing.freeze_support() 
    
    root = tk.Tk()
    app = WatchListApp(root, config)
    
    # 窗口置顶
    root.attributes("-topmost", True)
    
    root.mainloop()