import React, { useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, Skeleton, Typography } from 'antd';
import { usePreferences } from '../app/preferences';

const { Title, Paragraph } = Typography;

export default function AuthGate({ authenticated, loading, onLogin, children }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const { t } = usePreferences();

  const formInitialValues = useMemo(() => ({ password: '' }), []);

  const handleSubmit = async (values) => {
    setSubmitting(true);
    setError('');
    try {
      await onLogin(values.password);
    } catch (err) {
      setError(err.message || 'Login failed');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="login-screen">
        <Card className="panel-card login-panel">
          <Skeleton active paragraph={{ rows: 5 }} />
        </Card>
      </div>
    );
  }

  if (authenticated) {
    return children;
  }

  return (
    <div className="login-screen">
      <Card className="panel-card login-panel">
        <Title level={2} style={{ marginTop: 0 }}>
          {t('consoleHeadline')}
        </Title>
        <Paragraph type="secondary">{t('loginDescription')}</Paragraph>
        {error ? <Alert style={{ marginBottom: 16 }} type="error" message={error} showIcon /> : null}
        <Form layout="vertical" initialValues={formInitialValues} onFinish={handleSubmit}>
          <Form.Item label={t('password')} name="password" rules={[{ required: true, message: t('password') }]}>
            <Input.Password placeholder={t('adminPassword')} />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={submitting}>
            {t('login')}
          </Button>
        </Form>
      </Card>
    </div>
  );
}
