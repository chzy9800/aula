# 贡献

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m unittest discover -s tests -v
node tests/vad.test.mjs
```

不需要任何 API key：`examples/fake_llm.py` 就是为此存在的。

## 提 PR 前

1. 跑一遍上面两条测试，再加一次 `python examples/e2e_check.py <port>`。
2. 新增行为要有测试。尤其是"失败路径"——本项目大多数 bug 都出在失败处理上（翻译接口挂掉、模型返回空、音频过短）。
3. 不要为了少一个依赖而自己写协议。反过来，也不要为了一件小事拖进来一个 30MB 的包。

## 改前端

`aula/static/` 下没有构建步骤：直接改 `index.html` / `app.js`（上课）或 `reader.html` / `reader.js`（讲义），刷新即可。

断句逻辑在 `app.js` 的 `WORKLET_SRC` 里，改完必须跑 `node tests/vad.test.mjs`——浏览器里很难调试音频，只能靠这层测试兜住。

## 提交信息

`feat:` / `fix:` / `docs:` / `refactor:` / `test:` + 一句说清"改了什么、为什么"。中文可以。

## 报告问题

请附：`aula doctor` 的输出、操作步骤、以及期望与实际。涉及转写质量时，说明用的是哪个 ASR 实现与模型。
