#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
LLM客户端模块，统一使用OpenAI SDK
"""

import openai
import os
from typing import Dict, Any
import json
import httpx

def load_json_config(config_file: str) -> Dict[str, Any]:
    """加载JSON配置文件"""
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    else:
        raise FileNotFoundError(f"配置文件不存在: {config_file}")

def generate_text_with_config(
    config_file_path: str,
    format: Dict[str, Any] = {}
) -> str:
    """
    使用配置文件生成文本
    
    Args:
        config_file_path: json配置文件路径
        format: prompt模板参数
        
    Returns:
        str: 生成的文本
    """
    if not os.path.exists(config_file_path):
        raise FileNotFoundError(f"配置文件不存在: {config_file_path}")
    llm_config = load_json_config(config_file_path)
    
    # 验证配置结构
    if 'prompt_template' not in llm_config:
        raise ValueError("配置文件中缺少 'prompt_template' 字段")
    if 'ai_model' not in llm_config:
        raise ValueError("配置文件中缺少 'ai_model' 字段")
    
    # 格式化prompt模板
    try:
        prompt = llm_config['prompt_template'].format(**format)
    except KeyError as e:
        raise ValueError(f"prompt模板中缺少必需的参数: {e}")
    
    ai_model_config = llm_config['ai_model']
    
    # 验证必需的配置项
    if "api_base" not in ai_model_config:
        raise ValueError("ai_model配置中缺少 'api_base' 字段")
    if "api_key" not in ai_model_config:
        raise ValueError("ai_model配置中缺少 'api_key' 字段")
    
    # 配置HTTP客户端（处理代理）
    http_client = None
    if ai_model_config.get("proxy"):
        http_client = httpx.Client(proxy=ai_model_config["proxy"])
    
    # 调用AI模型
    client = openai.OpenAI(
        base_url=ai_model_config["api_base"],
        api_key=ai_model_config["api_key"],
        http_client=http_client
    )

    # 构建API调用参数（排除配置参数，只保留API参数）
    excluded_keys = {"api_base", "api_key", "proxy"}
    api_params = {
        "messages": [{"role": "user", "content": prompt}],
        **{k: v for k, v in ai_model_config.items() if k not in excluded_keys}
    }
    
    try:
        response = client.chat.completions.create(**api_params)
    except openai.APIError as e:
        raise RuntimeError(f"OpenAI API调用失败: {e}")
    except Exception as e:
        raise RuntimeError(f"调用AI模型时发生错误: {e}")
    
    # 解析AI响应
    if not response.choices or len(response.choices) == 0:
        raise RuntimeError("API响应中没有返回任何选择")
    
    message = response.choices[0].message
    if not message or not message.content:
        raise RuntimeError("API响应中消息内容为空")
    
    if hasattr(message, 'reasoning_content') and message.reasoning_content:
        reasoning_content = message.reasoning_content
        content = message.content
        print("--------------thinking process------------------")
        print(reasoning_content)
        print("--------------decision------------------")
        print(content)
        return content
    else:
        return message.content