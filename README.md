<p align="center">
  <a href="https://github.com/chinese-poetry/chinese-poetry">
      <img src="https://avatars3.githubusercontent.com/u/30764933?s=200&v=4" alt="chinese-poetry">
  </a>
</p>

<h2 align="center">chinese-poetry: 最全中文诗歌古典文集数据库</h2>

<p align="center">
  <a href="https://travis-ci.com/chinese-poetry/chinese-poetry" rel="nofollow">
    <img height="28px" alt="Build Status" src="https://img.shields.io/travis/chinese-poetry/chinese-poetry?style=for-the-badge" style="max-width:100%;">
  </a>
  <a href="https://github.com/chinese-poetry/chinese-poetry/blob/master/LICENSE">
    <img height="28px" alt="License" src="http://img.shields.io/badge/license-mit-blue.svg?style=for-the-badge" style="max-width:100%;">
  </a>
  <a href="https://github.com/chinese-poetry/chinese-poetry/graphs/contributors">
    <img height="28px" alt="Contributors" src="https://img.shields.io/github/contributors/chinese-poetry/chinese-poetry.svg?style=for-the-badge" style="max-width:100%;">
  </a>
  <a href="https://www.patreon.com/jackeygao" rel="nofollow">
    <img height="28px" alt="Patreon" src="https://img.shields.io/endpoint.svg?url=https%3A%2F%2Fshieldsio-patreon.vercel.app%2Fapi%3Fusername%3Djackeygao%26type%3Dpledges&style=for-the-badge" style="max-width:100%;">
  </a>
</p>


最全的中华古典文集数据库，包含 5.5 万首唐诗、26 万首宋诗、2.1 万首宋词和其他古典文集。诗人包括唐宋两朝近 1.4 万古诗人，和两宋时期 1.5 千古词人。数据来源于互联网。

**为什么要做这个仓库?** 古诗是中华民族乃至全世界的瑰宝，我们应该传承下去，虽然有古典文集，但大多数人并没有拥有这些书籍。从某种意义上来说，这些庞大的文集离我们是有一定距离的。而电子版方便拷贝，所以此开源数据库诞生了。此数据库通过 JSON 格式分发，可以让你很方便的开始你的项目。

古诗采集没有记录过程，因为古诗数据庞大，目标网站有限制，采集过程经常中断超过了一个星期。2017 年新加入全宋词，[全宋词爬取过程及数据分析](https://jackeygao.github.io/r/words/crawl-ci.html)。

## 高频词分析图

<details open>
  <summary><b>宋词受欢迎的词牌名</b></summary>

<div align="center">
<img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/ci_rhythmic_topK.png" alt="两宋喜欢的词牌名">
</div>
</details>

<details>
  <summary><b>宋词高频词</b></summary>
  <img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/ci_words_topK.png" alt="宋词高频词" style="max-width:100%;">
</details>

<details>
  <summary><b>宋词作者作品榜</b></summary>
  <img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/ci_author_topK.png" alt="宋词作者作品榜" style="max-width:100%;">
</details>

<details>
  <summary><b>唐诗高频词</b></summary>
  <img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/tang_text_topK.png" alt="唐诗高频词" style="max-width:100%;">
</details>

<details>
  <summary><b>唐诗作者作品榜</b></summary>
  <img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/tang_author_topK.png" alt="唐诗作者作品榜" style="max-width:100%;">
</details>

<details>
  <summary><b>宋诗高频词</b></summary>
  <img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/song_text_topK.png" alt="宋诗高频词" style="max-width:100%;">
</details>

<details>
  <summary><b>宋诗作者作品榜</b></summary>
  <img src="https://raw.githubusercontent.com/jackeygao/chinese-poetry/master/images/song_author_topK.png" alt="宋诗作者作品榜" style="max-width:100%;">
</details>

## 数据集

- [唐诗宋诗](./全唐诗)
- [全宋词](./宋词)
- [五代·花间集](./五代诗词/huajianji)
- [五代·南唐二主词](./五代诗词/nantang)
- [论语](./论语)
- [诗经](./诗经)
- [幽梦影](./幽梦影)
- [四书五经](./四书五经)
- [蒙学](./蒙学)
- [纳兰性德诗集](./纳兰性德)
- [御定全唐詩](./御定全唐詩)


## 贡献

本项目目的是借助技术来生成格式化(JSON)数据，让开发者更方便快速的构建诗词类应用程序。身单力薄，欢迎更多人来维护，你可以通过以下方法来参与贡献：

- 直接提交 PR 或者通过 issue 讨论来优化完善此数据库，理论上古诗歌体非宗教类都欢迎加入，部分有争议性的数据需要社区投票讨论决定是否加入。关于诗句的纠错在创建 PR 时请标明出处。更多规范请[参考贡献规范文档](https://github.com/chinese-poetry/chinese-poetry/wiki/%E5%8F%82%E4%B8%8E%E8%B4%A1%E7%8C%AE%E8%A7%84%E8%8C%83)。

- 如果你没有办法直接参与完善的过程，你也可以通过 「[爱发电赞助](https://afdian.net/a/chinese-poetry)」  「[Patreon 周期性赞助](https://www.patreon.com/jackeygao)」 的形式来持续帮助并激励我去优化完善此数据库。如果您不喜欢周期性赞助，你也可以通过「[支付宝](https://github.com/jackeyGao/JackeyGao.github.io/blob/master/static/images/alipay.png)」或者「[微信赞赏码](https://github.com/jackeyGao/JackeyGao.github.io/blob/master/static/images/wechat.jpg)」进行一次性赞助(备注留下邮箱)。

- 如有建议或吐槽，欢迎联系我的邮箱 gaojunqi@outlook.com。

无论通过哪种形式贡献最终都会使之变得更好！

### 赞助者

无

### 贡献者

<p align="center">
<img src="https://opencollective.com/chinese-poetry/contributors.svg?width=890&button=false" alt="Contributors">
</p>

## 案例展示

<details>
  <summary>案例展示</summary>
  
- [中文诗歌主页](https://chinese-poetry.github.io)是一个基于浏览器的诗词网站，包含唐诗三百首、宋词三百首等文集。
- [animalize](https://github.com/animalize) **/** [QuanTangshi](https://github.com/animalize/QuanTangshi)  *离线全唐诗 Android*
- [justdark](https://github.com/justdark) **/** [pytorch-poetry-gen](https://github.com/justdark/pytorch-poetry-gen)  *a char-RNN based on pytorch*
- [Clover27](https://github.com/Clover27) **/** [ancient-Chinese-poem-generator](https://github.com/Clover27/ancient-Chinese-poem-generator)  *Ancient-Chinese-Poem-Generator*
- [chinese-poetry](https://github.com/chinese-poetry) **/** [poetry-calendar](http://chinese-poetry.github.io/poetry-calendar/)  *诗词周历*
- [chenyuntc](https://github.com/chenyuntc) **/** [pytorch-book](https://github.com/chenyuntc/pytorch-book/blob/master/chapter9-神经网络写诗(CharRNN)/) *简体唐诗生成(char-RNN)，可生成藏头诗，自定义诗歌意境，前缀等。*
- [okcy1016](https://github.com/okcy1016) **/** [poetry-desktop](https://github.com/okcy1016/poetry-desktop/) *诗词桌面*
- [huangjianke](https://github.com/huangjianke) **/** [weapp-poem](https://github.com/huangjianke/weapp-poem/) *诗词墨客 小程序版*
- [汉字之美](https://hz.xusenlin.com/) *汉字之美是一个方便查询的诗词网站，简洁干净，方便使用。*
- [PaddlePaddle](https://github.com/PaddlePaddle) **/** [PaddleNLP](https://github.com/PaddlePaddle/PaddleNLP#%E4%BA%A4%E4%BA%92%E5%BC%8Fnotebook%E6%95%99%E7%A8%8B) *基于ERNIE-GEN(Transformer)的深度学习诗词生成，可自行修改逻辑来生成多种诗词风格。*
- [Harold-y](https://github.com/Harold-y) **/** [chinese-poetry-db-web](https://github.com/Harold-y/chinese-poetry-db-web) *基于本仓库的MySQL DB整合 + 诗词Web端展示与检索*
  
</details>

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=chinese-poetry/chinese-poetry&type=Date)](https://star-history.com/#chinese-poetry/chinese-poetry&Date)

## License

[MIT](https://github.com/chinese-poetry/chinese-poetry/blob/master/LICENSE) 许可证。

# 中国古典诗词翻译工具

这个项目提供了一套工具，用于将中国古典诗词导入 SQLite，并基于大模型将原文翻译为现代汉语、生成整体解读。

## 翻译脚本

当前可用的翻译入口是 `translate_poetry.py`。

它会：

- 读取 `loader/datas.json`
- 遍历数据库中存在的数据表
- 按 `tag` 字段提取原文
- 调用上游接口生成 `translation` 和 `interpretation`
- 将状态、错误信息和结果回写 SQLite

## 功能特点

- 支持 `loader/datas.json` 中的全部数据集
- 支持通过 `.env` 配置凭据、模型和并发参数
- 支持上游接口并发到 500
- 使用数据库状态字段恢复中断任务
- 翻译失败或原文异常时不会删除原始记录
- 使用批量提交降低 SQLite 写入开销

## 安装要求

建议使用 `uv` 执行 Python 命令，并安装项目依赖：

```bash
uv pip install -r requirements.txt
```

## 环境变量

先复制示例文件：

```bash
cp .env.example .env
```

至少需要配置：

- `POETRY_API_KEY`
- `POETRY_MODEL`
- `POETRY_BASE_URL`
- `POETRY_MAX_CONCURRENCY`
- `POETRY_REQUEST_TIMEOUT`
- `POETRY_DB_WRITE_BATCH_SIZE`

## 使用方法

### 查看帮助

```bash
uv run python translate_poetry.py --help
```

### 运行翻译

```bash
uv run python translate_poetry.py
```

### 指定数据库、配置文件和并发

```bash
uv run python translate_poetry.py \
  --db chinese_poetry.db \
  --config loader/datas.json \
  --tables shijing \
  --limit 10 \
  --batch 500 \
  --workers 500
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|-------|
| `--db` | 数据库文件路径 | `./chinese_poetry.db` |
| `--config` | 数据集配置文件路径 | `./loader/datas.json` |
| `--batch` | 单轮拉取记录数 | `50` |
| `--tables` | 仅处理指定表 | 全部表 |
| `--limit` | 最多处理记录数 | 不限制 |
| `--workers` | 最大并发数 | 从 `.env` 读取 |
| `--model` | 翻译模型 | 从 `.env` 读取 |
| `--base-url` | 接口基础地址 | 从 `.env` 读取 |
| `--timeout` | 请求超时时间 | 从 `.env` 读取 |
| `--write-batch-size` | SQLite 批量提交阈值 | 从 `.env` 读取 |
| `--log-level` | 日志级别 | `INFO` |

命令行参数优先级高于 `.env`。

## 数据库字段

翻译脚本会为目标表补齐以下字段：

- `translation`
- `interpretation`
- `translation_status`
- `translation_updated_at`
- `translation_error`
- `translation_retry_count`

状态含义：

- `pending`：待翻译
- `processing`：处理中
- `done`：已完成
- `failed`：失败，可后续重试
- `skipped`：跳过，但保留原始记录

## 恢复机制

系统不再依赖单独的进度文件，而是直接使用数据库状态字段恢复执行：

1. 已完成记录不会重复翻译
2. `failed` 记录会在后续运行中继续尝试
3. 上次中断时处于 `processing` 的记录会在新一轮启动时回收为 `pending`

## 错误处理

系统的处理策略如下：

1. API 请求失败会做有限重试
2. 重试后仍失败时，记录写为 `failed`
3. 原文为空或模型明确判定不可翻译时，记录写为 `skipped`
4. 原始古文记录不会被删除

## Go 向量检索

项目提供基于 SiliconFlow、Qdrant 和 SQLite 的 Go 向量检索服务，支持文本或
1024 维向量输入，并只返回一个最相关结果。部署与导入方式见
[`docs/vector-search-docker.md`](docs/vector-search-docker.md)。

## 开发与贡献

欢迎提交问题报告和改进建议。如需贡献代码，请遵循以下步骤：

1. Fork项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add some amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建Pull Request

## 许可证

本项目基于MIT许可证 - 详见 LICENSE 文件。

## 联系方式

如有问题或建议，请通过以下方式联系：

- 项目主页：[https://github.com/your-username/chinese-poetry-translation](https://github.com/your-username/chinese-poetry-translation)
- 邮箱：your-email@example.com
