_[English](CONTRIBUTING.md)_

# 给 Roboot 贡献代码

感谢看这份文档。Roboot 是一个私人 AI agent hub，维护者只有一个人，所以这份指南尽量短，并且直白地说明什么样的 PR 会被合进来。

## 范围

**接受：** bug 修复、安全加固、符合"Mac 上私人 agent"形态的新 Arcana 工具、新 I/O 适配器（语音 / 聊天平台）、文档。

**默认不接受：** 多租户、插件框架、云端托管部署、替换 agent 框架、Windows 支持。不是绝对不行——先开个 issue 聊完再动手，别自己埋头肝一个周末结果被拒。

拿不准的话，开个简短 issue 先问一下，比被 close 一个 PR 便宜多了。

## 跑起来

```bash
git clone git@github.com:tyxben/roboot.git
cd roboot
./scripts/setup.sh            # 装依赖、ffmpeg、预热 Whisper
cp config.example.yaml config.yaml
# 至少填一个 LLM provider 的 API key
python server.py              # http://localhost:8765
```

各入口的用途见 [docs/USAGE.zh.md](docs/USAGE.zh.md)。

## 跑测试

```bash
python -m pytest tests/        # 单测，~6s，离线
cd relay && npm run typecheck  # Cloudflare Worker + pair-page 的 TypeScript 检查
```

发版前还要走一遍 [docs/TESTING.md](docs/TESTING.md) —— 这份 checklist 覆盖自动化测不到的部分（手机配对、JARVIS 语音、iTerm2 桥接）。

## 提 PR

1. fork，从 `main` 切分支，分支保持单一关切。
2. 能测的改动就补测试。新的 pure-Python 模块对应一个 `tests/test_<module>.py`；适配器层改动在 `docs/TESTING.md` 加一条。
3. 推之前跑 `python -m pytest tests/` 和 `npm run typecheck`。
4. Commit 风格：祈使句、句首大写的标题，正文解释**为什么**并引用动机 issue/bug。仓库里的例子：
   - `Harden LAN API auth, self-upgrade, and cert bootstrap`
   - `Refactor STT into pluggable backend package`
   - `Fix CI: restrict setuptools package discovery`
5. 向 `main` 开 PR，按模板填（概要 + 测试计划）。
6. CI 在 push 时跑 pytest。review 时效尽力而为——超过一周没动静就在 PR 里 ping 一下。

不要求 CLA，不要求 DCO sign-off，不强制 squash/merge —— 合并时由维护者决定。

## 改 `soul.md`

不要 commit 你自己的实验性人格修改。`soul.md` 是活的身份文件，终端用户的本地版本已经和 repo 里的分叉了。如果你改的是 *schema*（节结构、解析方式），顺手改 `tools/soul.py` 并加 pytest。如果你改的是默认人格，在 PR 里显眼地说明——这会改变所有人下一次 pull 之后的 agent 行为。

## 安全问题

**漏洞不要开公开 issue。** 私下披露流程见 [SECURITY.md](SECURITY.md)。凡是可能暴露用户机器、API key、聊天记录、relay 流量的都算。

## 授权

提交即代表你同意你的变更按项目的 [MIT license](LICENSE) 发布。
