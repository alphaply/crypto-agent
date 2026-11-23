import React from 'react';
import { Empty, Tag } from 'antd';

const CATEGORY_ORDER = ['shared', 'trading', 'paper'];
const CATEGORY_LABELS = {
  shared: ['通用查询与情报', 'Shared market & intelligence'],
  trading: ['交易与账户操作', 'Trading & account operations'],
  paper: ['QQBot 模拟交易专属', 'QQBot paper trading'],
};

function scopeBadge(scope, zh) {
  if (scope === 'read') return <Tag>{zh ? '只读' : 'Read'}</Tag>;
  if (scope === 'trade') return <Tag color="blue">{zh ? '交易' : 'Trade'}</Tag>;
  if (scope === 'trade / cancel') return <Tag color="purple">{zh ? '交易 / 撤单' : 'Trade / Cancel'}</Tag>;
  return <Tag>{scope}</Tag>;
}

export default function McpToolCatalog({ tools = [], zh = true, categories = CATEGORY_ORDER }) {
  const groups = categories
    .map((category) => ({
      category,
      title: CATEGORY_LABELS[category]?.[zh ? 0 : 1] || category,
      items: tools.filter((tool) => (tool.category || 'trading') === category),
    }))
    .filter((group) => group.items.length > 0);

  if (!groups.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={zh ? '暂无可用工具信息' : 'No tools available'} />;
  }

  return (
    <div className="paper-tool-groups">
      {groups.map((group) => (
        <section key={group.category} className="paper-tool-group">
          <h4>{group.title}</h4>
          <div className="paper-tool-grid">
            {group.items.map((tool) => {
              const title = (zh ? tool.title_zh : tool.title_en) || tool.name;
              const desc = (zh ? tool.description_zh : tool.description_en) || tool.description || '';
              return (
                <div key={tool.name} className="paper-tool-card">
                  <div className="paper-tool-head">
                    <strong>{title}</strong>
                    {scopeBadge(tool.scope, zh)}
                  </div>
                  <code className="paper-tool-code">{tool.name}</code>
                  {desc && <p className="paper-tool-desc">{desc}</p>}
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
