#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
LLM客户端模块，统一使用OpenAI SDK
"""

import argparse
import openai
from urllib.parse import urlparse, parse_qs, urljoin
import sys
import os
from typing import Dict, Any, Optional
import re
import configparser

def validate_temperature(temp: Any) -> Optional[float]:
    """
    验证温度值是否为0-2之间的浮点数
    
    Args:
        temp: 温度值（可以是字符串或浮点数）
        
    Returns:
        float: 验证后的温度值，如果无效则返回None
    """
    if temp is None or temp == '':
        return None
        
    # 如果是字符串，先验证格式
    if isinstance(temp, str):
        # 使用正则表达式验证格式：0-2之间的数字，最多一位小数
        if not re.match(r'^[0-2](\.\d)?$', temp):
            return None
        try:
            temp = float(temp)
        except ValueError:
            return None
    
    # 如果是数字，验证范围
    if isinstance(temp, (int, float)):
        if 0 <= temp <= 2:
            return float(temp)
            
    return None

def get_llm_config(config: configparser.ConfigParser) -> Dict[str, Any]:
    """
    从配置文件中读取LLM相关配置
    
    Args:
        config: ConfigParser对象
        
    Returns:
        Dict: 包含LLM配置的字典
    """
    return {
        'url': config.get("CONFIG", "LLM_URL", fallback=''),
        'prompt': config.get("CONFIG", "LLM_PROMPT", fallback=''),
        'temperature': config.get("CONFIG", "LLM_TEMPERATURE", fallback=''),
        'proxy': config.get("CONFIG", "PROXY", fallback=None)
    }

def generate_text_with_config(
    config: configparser.ConfigParser,
    content: str,
    format: str = "email"
) -> str:
    """
    使用配置文件生成文本
    
    Args:
        config: ConfigParser对象
        content: 要处理的内容
        format: 输出格式，支持 "markdown"、"email" 或 "plain"
        
    Returns:
        str: 生成的文本
    """
    llm_config = get_llm_config(config)
    
    if not llm_config['url'] or not llm_config['prompt']:
        return content
        
    prompt = read_prompt(llm_config['prompt'])
    msg = generate_text(
        url=llm_config['url'],
        prompt=prompt + '\n' + content,
        temperature=llm_config['temperature'],
        format=format,
        proxy=llm_config['proxy']
    )
    
    return msg + '\n\n当日信号如下：\n' + content

def parse_llm_url(url: str) -> Dict[str, str|None]:
    """
    解析LLM API URL，提取base_url、model和api_key
    
    Args:
        url: 格式为 {base_url}?model={model}&key={api_key}
        示例：
        - https://openrouter.ai/api/v1?model=gpt-3.5-turbo&key=YOUR_API_KEY
        - https://api.deepseek.com?model=deepseek-chat&key=YOUR_API_KEY
        - https://open.bigmodel.cn/api/paas/v4/?model=glm-4&key=YOUR_API_KEY
        
    Returns:
        Dict: 包含base_url、model和api_key的字典
    """
    # 使用?分割base_url和参数
    if '?' not in url or 'key=' not in url or 'model=' not in url:
        raise ValueError("URL为'?'分隔的base_url和参数key和model")
        
    base_url, query = url.split('?', 1)
    query_params = parse_qs(query)
    
    return {
        "base_url": base_url,
        "model": query_params.get("model", [None])[0],
        "api_key": query_params.get("key", [None])[0]
    }

def generate_text(
    url: str,
    prompt: str,
    temperature: float | str = None,
    max_tokens: int = 2000,
    format: str = "markdown",
    proxy: str = None
) -> str:
    """
    使用指定的URL生成文本
    
    Args:
        url: LLM API URL，格式为 {base_url}?model={model}&key={api_key}
        prompt: 输入提示
        temperature: 温度参数（0-2之间的浮点数，最多一位小数）
        max_tokens: 最大生成token数
        format: 输出格式，支持 "markdown"、"email" 或 "plain"
        
    Returns:
        str: 模型响应
    """
    params = parse_llm_url(url)
    if not all([params["base_url"], params["model"], params["api_key"]]):
        raise ValueError("URL必须包含base_url、model和api_key参数")
    
    # 验证温度值
    temperature = validate_temperature(temperature)
    
    # 根据格式添加提示
    if format == "plain":
        prompt = f"{prompt}\n\n请使用纯文本格式输出，不要使用Markdown语法。使用以下格式：\n" + \
                 "1. 使用空行分隔段落\n" + \
                 "2. 使用缩进表示层级\n" + \
                 "3. 使用括号()表示注释\n"
    elif format == "email":
        prompt = f"{prompt}\n\n请使用邮件友好的格式输出，遵循以下规则：\n" + \
                 "1. 使用 # 作为标题标记（最多使用三级标题）\n" + \
                 "2. 使用 - 作为列表标记\n" + \
                 "3. 使用 * 作为强调标记（不要使用 ** 加粗）\n" + \
                 "4. 使用空行分隔段落\n" + \
                 "5. 使用缩进表示层级关系\n" + \
                 "6. 使用 ===== 作为分隔线\n" + \
                 "7. 避免使用表格、代码块等复杂格式"

    client = openai.OpenAI(
        base_url=params["base_url"],
        api_key=params["api_key"],
        http_client=openai.DefaultHttpxClient(
            proxy=proxy
        ) if proxy else None
    )
    
    response = client.chat.completions.create(
        model=params["model"],
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        reasoning_effort='low'
    )

    return response.choices[0].message.content

def read_prompt(prompt: str) -> str:
    """
    读取prompt，支持从文件读取或直接使用字符串
    
    Args:
        prompt: prompt字符串或文件路径（以@开头）
        
    Returns:
        str: 处理后的prompt
    """
    if prompt.startswith('@'):
        # 从文件读取
        file_path = prompt[1:]
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            raise ValueError(f"无法读取prompt文件: {str(e)}")
    else:
        # 直接使用字符串
        return prompt

def main():
    parser = argparse.ArgumentParser(description='LLM API客户端')
    parser.add_argument('--url', required=True, help='LLM API URL，格式：{base_url}?model={model}&key={api_key}')
    parser.add_argument('--prompt', required=True, help='输入提示，可以是文本或文件路径（以@开头）')
    parser.add_argument('--temperature', type=float, default=None, help='温度参数（默认：None）')
    parser.add_argument('--max-tokens', type=int, default=2000, help='最大生成token数（默认：2000）')
    parser.add_argument('--format', choices=['markdown', 'email', 'plain'], default='markdown', help='输出格式（默认：markdown）')
    parser.add_argument('--proxy', type=str, default=None, help='代理地址')
    args = parser.parse_args()
    
    try:
        # 读取prompt
        prompt = read_prompt(args.prompt)
        
        # 生成文本
        response = generate_text(
            url=args.url,
            prompt=prompt,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            format=args.format,
            proxy=args.proxy
        )
        print(response)
    except Exception as e:
        print(f"错误: {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
    