# ima-obsidian

面向 IMA 与 Obsidian 工作流的技能集合。每个技能放在 `skills/` 下的独立目录，包含自己的 `SKILL.md` 和所需脚本、参考资料。

## 技能目录

| 技能 | 用途 | 入口 |
| --- | --- | --- |
| IMA拉取入库前 | 把 IMA 等来源的语音转写整理成结构化纪要卡片，提取记录信息、主题和待办；缺失字段由用户确认 | [ima-transcript-cards](./skills/ima-transcript-cards/SKILL.md) |
| ima-to-obsidian | 从 IMA 知识库反向同步条目到 Obsidian vault 落盘成 markdown，含增量判定、敏感拦截、IMA 回链生成 | [ima-to-obsidian](./skills/ima-to-obsidian/SKILL.md) |

## 仓库结构

```text
ima-obsidian/
├── README.md
└── skills/
    ├── ima-transcript-cards/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── ttm.py
    └── ima-to-obsidian/
        ├── SKILL.md
        ├── config.json
        └── scripts/
            ├── ima_mcp_direct.py
            └── ima_to_obsidian.py
```

## 使用方式

1. 打开上方技能入口，阅读适用范围与执行步骤。
2. 安装工具支持从 GitHub 子目录安装时，选择对应 `skills/<技能目录>`；手动安装时，将该目录整体复制到所用 AI 工具的技能目录。
3. 根据本机环境设置 Python 和 Obsidian 库路径。当前技能示例使用 `D:/OB`，目录约定见技能说明。

从仓库根目录检查脚本命令：

```text
python skills/ima-transcript-cards/scripts/ttm.py --help
python skills/ima-to-obsidian/scripts/ima_to_obsidian.py --help
```

## 添加技能

在 `skills/` 下新建独立文件夹，添加含 `name`、`description` 的 `SKILL.md`；需要时再加入 `scripts/`、`references/` 或 `assets/`，并在首页技能目录登记。多个技能可以一起提交，也可以分别更新。

本次目录调整将原根目录的 `SKILL.md` 和 `scripts/` 移入 `skills/ima-transcript-cards/`。使用旧仓库路径的安装配置需要更新子目录；已安装到本机的副本需按原工具的方式更新。
