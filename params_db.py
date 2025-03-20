from typing import Dict, Any
import sqlite3
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
import os

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class StockParams:
    """股票参数数据类"""
    stock_code: str
    best_params: Dict[str, Any]
    meta_info: Dict[str, Any]
    performance: Dict[str, Any]
    last_updated: datetime
    source_file: str

class ParamsDB:
    """参数数据库管理类"""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self) -> None:
        """初始化数据库表结构"""
        sql_create_tables = """
        CREATE TABLE IF NOT EXISTS stock_params (
            stock_code TEXT PRIMARY KEY,
            best_params JSON NOT NULL,
            meta_info JSON NOT NULL,
            performance JSON NOT NULL,
            last_updated TIMESTAMP NOT NULL,
            source_file TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS params_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            params_snapshot JSON NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            source_file TEXT NOT NULL,
            FOREIGN KEY (stock_code) REFERENCES stock_params(stock_code)
        );
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(sql_create_tables)
            logger.info(f"Database initialized at {self.db_path}")

    def _get_file_timestamp(self, file_path: str) -> datetime:
        """获取文件创建时间"""
        # 尝试从文件名中解析时间戳 (如 analysis_params_20250317.json)
        try:
            filename = Path(file_path).stem
            date_str = filename.split('_')[-1]
            return datetime.strptime(date_str, '%Y%m%d')
        except (ValueError, IndexError):
            # 如果文件名解析失败，使用文件的创建时间
            stats = os.stat(file_path)
            # 优先使用创建时间，如果不可用则使用修改时间
            create_time = stats.st_ctime
            return datetime.fromtimestamp(create_time)

    def _parse_params_file(self, file_path: str) -> Dict[str, StockParams]:
        """解析参数文件"""
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        # 获取文件时间戳
        file_timestamp = self._get_file_timestamp(file_path)
        
        return {
            code: StockParams(
                stock_code=code,
                best_params=params['best_params'],
                meta_info={
                    'look_ahead': params['look_ahead'],
                    'target_multiplier': params['target_multiplier'],
                    'atr_period': params['atr_period']
                },
                performance=params['performance'],
                last_updated=file_timestamp,  # 使用文件时间戳
                source_file=file_path
            )
            for code, params in data.items()
        }

    def import_params(self, params_file: str) -> None:
        """导入参数文件到数据库"""
        stock_params = self._parse_params_file(params_file)
        
        with sqlite3.connect(self.db_path) as conn:
            for params in stock_params.values():
                self._update_stock_params(conn, params)
        
        logger.info(f"Successfully imported parameters from {params_file}")

    def _update_stock_params(self, conn: sqlite3.Connection, params: StockParams) -> None:
        """更新股票参数，检查完整内容是否相同"""
        # 获取当前记录
        current_record = conn.execute("""
            SELECT best_params, meta_info, performance, last_updated 
            FROM stock_params 
            WHERE stock_code = ?
        """, (params.stock_code,)).fetchone()

        # 准备新参数的JSON
        new_params = {
            'best_params': params.best_params,
            'meta_info': params.meta_info,
            'performance': params.performance
        }
        params_json = json.dumps(new_params, sort_keys=True)

        if current_record:
            # 构建当前记录的JSON以进行比较
            current_params = {
                'best_params': json.loads(current_record[0]),
                'meta_info': json.loads(current_record[1]),
                'performance': json.loads(current_record[2])
            }
            current_json = json.dumps(current_params, sort_keys=True)
            current_timestamp = datetime.fromisoformat(current_record[3])

            # 如果内容完全相同，跳过所有更新
            if current_json == params_json:
                logger.info(f"Skipping {params.stock_code}: Parameters are identical")
                return

            # 如果内容不同但现有记录更新，也跳过
            if current_timestamp > params.last_updated:
                logger.info(f"Skipping {params.stock_code}: Newer parameters exist "
                          f"(current: {current_timestamp}, new: {params.last_updated})")
                return

        # 检查历史记录中是否存在完全相同的记录
        existing_history = conn.execute("""
            SELECT id FROM params_history 
            WHERE stock_code = ? 
            AND json_extract(params_snapshot, '$') = ?
        """, (
            params.stock_code,
            params_json
        )).fetchone()

        if existing_history:
            logger.info(f"Skipping history record for {params.stock_code}: Identical record exists")
        else:
            # 插入新的历史记录
            conn.execute("""
                INSERT INTO params_history 
                    (stock_code, params_snapshot, timestamp, source_file)
                VALUES (?, ?, ?, ?)
            """, (
                params.stock_code,
                params_json,
                params.last_updated.isoformat(),
                params.source_file
            ))

        # 更新主表（只在内容不同时）
        conn.execute("""
            INSERT INTO stock_params 
                (stock_code, best_params, meta_info, performance, last_updated, source_file)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code) DO UPDATE SET
                best_params=excluded.best_params,
                meta_info=excluded.meta_info,
                performance=excluded.performance,
                last_updated=excluded.last_updated,
                source_file=excluded.source_file
            WHERE excluded.last_updated > stock_params.last_updated
        """, (
            params.stock_code,
            json.dumps(params.best_params),
            json.dumps(params.meta_info),
            json.dumps(params.performance),
            params.last_updated.isoformat(),
            params.source_file
        ))

def get_stock_params(db_path: str, stock_code: str) -> Dict[str, Any] | None:
    """从数据库读取股票参数
    
    Args:
        db_path: 数据库文件路径
        stock_code: 股票代码
    
    Returns:
        Dict[str, Any] | None: 包含best_params等参数的字典，如果未找到返回None
    """
    try:
        with sqlite3.connect(db_path) as conn:
            result = conn.execute("""
                SELECT best_params, meta_info, performance
                FROM stock_params 
                WHERE stock_code = ?
            """, (stock_code,)).fetchone()
            
            if result:
                return {
                    'best_params': json.loads(result[0]),
                    'meta_info': json.loads(result[1]),
                    'performance': json.loads(result[2])
                }
            return None
    except Exception as e:
        logger.error(f"Error reading parameters for {stock_code}: {str(e)}")
        return None

def main():
    parser = argparse.ArgumentParser(description='Import parameters to SQLite database')
    parser.add_argument('--db', type=str, required=True, help='Path to SQLite database')
    parser.add_argument('--params', type=str, required=True, help='Path to parameters JSON file')
    
    args = parser.parse_args()
    
    # 确保数据库目录存在
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        db = ParamsDB(str(db_path))
        db.import_params(args.params)
    except Exception as e:
        logger.error(f"Error during import: {str(e)}")
        raise

if __name__ == '__main__':
    main() 