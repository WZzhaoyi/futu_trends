import configparser
from argparse import ArgumentParser
import os

def build_parser(path:str=None):
    parser = ArgumentParser(description='Futu Trends')
    parser.add_argument(
        "--config",
        dest="config",
        help="config file path",
        default=path
    )
    parser.add_argument(
        '--config-dir',
        dest='config_dir',
        help='config file directory path',
    )
    parser.add_argument(
        '--timezone',
        dest='timezone',
        help='timezone',
        default='Asia/Shanghai'
    )
    parser.add_argument(
        '--log-level',
        dest='log_level',
        help='log level',
        default='INFO'
    )
    return parser

def get_config(path:str=None):
    parser = build_parser(path)
    args = parser.parse_args()
    config = configparser.ConfigParser()
    config.read(args.config, encoding='utf-8')
    return config

if __name__ == '__main__':
    config = get_config('config_template.ini')
    print(config.get("CONFIG", "FUTU_HOST"))