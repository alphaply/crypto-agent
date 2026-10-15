## Purpose

为管理员提供独立的 QQBot Paper 模拟交易配置入口，使模拟账户配置与 MCP 通用连接配置容易区分，并保持既有 Paper 设置、权限、账户数据及密钥创建流程可用。

## ADDED Requirements

### Requirement: Paper administration has its own tab
管理页面 SHALL 将现有“QQBot 模拟交易 · Paper”面板放入独立 tab。切换 tab SHALL 能访问原有配置和创建 Paper Key 功能，不改变原有权限、namespace、账户数据及配置保存语义。

#### Scenario: Open Paper administration
- **WHEN** 管理员进入管理页面并选择 Paper tab
- **THEN** 展示现有 Paper 面板功能，MCP 通用配置区域不再内嵌同一面板

#### Scenario: Preserve configuration and key creation
- **WHEN** 管理员在独立 tab 保存现有 Paper 配置或点击创建 Paper Key
- **THEN** 使用原有保存和密钥创建流程及权限，切换 tab 后可读取已保存设置，不重建或清空账户

#### Scenario: Responsive navigation
- **WHEN** 管理员使用窄屏访问管理页面
- **THEN** 可通过现有响应式 tab 导航进入 Paper 配置，保存与密钥入口仍可操作
