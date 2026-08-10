# output_all 数据校验

日期：2026-07-31
对象：`output_all/`（2026-07-30 复用缓存跑出的全量结果）
结论：**不可发布。** 主榜单前列虚高最多 12.8 倍，排名严重失真。

> **已失效（2026-07-31 同日）**：本文校验的是单字段 `commits` 混入 PR 分支
> 提交的旧版输出。该问题已修复 —— 提交数拆成 `commits`（主干）与
> `commits_loose`（主干 + PR 分支）两个并列字段，`output_all/` 已用新代码
> 重跑覆盖。本文的虚高倍数表现在应读作两列的差值分析，仍可用于判断哪些人
> 的 PR 分支邮箱未被关联。口径说明见 `README.md`。

## 一、校验方法与可信度

用项目自己的 `parse_git_log` 重算，保证口径与生产代码一致（同样的时间窗
过滤、同样的 author date 判定、同样的 `--no-merges`）。对 34 个参与统计的
仓库分别跑两种引用范围。

先验证重算能否复现 output_all 的数字：

| 步骤 | 提交数 |
|---|---|
| 重算 `--all` 全部作者 | 13189 |
| 减去 email 可解析出 `[bot]` login 的 | −3695 |
| 减去 name 带 `[bot]` 但邮箱解析不出的 | −16 |
| = 重算结果 | 9478 |
| `output_all/by_repo.csv` commits 合计 | 9478 |

精确吻合，重算口径可信。

## 二、总量偏差

| 口径 | 窗口内提交数 | 独立邮箱数 |
|---|---|---|
| `--all`（output_all 实际使用） | 13189 | 304 |
| `--branches --tags`（主干） | 5687 | 152 |

`--all` 比主干多 7502 次提交，多出 152 个邮箱。

**152 个邮箱在主干上零提交** —— 它们的全部 3021 次提交只存在于
`refs/pull/*/head`。也就是说 output_all 里恰好一半的身份是 PR 分支的产物。

## 三、主榜单失真

`summary.csv` 与 `contributors.md` 的跨仓库汇总行（`repo` 列为空）现有排序
与主干口径对比：

| 现排名 | 姓名 | 现显示 | 主干实际 | 虚高倍数 |
|---|---|---|---|---|
| 1 | Han Wang (wanghan-iapcm) | 1307 | 130 | 10.1× |
| 2 | Mohan Chen (mohanchen) | 872 | 68 | 12.8× |
| 3 | A bot of @njzjz (njzjz-bot) | 556 | 159 | 3.5× |
| 4 | Jinzhe Zeng (njzjz) | 518 | 146 | 3.5× |
| 5 | dyzheng | 411 | 25 | 16.4× |
| 6 | OutisLi | 368 | 60 | 6.1× |
| 7 | Copilot | 351 | 84 | 4.2× |
| 8 | Xuwznln | 319 | 308 | 1.04× |
| 9 | njzjz-bot（无账号） | 230 | 0 | ∞ |
| 10 | Growl (Growl1234) | 216 | 30 | 7.2× |
| 14 | Duo (iProzd) | 135 | 15 | 9.0× |

按主干口径重排，前列会变成完全不同的人：

| 主干排名 | 姓名 | 主干 | 现显示 | 现排名 |
|---|---|---|---|---|
| 1 | Xuwznln | 308 | 319 | 8 |
| 2 | A bot of @njzjz | 159 | 556 | 3 |
| 3 | Jinzhe Zeng (njzjz) | 146 | 518 | 4 |
| 4 | Han Wang | 130 | 1307 | 1 |
| 5 | q434343 | 113 | 113 | 17 |
| 6 | AsymmetryChou | 109 | 113 | — |
| 7 | Ziqi Yin (Zikkying) | 85 | 86 | — |
| 9 | AIS-Square | 68 | 68 | — |

现榜单第一名实际排第 4；真正的第一名 Xuwznln 现在排第 8。
`q434343`、`AsymmetryChou`、`Zikkying`、`AIS-Square` 这些提交几乎全在主干上
的人，被虚高者挤到了名单后段 —— 这正是发布名单最不该出错的地方。

注意 Xuwznln 的 319 对 308：偏差只有 1.04 倍，说明这个人的工作确实进了
主干。虚高倍数本身就是「工作是否合入」的指标。

## 四、成因确认

抽样核实，排除误判可能。以 deepmd-kit 里 `wang_han@iapcm.ac.cn` 的一条
提交为例：

```
36d6c082 2026-07-30 docs(dpa4): ZBL bridging and native-spin combinations are multi-rank
```

- `git for-each-ref --contains` 结果：只在 `refs/pull/5939/*`
- `git merge-base --is-ancestor <sha> refs/heads/devel`：devel 不可达

确认该提交只存在于 PR 分支，从未进入主干。同类提交构成了虚高的主体。

Mohan Chen 的 753 次来自 `mohanchen@pku.eud.cn`（注意是 `eud` 拼写错误的
那个邮箱），主干零提交。此前 Q7 确认的「abacus_fixer 即 mohanchen」这个
身份归并结论正确，但 872 这个数字不能用。

## 五、其他文件的受损情况

| 文件 | 状态 |
|---|---|
| `summary.csv` | commits 列失真，排序失真。身份归并本身正确 |
| `contributors.md` | 同上（由 summary 渲染） |
| `contributors.json` | 同上 |
| `by_repo.csv` | 同上，逐仓库粒度 |
| `unmatched.csv` | 47 条中 31 条是主干零提交的幽灵身份 |
| `ai_assisted.csv` | 19 条，ai_commits 同样按 `--all`，其中 link89 24 次、HydrogenSulfate 9 次在主干为 0 |
| `bots.csv` | 95 条，数值同样失真，但仅用于核对误判，影响小 |
| `run_meta.json` | `contributors: 363` 等计数偏高 |

`unmatched.csv` 里的幽灵身份值得单独说：`Fei Yang
<2501213217@stu.pku.edu.cn>` 60 次、`huangming@dp.tech` 29 次 —— 这些是
squash 前的原始提交作者，他们的工作已经以 squash 提交的形式计给了合并者。
把他们列进「待人工确认」会引导出错误的补录。另有 `you@example.com` 18 次、
`agent@example.com` 14 次这类明显的占位邮箱。

`commits_not_in_upstream` 列在非 fork 仓库上等于 `commits`，因此同样失真。

## 六、fork 缓存的叠加影响

5 个 fork 缓存（abacus-develop、deepflame-dev、dflow、dpgen2、GPUMD）
都已有 `refs/remotes/upstream/*`。output_all 是复用缓存跑的，所以这些仓库
的 `commits` 还额外混入了上游独有提交。GPUMD 实测多计 14 次，涉及
Liangting、XU Ke、duanzaixu、Paul Erhart 等从未向本组织 fork 提交过的人。

这一项与 PR 分支问题叠加，但量级小得多。

## 七、结论

output_all 的**身份归并部分是可靠的** —— 邮箱聚合、bot 判定、AI 助手识别、
noreply 解析都经得起核对，Q1–Q9 确认过的口径结论依然有效。

失真集中在 `commits` 及其派生的排序上，根因单一：`git log --all` 把
`refs/pull/*/head` 计入。修掉引用范围、重跑一次即可得到可发布的结果，
不需要改动身份归并逻辑。

`docs/2026-07-30-全量统计结果报告.md` 基于 output_all 撰写，其中所有提交数
和排名都需要随重跑一并更新。
