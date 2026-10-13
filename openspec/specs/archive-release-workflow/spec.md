# archive-release-workflow Specification

## Purpose

定义本仓库完成 OpenSpec 变更后的交付约定，使归档、主规范同步、Git 发布及 Docker 重新构建形成可核验的完整流程。每次交付可追溯到验证过的代码提交和镜像摘要，失败时能够明确判断已经完成的阶段并继续处理。

## Requirements

### Requirement: Archive includes specification synchronization and release
用户请求归档时，项目工作流 SHALL 默认同步本变更的全部增量规范、校验主规范与任务完成情况、保存带日期的归档，并执行 Git 与 Docker 发版。用户明确缩小范围时 SHALL 遵循该次要求；已有归档目录不得被覆盖，无关本地修改不得被混入发布。

#### Scenario: Completed change is archived
- **WHEN** 用户请求归档一个已完成且通过验证的变更
- **THEN** 主规范反映该变更，归档保留原始产物，后续自动进入 Git 与 Docker 发布流程，无需再次请求同一发布动作的确认

#### Scenario: User requests archive only
- **WHEN** 用户明确要求仅归档、暂不发版
- **THEN** 工作流完成指定范围并报告发布尚未执行，不执行 Git 推送或镜像发布

### Requirement: Release uses a verified commit and rebuilt image
默认 Git 发布目标 SHALL 为 origin 的 beta 分支，镜像仓库 SHALL 为 alphaply712/crypto-agent。每次发布 SHALL 从该提交重新构建前端和 Docker 镜像，完成隔离烟测后推送 Git，再发布 git-<short-sha> 与 latest 标签，并报告 Git commit 和镜像 digest。发布不得强推或包含凭据、运行数据库及无关工作。

#### Scenario: Successful release
- **WHEN** 归档提交的构建与烟测成功
- **THEN** beta 上的提交与镜像 revision 对应，两个镜像标签指向本次产物，用户获得可追溯的提交和 digest

#### Scenario: Build or smoke test fails
- **WHEN** 前端构建、Docker 构建或隔离烟测失败
- **THEN** 不推送本次 Git 提交或镜像，保留可诊断结果并报告归档已完成但发布未完成

### Requirement: Release reports partial failure and isolates runtime effects
工作流 SHALL 在任何阶段失败时停止依赖该阶段的后续动作，报告已完成与待完成的阶段，并允许基于同一归档继续处理。烟测 SHALL 使用空数据目录并关闭交易调度器。镜像发布 SHALL 不隐含重启生产容器或操作生产交易账户。

#### Scenario: Image push fails after Git succeeds
- **WHEN** Git 已推送但镜像发布失败
- **THEN** 报告已发布的 commit、镜像发布的失败阶段及仍需处理的标签，不宣称发布全部完成或重新移动归档

#### Scenario: Image is published
- **WHEN** Docker 镜像已成功发布
- **THEN** 烟测容器已清理，生产容器保持原运行状态，生产更新在用户请求部署时另行执行
