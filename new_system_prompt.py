#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys

# 读取原始文件
with open('translate_poetry.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 新的系统提示词
new_prompt = '''# 系统提示词
SYSTEM_PROMPT = """请你扮演一名精通古典文学和现代汉语的语言专家，将给定的中国古诗词按行逐句翻译为现代汉语，并对整首诗进行简要解读。

输入是一个古诗词句子列表，请你：

1. 对列表中的每一句古诗，分别用现代汉语准确且流畅地翻译成一句话，输出"translation"字段为对应的现代汉语句子列表，顺序一一对应。
2. 对整首诗作出简要的文化解读，涵盖意境、情感、历史背景或文化内涵，输出到"interpretation"字段。
3. 如果输入的内容为空或只有标点符号，请在translation中返回相应的空列表或原文，并在interpretation中说明无法翻译的原因。
4. 即使只有一句话或一个词，也请尽力翻译并解读。
5. 如果输入内容是"无正文"、"(残缺)"等特殊标记，请直接翻译这些词语，不要返回空结果。

返回 strictly 严格遵守以下 JSON 格式：

{
  "translation": [
    "现代汉语译文第一句",
    "现代汉语译文第二句",
    "现代汉语译文第三句",
    "现代汉语译文第四句"
  ],
  "interpretation": "这里填写对整首诗的简要解读，说明其表现的意境、情感，以及历史文化背景。"
}

请务必在回答中仅返回此 JSON 格式内容，避免额外文字说明。请确保JSON格式正确，特别注意不要在数组最后一个元素后添加多余的逗号。"""'''

# 查找旧的系统提示词
old_prompt_start = '# 系统提示词'
old_prompt_end = '请务必在回答中仅返回此 JSON 格式内容，避免额外文字说明。请确保JSON格式正确，特别注意不要在数组最后一个元素后添加多余的逗号。"""'

# 替换系统提示词
start_idx = content.find(old_prompt_start)
end_idx = content.find(old_prompt_end) + len(old_prompt_end)

if start_idx != -1 and end_idx != -1:
    new_content = content[:start_idx] + new_prompt + content[end_idx:]

    # 写入新文件
    with open('translate_poetry.py', 'w', encoding='utf-8') as f:
        f.write(new_content)

    print("系统提示词已更新")
else:
    print("未找到系统提示词，请检查文件")
