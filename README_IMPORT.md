# 古诗词数据导入SQLite工具

这个工具用于将中国古典诗词数据导入到SQLite数据库中，方便进行查询、修改和扩展。

## 功能特点

- 支持导入项目中所有的古诗词数据集
- 自动识别每个数据集的字段结构，并创建相应的数据库表
- 为每个表添加`translation`和`interpretation`字段，用于保存翻译和解释内容
- 支持选择性导入特定的数据集
- 分批处理数据，提高大量数据导入的效率
- 详细的日志记录，方便跟踪导入过程
- 提供错误处理和恢复机制

## 安装依赖

该脚本只依赖Python标准库，无需安装额外的依赖包。

## 使用方法

### 基本用法

导入所有数据集：

```bash
python import_to_sqlite.py
```

### 查看可用的数据集

```bash
python import_to_sqlite.py --list
```

### 导入特定数据集

```bash
python import_to_sqlite.py --datasets shijing lunyu songci
```

### 强制重建表

如果表已存在，默认会跳过。使用`--force`选项可以强制删除并重建表：

```bash
python import_to_sqlite.py --force
```

### 指定配置文件

```bash
python import_to_sqlite.py --config /path/to/config.json
```

### 指定数据库文件路径

```bash
python import_to_sqlite.py --db /path/to/database.db
```

### 设置每批导入的数据量

```bash
python import_to_sqlite.py --batch-size 2000
```

### 显示详细日志

```bash
python import_to_sqlite.py --verbose
```

## 数据库结构

导入后，每个数据集会在数据库中创建一个独立的表。每个表包含：

1. 原始数据中的所有字段
2. 自动添加的字段：
   - `id`: 自增主键
   - `translation`: 用于保存翻译内容
   - `interpretation`: 用于保存解释内容

## 数据表说明

| 表名 | 对应数据集 | 说明 |
|------|------------|------|
| wudai_huajianji | 五代-花间集 | 花间集诗词 |
| wudai_nantang | 五代-南唐 | 南唐诗词 |
| yuanqu | 元曲 | 元代戏曲 |
| tangsong | 全唐诗全宋诗 | 唐宋诗词合集 |
| songci | 宋词 | 宋代词作 |
| youmengying | 幽梦影-张潮文集 | 张潮文集 |
| caocao | 曹操诗集 | 曹操的诗作 |
| chuci | 楚辞 | 先秦文学名著 |
| lunyu | 论语 | 儒家经典 |
| shijing | 诗经 | 先秦诗歌总集 |

## 常见问题

1. **问题**: 导入过程中出现"表已存在"的警告
   **解决方法**: 使用`--force`选项重建表

2. **问题**: 找不到指定的数据集
   **解决方法**: 使用`--list`选项查看可用的数据集名称

3. **问题**: 数据量大导致导入速度慢
   **解决方法**: 调整`--batch-size`参数优化导入性能

4. **问题**: 字段名含有特殊字符导致错误
   **解决方法**: 脚本会自动处理特殊字符，无需手动修改

## 使用示例

### 示例1: 导入诗经和论语

```bash
python import_to_sqlite.py --datasets shijing lunyu
```

### 示例2: 强制重建所有表并使用详细日志

```bash
python import_to_sqlite.py --force --verbose
```

### 示例3: 使用自定义数据库路径和批处理大小

```bash
python import_to_sqlite.py --db ./my_poetry.db --batch-size 500
```

## 拓展使用

导入数据后，您可以使用SQLite工具或编程语言查询数据库。例如，使用Python查询诗经：

```python
import sqlite3

conn = sqlite3.connect('chinese_poetry.db')
cursor = conn.cursor()

# 查询诗经中的前10首诗
cursor.execute("SELECT * FROM shijing LIMIT 10")
poems = cursor.fetchall()

for poem in poems:
    print(poem)

conn.close()
```

## 注意事项

- 数据库文件可能会很大，确保有足够的磁盘空间
- 导入过程可能需要较长时间，特别是对于大型数据集
- 数据库表中已包含`translation`和`interpretation`字段，可以使用SQL更新语句添加翻译和解释内容
