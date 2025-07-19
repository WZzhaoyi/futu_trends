from typing import Dict, Any, Optional
import sqlite3
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
import os
from pymongo import MongoClient, ASCENDING, DESCENDING

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
    def __init__(self, db_uri: str, init_db: bool = False):
        """
        初始化数据库连接
        
        Args:
            db_uri: 数据库连接URI，支持以下格式：
                   - SQLite: "sqlite:///path/to/database.db"
                   - MongoDB: "mongodb+srv://username:password@host:port/database"
        """
        self.db_uri = db_uri
        if self.db_uri.startswith('sqlite:///'):
            # SQLite数据库
            self.db_path = self.db_uri.replace('sqlite:///', '')
            if init_db:
                self._init_sqlite()
        elif self.db_uri.startswith('mongodb+srv://'):
            # MongoDB数据库 连接错误和重试
            mongo_options = {
                'serverSelectionTimeoutMS': 30000,  # 服务器选择超时
                'connectTimeoutMS': 20000,          # 连接超时
                'socketTimeoutMS': 30000,           # Socket超时
                'retryWrites': True,                # 启用重试写入
                'retryReads': True,                 # 启用重试读取
                'readPreference': 'secondaryPreferred',  # 优先从节点，提高可用性
            }
            
            self.mongo_client = MongoClient(self.db_uri, **mongo_options)
            
            # 从URI中提取数据库名称
            db_name = self.db_uri.split('/')[-1]
            self.mongo_db = self.mongo_client[db_name]
            if init_db:
                self._init_mongo()
            else:
                self.params_collection = self.mongo_db['strategy_params']
        else:
            raise ValueError("不支持的数据库URI格式")
    
    def _init_sqlite(self) -> None:
        """初始化SQLite数据库表结构"""
        sql_create_tables = """
        CREATE TABLE IF NOT EXISTS stock_params (
            stock_code TEXT PRIMARY KEY,
            best_params JSON NOT NULL,
            meta_info JSON NOT NULL,
            performance JSON NOT NULL,
            last_updated TIMESTAMP NOT NULL,
            source_file TEXT NOT NULL
        );
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(sql_create_tables)
            logger.info(f"SQLite database initialized at {self.db_path}")

    def _init_mongo(self) -> None:
        """初始化MongoDB集合和索引"""
        # 创建集合
        self.params_collection = self.mongo_db['strategy_params']
        
        # 创建索引
        self.params_collection.create_index([
            ('stock_code', ASCENDING)
        ])
        self.params_collection.create_index([
            ('last_updated', DESCENDING)
        ])
        
        logger.info(f"MongoDB database {self.mongo_db.name} initialized")

    def _get_file_timestamp(self, file_path: str) -> datetime:
        """获取文件创建时间"""
        try:
            filename = Path(file_path).stem
            date_str = filename.split('_')[-1]
            return datetime.strptime(date_str, '%Y%m%d')
        except (ValueError, IndexError):
            stats = os.stat(file_path)
            create_time = stats.st_ctime
            return datetime.fromtimestamp(create_time)

    def _parse_params_file(self, file_path: str) -> Dict[str, StockParams]:
        """解析参数文件"""
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        file_timestamp = self._get_file_timestamp(file_path)
        
        return {
            code: StockParams(
                stock_code=code,
                best_params=params['best_params'],
                meta_info=params['meta_info'],
                performance=params['performance'],
                last_updated=file_timestamp,
                source_file=file_path
            )
            for code, params in data.items()
        }

    def import_params(self, params_file: str) -> None:
        """导入参数文件到数据库"""
        stock_params = self._parse_params_file(params_file)
        
        for params in stock_params.values():
            self._update_stock_params(params)
        
        logger.info(f"Successfully imported parameters from {params_file}")

    def _update_stock_params(self, params: StockParams) -> None:
        """更新股票参数到数据库"""
        # 准备参数数据
        params_data = {
            'stock_code': params.stock_code,
            'best_params': params.best_params,
            'meta_info': params.meta_info,
            'performance': params.performance,
            'last_updated': params.last_updated,
            'source_file': params.source_file
        }
        
        if self.db_uri.startswith('sqlite:///'):
            # SQLite更新
            with sqlite3.connect(self.db_path) as conn:
                # 检查是否需要更新
                current_record = conn.execute("""
                    SELECT last_updated 
                    FROM stock_params 
                    WHERE stock_code = ?
                """, (params.stock_code,)).fetchone()
                
                if current_record:
                    current_timestamp = datetime.fromisoformat(current_record[0])
                    if current_timestamp > params.last_updated:
                        logger.info(f"Skipping SQLite update for {params.stock_code}: "
                                  f"Newer parameters exist")
                        return
                
                # 更新主表
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

                logger.info(f"Updated SQLite parameters for {params.stock_code}")
        elif self.db_uri.startswith('mongodb+srv://'):
            # MongoDB更新
            # 检查是否需要更新
            current_doc = self.params_collection.find_one({'stock_code': params.stock_code})
            
            if current_doc:
                current_timestamp = current_doc['last_updated']
                if current_timestamp > params.last_updated:
                    logger.info(f"Skipping MongoDB update for {params.stock_code}: "
                              f"Newer parameters exist")
                    return
            
            # 更新主文档
            self.params_collection.update_one(
                {'stock_code': params.stock_code},
                {'$set': params_data},
                upsert=True
            )
            
            logger.info(f"Updated MongoDB parameters for {params.stock_code}")

    def get_stock_params(self, stock_code: str) -> Dict[str, Any] | None:
        """从数据库读取股票参数
        
        Args:
            stock_code: 股票代码
        
        Returns:
            Dict[str, Any] | None: 包含best_params等参数的字典，如果未找到返回None
        """
        if self.db_uri.startswith('mongodb+srv://'):
            try:
                result = self.params_collection.find_one({'stock_code': stock_code})
                
                if result:
                    return {
                        'best_params': result['best_params'],
                        'meta_info': result['meta_info'],
                        'performance': result['performance']
                    }
            except Exception as e:
                logger.error(f"Error reading from MongoDB for {stock_code}: {str(e)}")
                return None
        elif self.db_uri.startswith('sqlite:///'):
            try:
                with sqlite3.connect(self.db_path) as conn:
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
                logger.error(f"Error reading from SQLite for {stock_code}: {str(e)}")
                return None

    def backup_to_file(self, file_path: str) -> bool:
        """备份数据库到文件
        
        Args:
            file_path: 备份文件路径，例如 'backup/params_20240505.json'
            
        Returns:
            bool: 备份是否成功
        """
        try:
            # 确保备份目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # 获取所有数据
            data = {}
            if self.db_uri.startswith('mongodb+srv://'):
                # MongoDB备份
                for doc in self.params_collection.find():
                    # 转换ObjectId和datetime为字符串
                    doc['_id'] = str(doc['_id'])
                    doc['last_updated'] = doc['last_updated'].isoformat()
                    data[doc['stock_code']] = doc
            else:
                # SQLite备份
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.execute("SELECT * FROM stock_params")
                    for row in cursor:
                        stock_code = row[0]  # stock_code是主键
                        data[stock_code] = {
                            'stock_code': stock_code,
                            'best_params': json.loads(row[1]),
                            'meta_info': json.loads(row[2]),
                            'performance': json.loads(row[3]),
                            'last_updated': row[4],
                            'source_file': row[5]
                        }
            
            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"Successfully backed up database to {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error backing up database: {str(e)}")
            return False

    def restore_from_file(self, file_path: str) -> bool:
        """从文件恢复数据库
        
        Args:
            file_path: 备份文件路径
            
        Returns:
            bool: 恢复是否成功
        """
        try:
            # 读取备份文件
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 将数据转换为StockParams对象
            stock_params = {
                code: StockParams(
                    stock_code=code,
                    best_params=params['best_params'],
                    meta_info=params['meta_info'],
                    performance=params['performance'],
                    last_updated=datetime.fromisoformat(params['last_updated']),
                    source_file=params['source_file']
                )
                for code, params in data.items()
            }
            
            # 使用现有的_update_stock_params方法恢复数据
            for params in stock_params.values():
                self._update_stock_params(params)
            
            logger.info(f"Successfully restored database from {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error restoring database: {str(e)}")
            return False

def main():
    parser = argparse.ArgumentParser(description='Database parameter management tool')
    parser.add_argument('--db', type=str, required=True, 
                       help='Database URI (sqlite:///path/to/db.db or mongodb+srv://host:port/db_name)')
    parser.add_argument('--mode', type=str, default='update', choices=['update', 'backup', 'restore'],
                       help='Operation mode: update/backup/restore')
    parser.add_argument('--params', type=str, required=False,
                       help='Parameter file path (required for update mode)')
    parser.add_argument('--backup', type=str, required=False,
                       help='Backup file path (required for backup/restore mode)')
    
    args = parser.parse_args()
    
    try:
        db = ParamsDB(args.db, init_db=True)
        
        if args.mode == 'update':
            if not args.params:
                raise ValueError("--params required for update mode")
            db.import_params(args.params)
            logger.info(f"Parameters updated: {args.params}")
            
        elif args.mode == 'backup':
            if not args.backup:
                raise ValueError("--backup required for backup mode")
            if db.backup_to_file(args.backup):
                logger.info(f"Database backed up to: {args.backup}")
                
        elif args.mode == 'restore':
            if not args.backup:
                raise ValueError("--backup required for restore mode")
            if db.restore_from_file(args.backup):
                logger.info(f"Database restored from: {args.backup}")
                
    except Exception as e:
        logger.error(f"Operation failed: {str(e)}")
        raise

if __name__ == '__main__':
    main() 