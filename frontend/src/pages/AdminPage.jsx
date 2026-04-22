import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input, List, Space, Spin, Table, Tabs, Typography } from 'antd';
import { api } from '../lib/api';

const { TextArea } = Input;

export default function AdminPage() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [configJson, setConfigJson] = useState('[]');
  const [globalSettings, setGlobalSettings] = useState({ leverage: 20, enable_scheduler: true });
  const [promptFiles, setPromptFiles] = useState([]);
  const [selectedPrompt, setSelectedPrompt] = useState('');
  const [promptContent, setPromptContent] = useState('');
  const [pricing, setPricing] = useState([]);

  const loadAll = async () => {
    setLoading(true);
    setError('');
    try {
      const [configResponse, promptResponse, pricingResponse] = await Promise.all([
        api.get('/config'),
        api.get('/config/prompts'),
        api.get('/stats/pricing'),
      ]);
      setConfigJson(JSON.stringify(configResponse.data.configs || [], null, 2));
      setGlobalSettings(configResponse.data.global || {});
      setPromptFiles(promptResponse.data.files || []);
      setPricing(pricingResponse.data.pricing || []);
    } catch (err) {
      setError(err.message || 'Failed to load admin data');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
  }, []);

  const saveConfig = async () => {
    const parsed = JSON.parse(configJson);
    await api.put('/config', { configs: parsed, global: globalSettings });
    await loadAll();
  };

  const loadPrompt = async (name) => {
    setSelectedPrompt(name);
    const response = await api.get('/config/prompts/content', { params: { name } });
    setPromptContent(response.data.content || '');
  };

  const savePrompt = async () => {
    if (!selectedPrompt) {
      return;
    }
    await api.put('/config/prompts', { name: selectedPrompt, content: promptContent });
    await loadAll();
  };

  const deletePrompt = async () => {
    if (!selectedPrompt) {
      return;
    }
    await api.delete('/config/prompts', { data: { name: selectedPrompt } });
    setSelectedPrompt('');
    setPromptContent('');
    await loadAll();
  };

  const addPricing = async (values) => {
    await api.post('/stats/pricing', values);
    await loadAll();
  };

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Card className="glass-card">
        <Typography.Title level={3} style={{ margin: 0 }}>
          Admin
        </Typography.Title>
        <Typography.Text type="secondary">配置管理、Prompt 管理和模型定价。</Typography.Text>
      </Card>

      {error ? <Alert type="error" message={error} showIcon /> : null}

      {loading ? (
        <Card className="glass-card">
          <Spin />
        </Card>
      ) : (
        <Tabs
          items={[
            {
              key: 'config',
              label: 'Config',
              children: (
                <Card className="glass-card">
                  <Space direction="vertical" style={{ width: '100%' }} size="middle">
                    <Form layout="inline">
                      <Form.Item label="Leverage">
                        <Input
                          value={globalSettings.leverage}
                          onChange={(event) => setGlobalSettings((prev) => ({ ...prev, leverage: Number(event.target.value) }))}
                        />
                      </Form.Item>
                      <Form.Item label="Scheduler">
                        <Input
                          value={String(globalSettings.enable_scheduler)}
                          onChange={(event) =>
                            setGlobalSettings((prev) => ({ ...prev, enable_scheduler: event.target.value === 'true' }))
                          }
                        />
                      </Form.Item>
                    </Form>
                    <TextArea
                      className="code-editor"
                      value={configJson}
                      onChange={(event) => setConfigJson(event.target.value)}
                    />
                    <Button type="primary" onClick={saveConfig}>
                      保存配置
                    </Button>
                  </Space>
                </Card>
              ),
            },
            {
              key: 'prompts',
              label: 'Prompts',
              children: (
                <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
                  <Card className="glass-card" title="Prompt Files">
                    <List
                      dataSource={promptFiles}
                      renderItem={(item) => (
                        <List.Item style={{ cursor: 'pointer' }} onClick={() => loadPrompt(item)}>
                          {item}
                        </List.Item>
                      )}
                    />
                  </Card>
                  <Card className="glass-card" title={selectedPrompt || 'Prompt Content'}>
                    <Space direction="vertical" style={{ width: '100%' }}>
                      <TextArea
                        className="code-editor"
                        value={promptContent}
                        onChange={(event) => setPromptContent(event.target.value)}
                      />
                      <Space>
                        <Button type="primary" onClick={savePrompt} disabled={!selectedPrompt}>
                          保存 Prompt
                        </Button>
                        <Button danger onClick={deletePrompt} disabled={!selectedPrompt}>
                          删除 Prompt
                        </Button>
                      </Space>
                    </Space>
                  </Card>
                </div>
              ),
            },
            {
              key: 'pricing',
              label: 'Pricing',
              children: (
                <Card className="glass-card">
                  <Space direction="vertical" size="large" style={{ width: '100%' }}>
                    <Form layout="inline" onFinish={addPricing}>
                      <Form.Item name="model" rules={[{ required: true, message: '模型名必填' }]}>
                        <Input placeholder="model" />
                      </Form.Item>
                      <Form.Item name="input_price">
                        <Input placeholder="input price" />
                      </Form.Item>
                      <Form.Item name="output_price">
                        <Input placeholder="output price" />
                      </Form.Item>
                      <Button htmlType="submit" type="primary">
                        新增 / 更新
                      </Button>
                    </Form>
                    <Table
                      rowKey={(row) => row.model}
                      dataSource={pricing}
                      columns={[
                        { title: 'Model', dataIndex: 'model' },
                        { title: 'Input / M', dataIndex: 'input_price_per_m' },
                        { title: 'Output / M', dataIndex: 'output_price_per_m' },
                        { title: 'Currency', dataIndex: 'currency' },
                      ]}
                    />
                  </Space>
                </Card>
              ),
            },
          ]}
        />
      )}
    </Space>
  );
}
