import configparser
from argparse import ArgumentParser

def build_parser():
    parser = ArgumentParser()
    parser.add_argument(
        "--config",
        dest="config",
        help="start config file path",
        metavar="CONFiG",
        default="./config_template.ini"
    )
    return parser

def get_config():
    parser = build_parser()
    config_path = parser.parse_args().config
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    return config

if __name__ == '__main__':
    config = get_config()
    print(config.get("CONFIG", "FUTU_HOST"))