import React, { useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, InputNumber, Space, Switch, Tag, Typography } from 'antd';
import { KeyOutlined, SafetyCertificateOutlined, SettingOutlined } from '@ant-design/icons';
import { api } from '../lib/api';
import { usePreferences } from '../app/usePreferences';
import './AdminPage.css';

const { Title, Paragraph, Text } = Typography;

function generateRandomSecret(length = 32) {
  const bytes = new Uint8Array(length);
  window.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('').slice(0, length);
}

export default function SetupPage({ status, onComplete }) {
  const { t, locale } = usePreferences();
  const zh = locale === 'zh';
  const [form] = Form.useForm();
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  const initialValues = useMemo(
    () => ({
      jwt_expire_hours: 8,
      port: 7860,
      timezone: 'Asia/Shanghai',
      run_scheduler_in_web: true,
    }),
    [],
  );

  const submit = async (values) => {
    setSubmitting(true);
    setError('');
    try {
      const response = await api.post('/setup/apply', values);
      setResult(response.data);
      onComplete?.(response.data);
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || 'Setup failed');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-screen setup-screen">
      <Card className="panel-card setup-panel">
        <div className="setup-panel__header">
          <Space size={8} wrap>
            <span className="admin-topbar__eyebrow">{zh ? '首次初始化向导' : 'Initial Setup'}</span>
            {status?.docker_runtime ? <Tag color="blue">Docker</Tag> : null}
          </Space>
          <Title level={2} style={{ margin: 0 }}>{t('setupHeadline')}</Title>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            {t('setupDesc')}
          </Paragraph>
        </div>

        {status?.docker_runtime ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={t('dockerDetected')}
            description={t('dockerDesc')}
          />
        ) : null}
        {status?.weak_keys?.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={`${t('configMasterKey')}: ${status.weak_keys.join(', ')}`}
          />
        ) : null}
        {error ? <Alert type="error" showIcon style={{ marginBottom: 16 }} message={error} /> : null}
        {result ? (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message={t('setupCompleted')}
            description={t('restartBackend')}
          />
        ) : null}

        <Form form={form} layout="vertical" initialValues={initialValues} onFinish={submit}>
          <div className="setup-section">
            <div className="setup-section__title">
              <span className="setup-section__step">1</span>
              <SafetyCertificateOutlined />
              <span>{zh ? '管理员密码与加密密钥' : 'Admin Password & Encryption Keys'}</span>
            </div>
            <Form.Item
              label={t('adminPassword')}
              name="admin_password"
              rules={[{ required: true }]}
              extra={zh ? '用于登录本后台控制台，请设置便于记忆且足够安全的密码。' : 'Used to sign in to this console. Choose a strong password.'}
            >
              <Input.Password placeholder={zh ? '输入管理员登录密码' : 'Enter admin password'} />
            </Form.Item>
            <Form.Item
              label={
                <Space size={8}>
                  <span>{t('jwtSecret')}</span>
                  <Button
                    type="link"
                    size="small"
                    icon={<KeyOutlined />}
                    style={{ padding: 0, height: 'auto' }}
                    onClick={() => form.setFieldValue('jwt_secret', generateRandomSecret(32))}
                  >
                    {zh ? '随机生成' : 'Generate'}
                  </Button>
                </Space>
              }
              name="jwt_secret"
              rules={[{ required: true, min: 16 }]}
              extra={zh ? '用于签发登录令牌（至少 16 位），可直接点击上方随机生成。' : 'Signs login sessions (at least 16 characters).'}
            >
              <Input.Password placeholder={zh ? '至少 16 位随机字符串' : 'At least 16 characters'} />
            </Form.Item>
            <Form.Item
              label={
                <Space size={8}>
                  <span>{t('configMasterKey')}</span>
                  <Button
                    type="link"
                    size="small"
                    icon={<KeyOutlined />}
                    style={{ padding: 0, height: 'auto' }}
                    onClick={() => form.setFieldValue('config_master_key', generateRandomSecret(32))}
                  >
                    {zh ? '随机生成' : 'Generate'}
                  </Button>
                </Space>
              }
              name="config_master_key"
              rules={[{ required: true, min: 16 }]}
              style={{ marginBottom: 0 }}
              extra={zh ? '用于加密保存交易所与模型 API 密钥（至少 16 位），请妥善备份。' : 'Encrypts exchange & LLM API keys (at least 16 characters).'}
            >
              <Input.Password placeholder={zh ? '至少 16 位加密主密钥' : 'At least 16 characters'} />
            </Form.Item>
          </div>

          <div className="setup-section">
            <div className="setup-section__title">
              <span className="setup-section__step">2</span>
              <SettingOutlined />
              <span>{zh ? '运行环境与调度设置' : 'Runtime & Scheduler'}</span>
            </div>
            <div className="admin-form-grid">
              <Form.Item label={t('jwtExpireHours')} name="jwt_expire_hours">
                <InputNumber min={1} max={720} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item label={t('port')} name="port">
                <InputNumber min={1} max={65535} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item label={t('timezone')} name="timezone">
                <Input />
              </Form.Item>
              <Form.Item label={t('runSchedulerInWeb')} name="run_scheduler_in_web" valuePropName="checked">
                <Switch />
              </Form.Item>
            </div>
          </div>

          <Button type="primary" size="large" htmlType="submit" loading={submitting} block>
            {t('saveSetup')}
          </Button>
        </Form>

        <Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0, textAlign: 'center', fontSize: 12 }}>
          {zh ? '配置将写入：' : 'Saved to: '}
          <Text code>{status?.env_path || '.env'}</Text>
        </Paragraph>
      </Card>
    </div>
  );
}
