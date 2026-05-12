import React, { useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, InputNumber, Switch, Typography } from 'antd';
import { api } from '../lib/api';
import { usePreferences } from '../app/preferences';

const { Title, Paragraph, Text } = Typography;

export default function SetupPage({ status, onComplete }) {
  const { t } = usePreferences();
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
        <Title level={2} style={{ marginTop: 0 }}>{t('setupHeadline')}</Title>
        <Paragraph type="secondary">
          {t('setupDesc')}
        </Paragraph>
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
          <Form.Item label={t('adminPassword')} name="admin_password" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item label={t('jwtSecret')} name="jwt_secret" rules={[{ required: true, min: 16 }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item label={t('configMasterKey')} name="config_master_key" rules={[{ required: true, min: 16 }]}>
            <Input.Password />
          </Form.Item>
          <div className="admin-form-grid">
            <Form.Item label={t('jwtExpireHours')} name="jwt_expire_hours">
              <InputNumber min={1} max={720} />
            </Form.Item>
            <Form.Item label={t('port')} name="port">
              <InputNumber min={1} max={65535} />
            </Form.Item>
            <Form.Item label={t('timezone')} name="timezone">
              <Input />
            </Form.Item>
            <Form.Item label={t('runSchedulerInWeb')} name="run_scheduler_in_web" valuePropName="checked">
              <Switch />
            </Form.Item>
          </div>
          <Button type="primary" htmlType="submit" loading={submitting} block>
            {t('saveSetup')}
          </Button>
        </Form>
        <Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
          <Text code>{status?.env_path || '.env'}</Text>
        </Paragraph>
      </Card>
    </div>
  );
}
